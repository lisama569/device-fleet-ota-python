"""业务逻辑：注册、心跳、下发、回滚。

一致性约定：
- 所有变更都在 store._lock 内完成，多设备并发下状态不撕裂。
- 心跳按时间戳单调推进：乱序/重复到达被拒绝为 STALE_HEARTBEAT，不计数、不回退。
- 下发前先整体校验，再原子创建指令；任一目标非法则整批拒绝。
- 回滚幂等：重复触发返回首次回滚报告，不二次改动设备版本。
"""
from __future__ import annotations

import time

from .models import (
    Device,
    Instruction,
    InstructionKind,
    InstructionStatus,
    RejectReason,
    Rollout,
    RolloutStatus,
)
from .store import FleetStore


class FleetError(Exception):
    """携带可区分拒绝原因的业务异常。"""

    def __init__(self, reason: RejectReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


class FleetService:
    def __init__(self, store: FleetStore) -> None:
        self.store = store

    # ---- 设备注册与心跳 ----

    def register_device(self, device_id: str, firmware_version: str, config_version: str) -> dict:
        if not device_id or not firmware_version:
            raise FleetError(RejectReason.INVALID_PAYLOAD, "device_id 与 firmware_version 必填")
        store = self.store
        with store._lock:
            dev = store.devices.get(device_id)
            if dev is None:
                dev = Device(device_id=device_id)
                store.devices[device_id] = dev
            dev.firmware_version = firmware_version
            dev.config_version = config_version
            return self._device_view(dev)

    def heartbeat(self, device_id: str, firmware_version: str, config_version: str, ts: float | None) -> dict:
        store = self.store
        with store._lock:
            dev = store.devices.get(device_id)
            if dev is None:
                raise FleetError(RejectReason.UNKNOWN_DEVICE, f"设备未注册: {device_id}")
            if not firmware_version or ts is None or ts <= 0:
                raise FleetError(RejectReason.INVALID_PAYLOAD, "firmware_version 必填且 ts 必须为正数时间戳")
            if ts <= dev.last_heartbeat_ts:
                # 乱序或重复到达：拒绝，不计数、不回退在线状态
                raise FleetError(
                    RejectReason.STALE_HEARTBEAT,
                    f"ts={ts} 不晚于已记录的 {dev.last_heartbeat_ts}",
                )
            dev.last_heartbeat_ts = ts
            dev.heartbeat_count += 1
            dev.firmware_version = firmware_version
            dev.config_version = config_version or dev.config_version
            return self._device_view(dev)

    # ---- 版本仓库 ----

    def add_firmware_version(self, version: str, payload: dict) -> dict:
        return self._add_version(self.store.firmware_versions, version, payload)

    def add_config_version(self, version: str, payload: dict) -> dict:
        return self._add_version(self.store.config_versions, version, payload)

    def _add_version(self, repo: dict, version: str, payload: dict) -> dict:
        if not version:
            raise FleetError(RejectReason.INVALID_PAYLOAD, "version 必填")
        with self.store._lock:
            repo[version] = dict(payload or {})
            return {"version": version, "registered": True}

    # ---- 分批下发 ----

    def create_rollout(self, kind: str, version: str, targets: list[str]) -> dict:
        store = self.store
        with store._lock:
            try:
                kind_enum = InstructionKind(kind)
            except ValueError:
                raise FleetError(RejectReason.INVALID_PAYLOAD, f"非法下发类型: {kind}") from None
            repo = store.firmware_versions if kind_enum is InstructionKind.FIRMWARE else store.config_versions
            if version not in repo:
                raise FleetError(RejectReason.VERSION_NOT_FOUND, f"{kind} 版本不存在: {version}")
            unique_targets = list(dict.fromkeys(targets or []))
            if not unique_targets:
                raise FleetError(RejectReason.EMPTY_TARGETS, "目标设备列表为空")
            now = time.time()
            # 先整体校验，任一目标非法则整批拒绝，不产生半成品指令
            for device_id in unique_targets:
                dev = store.devices.get(device_id)
                if dev is None:
                    raise FleetError(RejectReason.UNKNOWN_DEVICE, f"目标设备未注册: {device_id}")
                if not dev.is_online(now, store.online_threshold_sec):
                    raise FleetError(RejectReason.TARGET_OFFLINE, f"目标设备离线: {device_id}")
                if dev.pending_instruction is not None:
                    raise FleetError(
                        RejectReason.CONFLICTING_INSTRUCTION,
                        f"设备 {device_id} 存在未完成的指令 "
                        f"{dev.pending_instruction.instruction_id}，不得并发冲突下发",
                    )
            rollout = Rollout(
                rollout_id=store.next_id("ro"),
                kind=kind_enum,
                version=version,
                targets=unique_targets,
            )
            for device_id in unique_targets:
                dev = store.devices[device_id]
                ins = Instruction(
                    instruction_id=store.next_id("ins"),
                    rollout_id=rollout.rollout_id,
                    device_id=device_id,
                    kind=kind_enum,
                    target_version=version,
                    prev_version=dev.firmware_version if kind_enum is InstructionKind.FIRMWARE else dev.config_version,
                )
                rollout.instructions[ins.instruction_id] = ins
                store.instructions[ins.instruction_id] = ins
                dev.pending_instruction = ins
            store.rollouts[rollout.rollout_id] = rollout
            return self._rollout_view(rollout)

    def report_instruction(self, instruction_id: str, success: bool, reason: str = "") -> dict:
        store = self.store
        with store._lock:
            ins = store.instructions.get(instruction_id)
            if ins is None:
                raise FleetError(RejectReason.INSTRUCTION_NOT_FOUND, f"指令不存在: {instruction_id}")
            if ins.status is not InstructionStatus.PENDING:
                # 重复上报幂等：返回当前终态，不二次变更
                return self._instruction_view(ins)
            dev = store.devices[ins.device_id]
            if success:
                ins.status = InstructionStatus.APPLIED
                if ins.kind is InstructionKind.FIRMWARE:
                    dev.firmware_version = ins.target_version
                else:
                    dev.config_version = ins.target_version
            else:
                ins.status = InstructionStatus.FAILED
                ins.reason = reason or "device_reported_failure"
                # 失败不改动设备当前有效版本
            if dev.pending_instruction is ins:
                dev.pending_instruction = None
            self._refresh_rollout_status(store.rollouts[ins.rollout_id])
            return self._instruction_view(ins)

    # ---- 回滚 ----

    def rollback_rollout(self, rollout_id: str) -> dict:
        store = self.store
        with store._lock:
            rollout = store.rollouts.get(rollout_id)
            if rollout is None:
                raise FleetError(RejectReason.ROLLOUT_NOT_FOUND, f"下发批次不存在: {rollout_id}")
            if rollout.rolled_back:
                # 重复触发幂等：返回首次报告，不二次改动设备版本
                report = dict(rollout.rollback_report)
                report["replay_reason"] = RejectReason.ROLLOUT_ALREADY_ROLLED_BACK.value
                return report
            report: dict[str, dict] = {}
            for ins in rollout.instructions.values():
                dev = store.devices[ins.device_id]
                if ins.status is InstructionStatus.APPLIED:
                    # 已应用：回退到下发前版本
                    if ins.kind is InstructionKind.FIRMWARE:
                        dev.firmware_version = ins.prev_version
                    else:
                        dev.config_version = ins.prev_version
                    ins.status = InstructionStatus.ROLLED_BACK
                    ins.reason = "reverted_to_previous"
                    action = "reverted"
                elif ins.status is InstructionStatus.PENDING:
                    # 下发中途：取消未生效指令，设备保持当前有效版本
                    ins.status = InstructionStatus.ROLLED_BACK
                    ins.reason = "cancelled_before_apply"
                    action = "cancelled"
                else:
                    # FAILED：本就未生效，无需变更
                    ins.reason = ins.reason or "no_action_failed_before"
                    action = "no_action"
                if dev.pending_instruction is ins:
                    dev.pending_instruction = None
                report[ins.device_id] = {
                    "device_id": ins.device_id,
                    "instruction_id": ins.instruction_id,
                    "action": action,
                    "reason": ins.reason,
                    "firmware_version": dev.firmware_version,
                    "config_version": dev.config_version,
                }
            rollout.status = RolloutStatus.ROLLED_BACK
            rollout.rolled_back = True
            rollout.rollback_report = report
            return report

    # ---- 查询 ----

    def device_state(self, device_id: str) -> dict:
        store = self.store
        with store._lock:
            dev = store.devices.get(device_id)
            if dev is None:
                raise FleetError(RejectReason.UNKNOWN_DEVICE, f"设备未注册: {device_id}")
            return self._device_view(dev)

    # ---- 内部视图 ----

    def _device_view(self, dev: Device) -> dict:
        store = self.store
        now = time.time()
        return {
            "device_id": dev.device_id,
            "firmware_version": dev.firmware_version,
            "config_version": dev.config_version,
            "online": dev.is_online(now, store.online_threshold_sec),
            "last_heartbeat_ts": dev.last_heartbeat_ts,
            "heartbeat_count": dev.heartbeat_count,
            "pending_instruction": (
                dev.pending_instruction.instruction_id if dev.pending_instruction else None
            ),
        }

    def _instruction_view(self, ins: Instruction) -> dict:
        return {
            "instruction_id": ins.instruction_id,
            "rollout_id": ins.rollout_id,
            "device_id": ins.device_id,
            "kind": ins.kind.value,
            "target_version": ins.target_version,
            "prev_version": ins.prev_version,
            "status": ins.status.value,
            "reason": ins.reason,
        }

    def _rollout_view(self, rollout: Rollout) -> dict:
        return {
            "rollout_id": rollout.rollout_id,
            "kind": rollout.kind.value,
            "version": rollout.version,
            "targets": list(rollout.targets),
            "status": rollout.status.value,
            "instructions": [self._instruction_view(i) for i in rollout.instructions.values()],
        }

    def _refresh_rollout_status(self, rollout: Rollout) -> None:
        if rollout.rolled_back:
            return
        statuses = {i.status for i in rollout.instructions.values()}
        if InstructionStatus.PENDING in statuses:
            rollout.status = RolloutStatus.IN_PROGRESS
        elif statuses == {InstructionStatus.APPLIED}:
            rollout.status = RolloutStatus.COMPLETED
        else:
            rollout.status = RolloutStatus.PARTIAL_FAILED

"""业务服务层：所有并发控制与拒绝原因判定集中在这里。"""
from __future__ import annotations

import re

from app import audit
from app.errors import (
    DeviceAlreadyRegistered,
    DeviceNotFound,
    EmptyTargetScope,
    HeartbeatPayloadInvalid,
    NoMatchingTarget,
    ReportedVersionInvalid,
    StaleHeartbeatRejected,
    TargetVersionConflict,
    VersionNotFound,
)
from app.models import (
    ArtifactKind,
    AssignmentState,
    AssignmentView,
    DeploymentView,
    DeviceStatus,
    DeviceView,
    RegisterRequest,
    RegisterVersionRequest,
    ResultReport,
    RollbackView,
    HeartbeatRequest,
    DeployRequest,
)
from app.store import DeviceRecord, Store, VersionRecord

# 心跳在线判定阈值（秒）：距当前时间超过该值视为离线
ONLINE_THRESHOLD_SECONDS = 30.0

# 版本号：字母/数字/点/横线/下划线，1~64 字符
_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _valid_version(value: str) -> bool:
    return bool(_VERSION_RE.match(value))


class OTAService:
    def __init__(self, store: Store, rollback_hook=None) -> None:
        self.store = store
        # 测试钩子：在回滚窗口登记完成、真正生效前被调用，签名 hook(rollback_id)
        self.rollback_hook = rollback_hook

    def register_device(self, req: RegisterRequest):
        with self.store.registry_lock:
            if req.device_id in self.store.devices:
                raise DeviceAlreadyRegistered(
                    f"device {req.device_id} already registered",
                    device_id=req.device_id,
                )
            record = DeviceRecord(device_id=req.device_id, model=req.model)
            if req.firmware_version:
                record.active["firmware"] = req.firmware_version
                record.reported["firmware"] = req.firmware_version
            if req.config_version:
                record.active["config"] = req.config_version
                record.reported["config"] = req.config_version
            self.store.devices[req.device_id] = record
        return {"device_id": req.device_id, "registered": True}

    def heartbeat(self, req: HeartbeatRequest):
        # 1) 字段缺失 / 非法与乱序是不同拒绝原因，先做无锁的入参校验
        if not req.device_id:
            raise HeartbeatPayloadInvalid("field 'device_id' is required")
        if req.ts is None:
            raise HeartbeatPayloadInvalid("field 'ts' is required")

        record = self.store.devices.get(req.device_id)
        if record is None:
            raise DeviceNotFound(
                f"device {req.device_id} not registered",
                device_id=req.device_id,
            )

        reported: dict[str, str] = {}
        if req.firmware_version is not None:
            if not _valid_version(req.firmware_version):
                audit.log_heartbeat(
                    device_id=req.device_id,
                    ts=req.ts,
                    reported={"firmware": req.firmware_version},
                    decision="rejected",
                    reason="firmware_version format invalid",
                )
                raise ReportedVersionInvalid(
                    "field 'firmware_version' has invalid format",
                    device_id=req.device_id,
                )
            reported["firmware"] = req.firmware_version
        if req.config_version is not None:
            if not _valid_version(req.config_version):
                audit.log_heartbeat(
                    device_id=req.device_id,
                    ts=req.ts,
                    reported={"config": req.config_version},
                    decision="rejected",
                    reason="config_version format invalid",
                )
                raise ReportedVersionInvalid(
                    "field 'config_version' has invalid format",
                    device_id=req.device_id,
                )
            reported["config"] = req.config_version

        # 2) 同一设备串行化：并发心跳不会重复计数，乱序心跳不会让时间戳回退
        with record.lock:
            last = record.last_heartbeat_ts
            if last is not None and req.ts < last:
                audit.log_heartbeat(
                    device_id=req.device_id,
                    ts=req.ts,
                    reported=reported,
                    decision="rejected",
                    reason=f"stale ts {req.ts} < last {last}",
                )
                raise StaleHeartbeatRejected(
                    f"heartbeat ts {req.ts} is older than last {last}",
                    device_id=req.device_id,
                )
            if last is not None and req.ts == last:
                # 同时间戳重复心跳：幂等接受，但不重复计数、不覆盖版本快照
                status = self._status_of(record)
                audit.log_heartbeat(
                    device_id=req.device_id,
                    ts=req.ts,
                    reported=reported,
                    decision="accepted_duplicate",
                    reason="identical ts, idempotent accept",
                )
                return {
                    "device_id": req.device_id,
                    "accepted": False,
                    "duplicate": True,
                    "status": status.value,
                }

            record.last_heartbeat_ts = req.ts
            record.reported.update(reported)

            status = self._status_of(record)
            audit.log_heartbeat(
                device_id=req.device_id,
                ts=req.ts,
                reported=reported,
                decision="accepted",
                reason=f"last_ts={req.ts}, within threshold => {status.value}",
            )
            return {
                "device_id": req.device_id,
                "accepted": True,
                "duplicate": False,
                "status": status.value,
            }

    def register_version(self, req: RegisterVersionRequest):
        if not _valid_version(req.version):
            raise ReportedVersionInvalid(
                f"version '{req.version}' has invalid format"
            )
        with self.store.registry_lock:
            key = (req.kind, req.version)
            if key in self.store.versions:
                # 版本重复注册做幂等处理，不视为错误
                return {
                    "kind": req.kind.value,
                    "version": req.version,
                    "registered": False,
                    "duplicate": True,
                }
            self.store.versions[key] = VersionRecord(
                kind=req.kind, version=req.version, content=req.content
            )
            return {
                "kind": req.kind.value,
                "version": req.version,
                "registered": True,
                "duplicate": False,
            }

    def deploy(self, req: DeployRequest):
        from app.store import (
            AssignmentRecord,
            DeploymentRecord,
            new_id,
        )

        kind_str = req.kind.value

        # 规则 1：目标范围不允许为空（显式区分"没圈选"与"圈了但没人"）
        if not req.target_ids and not req.target_models:
            raise EmptyTargetScope(
                "at least one of target_ids/target_models is required"
            )

        # 规则 2：版本必须在仓库中存在
        with self.store.registry_lock:
            if (req.kind, req.version) not in self.store.versions:
                raise VersionNotFound(
                    f"{kind_str} version '{req.version}' is not registered"
                )

            # 规则 3：解析目标范围（取并集去重）
            selected: dict[str, DeviceRecord] = {}
            if req.target_ids:
                for dev_id in dict.fromkeys(req.target_ids):  # 去重保序
                    record = self.store.devices.get(dev_id)
                    if record is not None:
                        selected[dev_id] = record
            if req.target_models:
                models = set(req.target_models)
                for dev_id, record in self.store.devices.items():
                    if record.model in models:
                        selected[dev_id] = record

            if not selected:
                raise NoMatchingTarget(
                    "target scope resolved to zero registered devices"
                )

            deployment_id = new_id("dep")
            ordered = [selected[k] for k in sorted(selected)]

        # 规则 4：整批原子判定冲突。按设备 id 排序加锁，避免多批次交叉时死锁。
        for record in ordered:
            record.lock.acquire()
        try:
            conflicts: list[str] = []
            reused_dep_ids: set[str] = set()
            for record in ordered:
                existing = record.pending.get(kind_str)
                if existing is not None:
                    other_dep, other_ver = existing
                    if other_ver == req.version:
                        # 同版本同设备的在途指令：记录待复用的批次
                        reused_dep_ids.add(other_dep)
                    else:
                        conflicts.append(
                            f"{record.device_id}: pending {kind_str} "
                            f"{other_ver} (deployment {other_dep})"
                        )
                if record.rollback_in_flight.get(kind_str):
                    conflicts.append(
                        f"{record.device_id}: rollback in flight for {kind_str}"
                    )

            # 部分设备已有同版本在途批次但目标不完全重叠：拒绝，避免产生孤儿指令
            if not conflicts and reused_dep_ids and len(reused_dep_ids) != 1:
                conflicts.append(
                    "targets span multiple existing deployments of same version"
                )
            if not conflicts and reused_dep_ids:
                all_reused = all(
                    record.pending.get(kind_str, (None, None))[0] in reused_dep_ids
                    for record in ordered
                )
                if not all_reused:
                    conflicts.append(
                        "some targets already have an in-flight deployment of "
                        "the same version; complete it before widening scope"
                    )

            # 全部目标设备都在同一个同版本在途批次上：幂等返回该批次
            if not conflicts and len(reused_dep_ids) == 1:
                return self._deployment_view(
                    self.store.deployments[next(iter(reused_dep_ids))]
                )

            if conflicts:
                # 冲突时整批拒绝，不产生任何部分指令，保证"同一设备无互相冲突指令"
                detail = "; ".join(conflicts)
                for record in ordered:
                    audit.log_deploy(
                        deployment_id=deployment_id,
                        device_id=record.device_id,
                        kind=kind_str,
                        version=req.version,
                        decision="rejected",
                        reason=f"batch conflict: {detail}",
                    )
                err = TargetVersionConflict(
                    f"target conflicts with in-flight instructions: {detail}"
                )
                err.conflicts = conflicts
                raise err

            assignments: dict[str, AssignmentRecord] = {}
            for record in ordered:
                previous = record.active.get(kind_str)
                assignments[record.device_id] = AssignmentRecord(
                    deployment_id=deployment_id,
                    device_id=record.device_id,
                    kind=req.kind,
                    version=req.version,
                    previous_version=previous,
                )
                record.pending[kind_str] = (deployment_id, req.version)
                audit.log_deploy(
                    deployment_id=deployment_id,
                    device_id=record.device_id,
                    kind=kind_str,
                    version=req.version,
                    decision="accepted",
                    reason=f"previous_active={previous}",
                )

            deployment = DeploymentRecord(
                deployment_id=deployment_id,
                kind=req.kind,
                version=req.version,
                assignments=assignments,
                created_ts=self.store.clock(),
            )
            with self.store.registry_lock:
                self.store.deployments[deployment_id] = deployment
        finally:
            for record in ordered:
                record.lock.release()

        return self._deployment_view(deployment)

    def report_result(
        self, req: ResultReport, deployment_id: str | None = None
    ):
        from app.errors import HeartbeatPayloadInvalid

        if not req.device_id:
            raise HeartbeatPayloadInvalid("field 'device_id' is required")
        if req.kind is None:
            raise HeartbeatPayloadInvalid("field 'kind' is required")
        if req.success is None:
            raise HeartbeatPayloadInvalid("field 'success' is required")

        record = self.store.devices.get(req.device_id)
        if record is None:
            raise DeviceNotFound(
                f"device {req.device_id} not registered",
                device_id=req.device_id,
            )

        deployment = (
            self.store.deployments.get(deployment_id) if deployment_id else None
        )
        if deployment_id is not None and deployment is None:
            from app.errors import DeploymentNotFound

            raise DeploymentNotFound(f"deployment {deployment_id} not found")

        kind_str = req.kind.value
        with record.lock:
            pending = record.pending.get(kind_str)
            if deployment is not None:
                assignment = deployment.assignments.get(req.device_id)
                if assignment is None:
                    return {
                        "device_id": req.device_id,
                        "applied": False,
                        "reason": (
                            f"device is not a target of deployment {deployment_id}"
                        ),
                    }
                if assignment.state != AssignmentState.PENDING:
                    # 重复上报：幂等返回当前终态，绝不二次改写有效版本
                    return {
                        "device_id": req.device_id,
                        "applied": False,
                        "duplicate": True,
                        "state": assignment.state.value,
                        "reason": "terminal result already recorded",
                    }
                if pending is None or pending[0] != deployment_id:
                    return {
                        "device_id": req.device_id,
                        "applied": False,
                        "reason": "deployment is no longer the current assignment",
                    }
            elif pending is None:
                return {
                    "device_id": req.device_id,
                    "applied": False,
                    "reason": "no pending assignment for this kind",
                }
            else:
                assignment = (
                    self.store.deployments[pending[0]]
                    .assignments[req.device_id]
                )

            pending_dep_id, pending_version = pending
            if req.success:
                # 成功才切换有效版本，并把旧版本记为可回滚目标
                previous = record.active.get(kind_str)
                record.previous[kind_str] = previous if previous is not None else ""
                record.active[kind_str] = pending_version
                assignment.state = AssignmentState.SUCCEEDED
                assignment.reason = req.reason or "applied"
                applied = True
            else:
                # 失败：有效版本保持不变，仅清出在途指令
                assignment.state = AssignmentState.FAILED
                assignment.reason = req.reason or "device reported failure"
                applied = False

            record.pending.pop(kind_str, None)
            if req.success:
                record.reported[kind_str] = pending_version
            audit.log_deploy(
                deployment_id=pending_dep_id,
                device_id=req.device_id,
                kind=kind_str,
                version=pending_version,
                decision="succeeded" if req.success else "failed",
                reason=assignment.reason,
            )
            return {
                "device_id": req.device_id,
                "deployment_id": pending_dep_id,
                "applied": applied,
                "state": assignment.state.value,
                "active_version": record.active.get(kind_str),
            }

    def rollback(self, deployment_id: str):
        from app.errors import DeploymentNotFound
        from app.store import RollbackRecord, new_id

        deployment = self.store.deployments.get(deployment_id)
        if deployment is None:
            raise DeploymentNotFound(f"deployment {deployment_id} not found")

        kind_str = deployment.kind.value

        with self.store.registry_lock:
            existing = next(
                (
                    rb
                    for rb in self.store.rollbacks.values()
                    if rb.deployment_id == deployment_id
                ),
                None,
            )
            if existing is not None:
                if existing.in_progress:
                    # 回滚窗口内的重复触发：明确拒绝，保证只有一个执行者
                    from app.errors import RollbackInProgress

                    raise RollbackInProgress(
                        f"rollback {existing.rollback_id} is still in progress"
                    )
                # 已完成的重复触发：幂等返回既有回滚单
                return self._rollback_view(existing)

            records = [
                self.store.devices[device_id]
                for device_id in sorted(deployment.assignments)
                if device_id in self.store.devices
            ]
            rollback_id = new_id("rb")
            record = RollbackRecord(
                rollback_id=rollback_id, deployment_id=deployment_id
            )
            self.store.rollbacks[rollback_id] = record

        # 排序加锁，与 deploy 保持同一加锁顺序，避免交叉死锁
        for device in records:
            device.lock.acquire()
        try:
            # 任何一个目标设备已有在途回滚 -> 整批拒绝并清理刚登记的回滚单
            blocked = [
                device.device_id
                for device in records
                if kind_str in device.rollback_in_flight
            ]
            if blocked:
                with self.store.registry_lock:
                    self.store.rollbacks.pop(rollback_id, None)
                from app.errors import RollbackInProgress

                raise RollbackInProgress(
                    f"rollback already in progress for devices: {blocked}"
                )

            # 阶段 1：登记在途回滚，判定每个设备的终态；真正生效在阶段 2
            decisions: dict[str, str] = {}
            for device in records:
                assignment = deployment.assignments[device.device_id]

                if assignment.state == AssignmentState.PENDING:
                    decisions[device.device_id] = "in_progress"
                elif assignment.state == AssignmentState.FAILED:
                    decisions[device.device_id] = "failed_deploy"
                elif assignment.state == AssignmentState.SUPERSEDED:
                    decisions[device.device_id] = "superseded"
                elif (
                    assignment.previous_version is None
                    or assignment.previous_version == ""
                ):
                    decisions[device.device_id] = "no_previous"
                elif device.active.get(kind_str) != deployment.version:
                    decisions[device.device_id] = "active_mismatch"
                else:
                    decisions[device.device_id] = "restore"

                if decisions[device.device_id] == "restore":
                    # 只有需要真正恢复的设备占用在途窗口
                    device.rollback_in_flight[kind_str] = (
                        rollback_id,
                        deployment_id,
                    )
                audit.log_rollback(
                    rollback_id=rollback_id,
                    device_id=device.device_id,
                    kind=kind_str,
                    decision="planned",
                    reason=f"plan={decisions[device.device_id]}",
                )
        finally:
            for device in records:
                device.lock.release()

        # 阶段 2：模拟回滚下发到设备并生效
        if self.rollback_hook is not None:
            self.rollback_hook(rollback_id)
        self._complete_rollback(
            rollback_id, deployment, records, decisions
        )
        return self._rollback_view(record)

    def _complete_rollback(self, rollback_id, deployment, records, decisions):
        from app.models import RollbackState
        from app.store import RollbackResult

        kind_str = deployment.kind.value
        for device in records:
            device_id = device.device_id
            plan = decisions[device_id]
            assignment = deployment.assignments[device_id]
            result = RollbackResult(device_id=device_id, kind=deployment.kind)

            with device.lock:
                if plan == "restore":
                    previous = assignment.previous_version
                    # 双重检查：窗口期间有效版本未被其他动作改变
                    if device.active.get(kind_str) != deployment.version:
                        result.state = RollbackState.FAILED
                        result.reason = (
                            "active version changed during rollback; preserved"
                        )
                    else:
                        device.active[kind_str] = previous
                        device.previous[kind_str] = deployment.version
                        assignment.state = AssignmentState.SUPERSEDED
                        assignment.reason = "rolled back by operator"
                        result.state = RollbackState.SUCCEEDED
                        result.restored_version = previous
                        result.reason = f"restored previous version {previous}"
                    device.rollback_in_flight.pop(kind_str, None)
                elif plan == "in_progress":
                    result.state = RollbackState.FAILED
                    result.reason = (
                        "deployment still in progress for this device; "
                        "active version preserved"
                    )
                elif plan == "failed_deploy":
                    result.state = RollbackState.FAILED
                    result.reason = (
                        "deployment failed; active version was never changed"
                    )
                elif plan == "superseded":
                    result.state = RollbackState.FAILED
                    result.reason = "assignment was superseded by a newer deployment"
                elif plan == "no_previous":
                    result.state = RollbackState.FAILED
                    result.reason = "no previous version available to restore"
                else:  # active_mismatch
                    result.state = RollbackState.FAILED
                    result.reason = (
                        f"active version is not {deployment.version}; "
                        "refusing to overwrite"
                    )

                deployment_record = self.store.rollbacks[rollback_id]
                deployment_record.results[device_id] = result
                audit.log_rollback(
                    rollback_id=rollback_id,
                    device_id=device_id,
                    kind=kind_str,
                    decision=result.state.value,
                    reason=result.reason,
                )

        with self.store.registry_lock:
            self.store.rollbacks[rollback_id].in_progress = False

    def get_deployment(self, deployment_id: str) -> DeploymentView:
        deployment = self.store.deployments.get(deployment_id)
        if deployment is None:
            from app.errors import DeploymentNotFound

            raise DeploymentNotFound(f"deployment {deployment_id} not found")
        return self._deployment_view(deployment)

    def get_rollback(self, rollback_id: str) -> RollbackView:
        from app.errors import RollbackNotFound

        record = self.store.rollbacks.get(rollback_id)
        if record is None:
            raise RollbackNotFound(f"rollback {rollback_id} not found")
        return self._rollback_view(record)

    def _rollback_view(self, record) -> RollbackView:
        from app.models import RollbackDeviceView

        return RollbackView(
            rollback_id=record.rollback_id,
            deployment_id=record.deployment_id,
            results=[
                RollbackDeviceView(
                    device_id=device_id,
                    kind=result.kind,
                    state=result.state,
                    restored_version=result.restored_version,
                    reason=result.reason,
                )
                for device_id, result in sorted(record.results.items())
            ],
        )

    def list_devices(self) -> list[DeviceView]:
        views: list[DeviceView] = []
        # 快照注册表后逐设备加锁，避免遍历期间注册变更
        with self.store.registry_lock:
            records = list(self.store.devices.values())
        for record in records:
            with record.lock:
                pending = [
                    self._assignment_view(
                        deployment_id=dep_id,
                        device_id=record.device_id,
                        kind_str=kind_str,
                        version=version,
                    )
                    for kind_str, (dep_id, version) in record.pending.items()
                ]
                views.append(
                    DeviceView(
                        device_id=record.device_id,
                        model=record.model,
                        status=self._status_of(record),
                        last_heartbeat_ts=record.last_heartbeat_ts,
                        active_versions=dict(record.active),
                        pending=pending,
                    )
                )
        return views

    def _assignment_view(
        self, *, deployment_id: str, device_id: str, kind_str: str, version: str
    ) -> AssignmentView:
        return AssignmentView(
            deployment_id=deployment_id,
            device_id=device_id,
            kind=ArtifactKind(kind_str),
            version=version,
            state=self.store.deployments[deployment_id]
            .assignments[device_id]
            .state,
        )

    # ---------- 内部助手 ----------

    def _deployment_view(self, deployment) -> DeploymentView:
        from app.models import DeploymentDeviceView, DeploymentState

        targets: list[DeploymentDeviceView] = []
        all_terminal = True
        for device_id in sorted(deployment.assignments):
            assignment = deployment.assignments[device_id]
            record = self.store.devices.get(device_id)
            active = record.active.get(deployment.kind.value) if record else None
            targets.append(
                DeploymentDeviceView(
                    device_id=device_id,
                    state=assignment.state,
                    active_version=active,
                    reason=assignment.reason,
                )
            )
            if assignment.state.value not in ("succeeded", "failed", "superseded"):
                all_terminal = False

        return DeploymentView(
            deployment_id=deployment.deployment_id,
            kind=deployment.kind,
            version=deployment.version,
            state=DeploymentState.COMPLETE if all_terminal else DeploymentState.PENDING,
            targets=targets,
        )

    def _status_of(self, record: DeviceRecord) -> DeviceStatus:
        last = record.last_heartbeat_ts
        if last is None:
            return DeviceStatus.OFFLINE
        return (
            DeviceStatus.ONLINE
            if self.store.clock() - last <= ONLINE_THRESHOLD_SECONDS
            else DeviceStatus.OFFLINE
        )

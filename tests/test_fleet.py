"""并发心跳、冲突下发与回滚失败场景测试。

日志约定：每个关键断言前打印 设备标识 / 上报版本 / 判定依据。
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

from app.models import RejectReason
from app.service import FleetError, FleetService
from app.store import FleetStore

log = logging.getLogger("fleet-test")


@pytest.fixture()
def svc() -> FleetService:
    return FleetService(FleetStore(online_threshold_sec=30.0))


def _register_and_online(svc: FleetService, device_id: str, fw: str = "1.0.0", cfg: str = "c1") -> None:
    svc.register_device(device_id, fw, cfg)
    svc.heartbeat(device_id, fw, cfg, ts=time.time())


# ---------- 心跳 ----------

def test_concurrent_heartbeats_no_duplicate_count(svc: FleetService) -> None:
    """同一设备并发上报同一时间戳：只允许一次计数，其余判 STALE。"""
    svc.register_device("d1", "1.0.0", "c1")
    ts = time.time()
    results: list[str] = []

    def beat() -> None:
        try:
            svc.heartbeat("d1", "1.0.0", "c1", ts=ts)
            results.append("accepted")
        except FleetError as e:
            results.append(e.reason.value)

    threads = [threading.Thread(target=beat) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state = svc.device_state("d1")
    log.info(
        "device=%s reported_version=%s basis=heartbeat_count==1 accepted=%d stale=%d",
        "d1", "1.0.0", results.count("accepted"), results.count("STALE_HEARTBEAT"),
    )
    assert results.count("accepted") == 1
    assert results.count("STALE_HEARTBEAT") == 19
    assert state["heartbeat_count"] == 1
    assert state["last_heartbeat_ts"] == ts


def test_out_of_order_heartbeat_no_state_regression(svc: FleetService) -> None:
    svc.register_device("d1", "1.0.0", "c1")
    now = time.time()
    svc.heartbeat("d1", "1.0.0", "c1", ts=now)
    with pytest.raises(FleetError) as ei:
        svc.heartbeat("d1", "1.0.0", "c1", ts=now - 100)
    state = svc.device_state("d1")
    log.info(
        "device=%s reported_version=%s basis=reason==STALE_HEARTBEAT and last_ts unchanged",
        "d1", "1.0.0",
    )
    assert ei.value.reason is RejectReason.STALE_HEARTBEAT
    assert state["last_heartbeat_ts"] == now
    assert state["heartbeat_count"] == 1
    assert state["online"]


def test_heartbeat_rejections_distinguishable(svc: FleetService) -> None:
    with pytest.raises(FleetError) as e_unknown:
        svc.heartbeat("ghost", "1.0.0", "c1", ts=time.time())
    svc.register_device("d1", "1.0.0", "c1")
    with pytest.raises(FleetError) as e_invalid:
        svc.heartbeat("d1", "", "c1", ts=time.time())
    with pytest.raises(FleetError) as e_invalid_ts:
        svc.heartbeat("d1", "1.0.0", "c1", ts=None)
    log.info(
        "device=%s reported_version=%s basis=reasons %s/%s/%s distinguishable",
        "d1", "1.0.0", e_unknown.value.reason, e_invalid.value.reason, e_invalid_ts.value.reason,
    )
    assert e_unknown.value.reason is RejectReason.UNKNOWN_DEVICE
    assert e_invalid.value.reason is RejectReason.INVALID_PAYLOAD
    assert e_invalid_ts.value.reason is RejectReason.INVALID_PAYLOAD


def test_online_offline_threshold(svc: FleetService) -> None:
    svc.register_device("d1", "1.0.0", "c1")
    svc.heartbeat("d1", "1.0.0", "c1", ts=time.time())
    assert svc.device_state("d1")["online"]
    # 直接老化最后心跳时间，模拟超时离线
    svc.store.devices["d1"].last_heartbeat_ts = time.time() - 3600
    state = svc.device_state("d1")
    log.info("device=%s reported_version=%s basis=last_heartbeat older than threshold", "d1", "1.0.0")
    assert not state["online"]


# ---------- 下发 ----------

def _setup_rollout(svc: FleetService, n: int = 3) -> dict:
    svc.add_firmware_version("2.0.0", {"url": "http://fw/2.0.0"})
    for i in range(n):
        _register_and_online(svc, f"d{i}")
    return svc.create_rollout("firmware", "2.0.0", [f"d{i}" for i in range(n)])


def test_rollout_rejections_distinguishable(svc: FleetService) -> None:
    _register_and_online(svc, "d1")
    svc.add_firmware_version("2.0.0", {})
    with pytest.raises(FleetError) as e_ver:
        svc.create_rollout("firmware", "9.9.9", ["d1"])
    with pytest.raises(FleetError) as e_empty:
        svc.create_rollout("firmware", "2.0.0", [])
    with pytest.raises(FleetError) as e_unknown:
        svc.create_rollout("firmware", "2.0.0", ["ghost"])
    svc.store.devices["d1"].last_heartbeat_ts = time.time() - 3600
    with pytest.raises(FleetError) as e_offline:
        svc.create_rollout("firmware", "2.0.0", ["d1"])
    log.info(
        "device=%s reported_version=%s basis=reasons %s/%s/%s/%s",
        "d1", "2.0.0", e_ver.value.reason, e_empty.value.reason,
        e_unknown.value.reason, e_offline.value.reason,
    )
    assert e_ver.value.reason is RejectReason.VERSION_NOT_FOUND
    assert e_empty.value.reason is RejectReason.EMPTY_TARGETS
    assert e_unknown.value.reason is RejectReason.UNKNOWN_DEVICE
    assert e_offline.value.reason is RejectReason.TARGET_OFFLINE


def test_conflicting_rollout_rejected_per_device(svc: FleetService) -> None:
    """设备已有 PENDING 指令时，第二次下发整批拒绝且首次指令不受影响。"""
    svc.add_firmware_version("2.0.0", {})
    svc.add_config_version("c2", {})
    _register_and_online(svc, "d1")
    _register_and_online(svc, "d2")
    svc.create_rollout("firmware", "2.0.0", ["d1"])
    with pytest.raises(FleetError) as ei:
        svc.create_rollout("config", "c2", ["d1", "d2"])
    log.info(
        "device=%s reported_version=%s basis=reason==CONFLICTING_INSTRUCTION, d2 has no pending",
        "d1", "2.0.0",
    )
    assert ei.value.reason is RejectReason.CONFLICTING_INSTRUCTION
    assert svc.device_state("d1")["pending_instruction"] is not None
    # 整批拒绝：d2 不应被半下发
    assert svc.device_state("d2")["pending_instruction"] is None


def test_concurrent_rollouts_only_one_wins(svc: FleetService) -> None:
    """多线程并发对同一批设备下发：只有一个批次成功。"""
    svc.add_firmware_version("2.0.0", {})
    svc.add_firmware_version("3.0.0", {})
    for i in range(5):
        _register_and_online(svc, f"d{i}")
    targets = [f"d{i}" for i in range(5)]
    outcomes: list[str] = []

    def push(version: str) -> None:
        try:
            svc.create_rollout("firmware", version, targets)
            outcomes.append(f"accepted:{version}")
        except FleetError as e:
            outcomes.append(e.reason.value)

    threads = [threading.Thread(target=push, args=(v,)) for v in ("2.0.0", "3.0.0")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info(
        "device=%s reported_version=%s basis=exactly one rollout accepted, outcomes=%s",
        "d0..d4", "2.0.0|3.0.0", outcomes,
    )
    assert sum(1 for o in outcomes if o.startswith("accepted")) == 1
    assert outcomes.count("CONFLICTING_INSTRUCTION") == 1
    for i in range(5):
        assert svc.device_state(f"d{i}")["pending_instruction"] is not None


# ---------- 回滚 ----------

def test_rollback_partial_success(svc: FleetService) -> None:
    """部分成功时回滚：已应用的回退，失败的保持原版本，原因可区分。"""
    ro = _setup_rollout(svc, n=3)
    ins = {i["device_id"]: i for i in ro["instructions"]}
    svc.report_instruction(ins["d0"]["instruction_id"], True)
    svc.report_instruction(ins["d1"]["instruction_id"], False, reason="flash_write_error")
    # d2 保持 PENDING（下发中途失败/未上报）
    report = svc.rollback_rollout(ro["rollout_id"])
    for device_id, entry in report.items():
        log.info(
            "device=%s reported_version=%s basis=action=%s reason=%s",
            device_id, "2.0.0", entry["action"], entry["reason"],
        )
    assert report["d0"]["action"] == "reverted"
    assert report["d0"]["reason"] == "reverted_to_previous"
    assert report["d1"]["action"] == "no_action"
    assert report["d1"]["reason"] == "flash_write_error"
    assert report["d2"]["action"] == "cancelled"
    # 所有设备回到有效版本 1.0.0，且无悬挂指令
    for i in range(3):
        state = svc.device_state(f"d{i}")
        assert state["firmware_version"] == "1.0.0"
        assert state["pending_instruction"] is None


def test_rollback_idempotent_replay(svc: FleetService) -> None:
    """重复触发回滚：不二次改动设备版本，返回首次报告并标注重放原因。"""
    ro = _setup_rollout(svc, n=2)
    ins = {i["device_id"]: i for i in ro["instructions"]}
    svc.report_instruction(ins["d0"]["instruction_id"], True)
    svc.report_instruction(ins["d1"]["instruction_id"], True)
    first = svc.rollback_rollout(ro["rollout_id"])
    second = svc.rollback_rollout(ro["rollout_id"])
    log.info(
        "device=%s reported_version=%s basis=replay_reason==ROLLOUT_ALREADY_ROLLED_BACK",
        "d0,d1", "2.0.0",
    )
    assert second["replay_reason"] == RejectReason.ROLLOUT_ALREADY_ROLLED_BACK.value
    for i in range(2):
        assert svc.device_state(f"d{i}")["firmware_version"] == "1.0.0"
    assert first["d0"]["firmware_version"] == second["d0"]["firmware_version"]


def test_rollback_unknown_rollout(svc: FleetService) -> None:
    with pytest.raises(FleetError) as ei:
        svc.rollback_rollout("ro-999999")
    log.info("device=%s reported_version=%s basis=reason==ROLLOUT_NOT_FOUND", "-", "-")
    assert ei.value.reason is RejectReason.ROLLOUT_NOT_FOUND


def test_concurrent_rollback_and_report_consistent(svc: FleetService) -> None:
    """回滚与设备上报并发：指令只被终结一次，设备版本最终一致。"""
    ro = _setup_rollout(svc, n=4)
    instructions = ro["instructions"]
    errors: list[Exception] = []

    def do_rollback() -> None:
        try:
            svc.rollback_rollout(ro["rollout_id"])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def do_report(ins_id: str) -> None:
        try:
            svc.report_instruction(ins_id, True)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=do_rollback)]
    threads += [threading.Thread(target=do_report, args=(i["instruction_id"],)) for i in instructions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(4):
        state = svc.device_state(f"d{i}")
        log.info(
            "device=%s reported_version=%s basis=version in {1.0.0,2.0.0} and no pending",
            f"d{i}", state["firmware_version"],
        )
        # 设备要么应用成功停在 2.0.0（上报先于回滚），要么被回滚/取消停在 1.0.0
        assert state["firmware_version"] in {"1.0.0", "2.0.0"}
        assert state["pending_instruction"] is None
    # 回滚后版本为 2.0.0 的设备必须已被回滚报告覆盖（applied 后被 revert）
    report = svc.rollback_rollout(ro["rollout_id"])  # 幂等重放取报告
    for i in range(4):
        state = svc.device_state(f"d{i}")
        if state["firmware_version"] == "2.0.0":
            pytest.fail(f"d{i} 在回滚后不应停留在 2.0.0")
    assert not errors

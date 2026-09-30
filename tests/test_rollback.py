"""执行结果上报与回滚：部分成功、中途失败、重复触发、失败原因可区分。"""
from __future__ import annotations

import logging

from app.models import (
    ArtifactKind,
    DeployRequest,
    RegisterRequest,
    RegisterVersionRequest,
    ResultReport,
)


def _bootstrap(svc, devices=("d1", "d2"), initial="fw-1.0", target="fw-2.0"):
    for d in devices:
        svc.register_device(
            RegisterRequest(
                device_id=d, model="box-A", firmware_version=initial
            )
        )
    svc.register_version(
        RegisterVersionRequest(kind=ArtifactKind.FIRMWARE, version=initial)
    )
    svc.register_version(
        RegisterVersionRequest(kind=ArtifactKind.FIRMWARE, version=target)
    )
    view = svc.deploy(
        DeployRequest(
            kind=ArtifactKind.FIRMWARE,
            version=target,
            target_ids=list(devices),
        )
    )
    return view.deployment_id


def _report(svc, dep, device, success, reason=""):
    return svc.report_result(
        ResultReport(
            device_id=device,
            kind=ArtifactKind.FIRMWARE,
            success=success,
            reason=reason,
        ),
        deployment_id=dep,
    )


def test_partial_success_rollback_restores_only_succeeded(svc, caplog):
    dep = _bootstrap(svc, devices=("d1", "d2"))
    caplog.set_level(logging.INFO, logger="ota.audit")

    assert _report(svc, dep, "d1", True)["applied"] is True
    assert _report(svc, dep, "d2", False, reason="checksum mismatch")["applied"] is False

    devices = {d.device_id: d for d in svc.list_devices()}
    # 失败设备有效版本不被破坏
    assert devices["d2"].active_versions["firmware"] == "fw-1.0"
    assert devices["d1"].active_versions["firmware"] == "fw-2.0"

    view = svc.rollback(dep)
    by_id = {r.device_id: r for r in view.results}

    assert by_id["d1"].state.value == "succeeded"
    assert by_id["d1"].restored_version == "fw-1.0"
    # 下发失败的设备：回滚单明确给出"未改动"原因
    assert by_id["d2"].state.value == "failed"
    assert "was never changed" in by_id["d2"].reason

    devices = {d.device_id: d for d in svc.list_devices()}
    assert devices["d1"].active_versions["firmware"] == "fw-1.0"
    assert devices["d2"].active_versions["firmware"] == "fw-1.0"

    logs = [r.message for r in caplog.records]
    assert any("device_id=d1" in m and "restored" in m for m in logs)
    assert any("device_id=d2" in m and "was never changed" in m for m in logs)


def test_rollback_in_progress_deployment_rejected(svc):
    """下发中途（仍有 PENDING 设备）触发回滚：拒绝且保留有效版本。"""
    dep = _bootstrap(svc, devices=("d1", "d2"))
    _report(svc, dep, "d1", True)
    # d2 仍处于 pending

    view = svc.rollback(dep)
    by_id = {r.device_id: r for r in view.results}
    assert by_id["d1"].state.value == "succeeded"
    assert by_id["d2"].state.value == "failed"
    assert "still in progress" in by_id["d2"].reason

    devices = {d.device_id: d for d in svc.list_devices()}
    assert devices["d2"].active_versions["firmware"] == "fw-1.0"
    # d2 的在途指令仍然保留，可继续上报结果
    assert devices["d2"].pending[0].version == "fw-2.0"


def test_duplicate_rollback_is_idempotent(svc):
    dep = _bootstrap(svc, devices=("d1",))
    _report(svc, dep, "d1", True)
    first = svc.rollback(dep)
    second = svc.rollback(dep)
    assert second.rollback_id == first.rollback_id
    # 有效版本只恢复一次
    assert svc.list_devices()[0].active_versions["firmware"] == "fw-1.0"


def test_duplicate_result_is_idempotent(svc):
    dep = _bootstrap(svc, devices=("d1",))
    r1 = _report(svc, dep, "d1", True)
    r2 = _report(svc, dep, "d1", True)
    assert r1["applied"] is True
    assert r2.get("duplicate") is True
    assert svc.list_devices()[0].active_versions["firmware"] == "fw-2.0"


def test_rollback_without_previous_version_fails_with_reason(svc):
    # 设备注册时无初始版本，下发 fw-2.0 成功后没有可恢复的上一版本
    svc.register_device(RegisterRequest(device_id="d1", model="box-A"))
    svc.register_version(
        RegisterVersionRequest(kind=ArtifactKind.FIRMWARE, version="fw-2.0")
    )
    dep = svc.deploy(
        DeployRequest(
            kind=ArtifactKind.FIRMWARE, version="fw-2.0", target_ids=["d1"]
        )
    ).deployment_id
    _report(svc, dep, "d1", True)

    view = svc.rollback(dep)
    result = view.results[0]
    assert result.state.value == "failed"
    assert "no previous version" in result.reason
    # 有效版本保持 fw-2.0
    assert svc.list_devices()[0].active_versions["firmware"] == "fw-2.0"


def test_failed_deploy_keeps_active_version(svc):
    dep = _bootstrap(svc, devices=("d1",))
    _report(svc, dep, "d1", False, reason="power loss during flash")
    assert svc.list_devices()[0].active_versions["firmware"] == "fw-1.0"

    # 失败后允许重新下发同版本（无在途指令）
    again = svc.deploy(
        DeployRequest(
            kind=ArtifactKind.FIRMWARE, version="fw-2.0", target_ids=["d1"]
        )
    )
    assert all(t.state.value == "pending" for t in again.targets)


def test_mid_flight_failure_preserves_version(svc):
    """下发中途设备上报失败：active 不变，批次视图可看到失败原因。"""
    dep = _bootstrap(svc, devices=("d1",))
    _report(svc, dep, "d1", False, reason="network reset")
    view = svc.get_deployment(dep)
    target = view.targets[0]
    assert target.state.value == "failed"
    assert target.reason == "network reset"
    assert target.active_version == "fw-1.0"


def test_concurrent_duplicate_rollback_triggers_once(svc):
    """并发重复触发同一批次回滚：恰好一个成功，另一个收到明确错误码。"""
    import threading

    from app.errors import RollbackInProgress

    dep = _bootstrap(svc, devices=("d1",))
    _report(svc, dep, "d1", True)

    enter = threading.Barrier(2)
    outcomes: list = []

    def hook(_rollback_id):
        # 先放行另一个线程，让它撞在途窗口
        try:
            enter.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass

    svc.rollback_hook = hook

    def worker():
        try:
            enter.wait()
        except threading.BrokenBarrierError:
            pass
        try:
            view = svc.rollback(dep)
            outcomes.append(("ok", view.rollback_id))
        except RollbackInProgress as exc:
            outcomes.append(("reject", exc.code))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    oks = [o for o in outcomes if o[0] == "ok"]
    rejects = [o for o in outcomes if o[0] == "reject"]
    assert len(oks) == 1, outcomes
    assert rejects == [("reject", "ROLLBACK_IN_PROGRESS")]
    # 有效版本只恢复一次
    assert svc.list_devices()[0].active_versions["firmware"] == "fw-1.0"


def test_deploy_blocked_during_rollback_window(svc):
    """回滚窗口内，对同类型的新下发必须被 TARGET_VERSION_CONFLICT 拒绝。"""
    import threading

    from app.models import DeployRequest as DR

    dep = _bootstrap(svc, devices=("d1",))
    _report(svc, dep, "d1", True)

    checked = threading.Event()

    def hook(_rollback_id):
        # 在窗口内尝试下发新版本
        svc.register_version(
            RegisterVersionRequest(
                kind=ArtifactKind.FIRMWARE, version="fw-3.0"
            )
        )
        from app.errors import TargetVersionConflict

        try:
            svc.deploy(
                DR(
                    kind=ArtifactKind.FIRMWARE,
                    version="fw-3.0",
                    target_ids=["d1"],
                )
            )
        except TargetVersionConflict as exc:
            assert exc.code == "TARGET_VERSION_CONFLICT"
            checked.set()

    svc.rollback_hook = hook
    svc.rollback(dep)
    assert checked.is_set()
    # 回滚完成后窗口释放，可以重新下发
    svc.deploy(
        DR(
            kind=ArtifactKind.FIRMWARE,
            version="fw-3.0",
            target_ids=["d1"],
        )
    )
    assert svc.list_devices()[0].pending[0].version == "fw-3.0"

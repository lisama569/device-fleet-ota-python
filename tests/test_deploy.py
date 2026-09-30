"""分批下发：版本/目标校验、冲突拒绝、灰度分批。"""
from __future__ import annotations

import logging
import threading

from app.models import (
    ArtifactKind,
    DeployRequest,
    RegisterRequest,
    RegisterVersionRequest,
)


def _setup(svc, devices=("d1", "d2"), models=None, versions=("fw-1.0", "fw-2.0")):
    for i, d in enumerate(devices):
        model = models[i] if models else "box-A"
        svc.register_device(RegisterRequest(device_id=d, model=model))
    for v in versions:
        svc.register_version(
            RegisterVersionRequest(kind=ArtifactKind.FIRMWARE, version=v)
        )


def test_deploy_version_not_found(svc):
    _setup(svc)
    from app.errors import VersionNotFound

    try:
        svc.deploy(
            DeployRequest(
                kind=ArtifactKind.FIRMWARE,
                version="fw-9.9",
                target_ids=["d1"],
            )
        )
    except VersionNotFound as exc:
        assert exc.code == "VERSION_NOT_FOUND"
        assert "fw-9.9" in exc.message
    else:
        assert False


def test_deploy_empty_scope_distinguishable(svc):
    _setup(svc)
    from app.errors import EmptyTargetScope, NoMatchingTarget

    # 完全没圈选
    try:
        svc.deploy(
            DeployRequest(kind=ArtifactKind.FIRMWARE, version="fw-2.0")
        )
    except EmptyTargetScope as exc:
        assert exc.code == "EMPTY_TARGET_SCOPE"
    else:
        assert False

    # 圈了但设备不存在 —— 不同错误码
    try:
        svc.deploy(
            DeployRequest(
                kind=ArtifactKind.FIRMWARE,
                version="fw-2.0",
                target_ids=["ghost"],
            )
        )
    except NoMatchingTarget as exc:
        assert exc.code == "NO_MATCHING_TARGET"
    else:
        assert False


def test_deploy_success_creates_pending(svc, caplog):
    _setup(svc)
    caplog.set_level(logging.INFO, logger="ota.audit")
    view = svc.deploy(
        DeployRequest(
            kind=ArtifactKind.FIRMWARE,
            version="fw-2.0",
            target_ids=["d1", "d2"],
        )
    )
    assert view.state.value == "pending"
    assert {t.device_id for t in view.targets} == {"d1", "d2"}
    assert all(t.state.value == "pending" for t in view.targets)

    devices = {d.device_id: d for d in svc.list_devices()}
    assert devices["d1"].pending[0].version == "fw-2.0"

    # 日志含设备标识、版本与判定依据
    logs = [r.message for r in caplog.records]
    assert any("device_id=d1" in m and "fw-2.0" in m for m in logs)


def test_conflicting_deploy_rejected_atomically(svc):
    """同一设备已有不同版本在途时，整批拒绝、无部分指令。"""
    _setup(svc, devices=("d1", "d2", "d3"), versions=("fw-1.0", "fw-2.0", "fw-3.0"))
    svc.deploy(
        DeployRequest(
            kind=ArtifactKind.FIRMWARE, version="fw-2.0", target_ids=["d1"]
        )
    )
    from app.errors import TargetVersionConflict

    try:
        # d2 空闲、d1 冲突 -> 整批必须失败，d2 也不能收到指令
        svc.deploy(
            DeployRequest(
                kind=ArtifactKind.FIRMWARE,
                version="fw-3.0",
                target_ids=["d1", "d2"],
            )
        )
    except TargetVersionConflict as exc:
        assert exc.code == "TARGET_VERSION_CONFLICT"
        assert any("d1" in c for c in exc.conflicts)
    else:
        assert False

    devices = {d.device_id: d for d in svc.list_devices()}
    assert devices["d2"].pending == []
    assert devices["d1"].pending[0].version == "fw-2.0"


def test_redeploy_same_version_is_idempotent(svc):
    """同版本重复下发不视为冲突。"""
    _setup(svc)
    v1 = svc.deploy(
        DeployRequest(
            kind=ArtifactKind.FIRMWARE, version="fw-2.0", target_ids=["d1"]
        )
    )
    v2 = svc.deploy(
        DeployRequest(
            kind=ArtifactKind.FIRMWARE, version="fw-2.0", target_ids=["d1"]
        )
    )
    # 同一在途指令被复用（同一 deployment id）
    assert v2.deployment_id == v1.deployment_id


def test_gray_rollout_by_model_and_batch(svc):
    """灰度：先按机型/名单下发第一批，成功后再下发第二批。"""
    _setup(
        svc,
        devices=("canary-1", "stable-1", "stable-2"),
        models=("canary", "stable", "stable"),
    )
    # 第一批：仅金丝雀机型
    first = svc.deploy(
        DeployRequest(
            kind=ArtifactKind.FIRMWARE,
            version="fw-2.0",
            target_models=["canary"],
        )
    )
    assert [t.device_id for t in first.targets] == ["canary-1"]

    # 稳定机型仍无在途指令
    devices = {d.device_id: d for d in svc.list_devices()}
    assert devices["stable-1"].pending == []


def test_concurrent_conflicting_deploys_only_one_wins(svc):
    """两个不同版本批次并发打向同一设备，只能有一个成功。"""
    _setup(svc, devices=("d1",), versions=("fw-1.0", "fw-2.0", "fw-3.0"))
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker(version: str) -> None:
        barrier.wait()
        try:
            svc.deploy(
                DeployRequest(
                    kind=ArtifactKind.FIRMWARE,
                    version=version,
                    target_ids=["d1"],
                )
            )
            outcomes.append(f"ok:{version}")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"reject:{getattr(exc, 'code', type(exc).__name__)}")

    t1 = threading.Thread(target=worker, args=("fw-2.0",))
    t2 = threading.Thread(target=worker, args=("fw-3.0",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len([o for o in outcomes if o.startswith("ok")]) == 1
    rejected = [o for o in outcomes if o.startswith("reject")][0]
    assert rejected == "reject:TARGET_VERSION_CONFLICT"

    pending = svc.list_devices()[0].pending
    assert len(pending) == 1


def test_firmware_and_config_assignments_coexist(svc):
    svc.register_device(RegisterRequest(device_id="d1", model="box-A"))
    svc.register_version(
        RegisterVersionRequest(kind=ArtifactKind.FIRMWARE, version="fw-2.0")
    )
    svc.register_version(
        RegisterVersionRequest(
            kind=ArtifactKind.CONFIG, version="cfg-9.0"
        )
    )
    fw = svc.deploy(
        DeployRequest(
            kind=ArtifactKind.FIRMWARE, version="fw-2.0", target_ids=["d1"]
        )
    )
    cfg = svc.deploy(
        DeployRequest(
            kind=ArtifactKind.CONFIG, version="cfg-9.0", target_ids=["d1"]
        )
    )
    pending = {p.kind.value: p.version for p in svc.list_devices()[0].pending}
    assert pending == {"firmware": "fw-2.0", "config": "cfg-9.0"}
    assert fw.deployment_id != cfg.deployment_id

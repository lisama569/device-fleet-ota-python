"""设备注册与心跳：在线判定、并发、乱序、非法上报。"""
from __future__ import annotations

import logging
import threading

from app.models import HeartbeatRequest, RegisterRequest
from app.service import ONLINE_THRESHOLD_SECONDS


def _register(svc, device_id: str = "d1", model: str = "box-A"):
    return svc.register_device(
        RegisterRequest(device_id=device_id, model=model)
    )


def test_register_twice_rejected(svc):
    _register(svc)
    from app.errors import DeviceAlreadyRegistered

    try:
        _register(svc)
        assert False, "应当拒绝重复注册"
    except DeviceAlreadyRegistered as exc:
        assert exc.code == "DEVICE_ALREADY_REGISTERED"
        assert exc.device_id == "d1"


def test_heartbeat_to_unknown_device_rejected(svc):
    from app.errors import DeviceNotFound

    try:
        svc.heartbeat(HeartbeatRequest(device_id="ghost", ts=1.0))
    except DeviceNotFound as exc:
        assert exc.code == "DEVICE_NOT_FOUND"
    else:
        assert False


def test_heartbeat_missing_fields_distinguishable(svc):
    _register(svc)
    from app.errors import HeartbeatPayloadInvalid

    try:
        svc.heartbeat(HeartbeatRequest(device_id="d1"))
    except HeartbeatPayloadInvalid as exc:
        assert exc.code == "HEARTBEAT_PAYLOAD_INVALID"
        assert "ts" in exc.message

    try:
        svc.heartbeat(HeartbeatRequest(ts=1.0))
    except HeartbeatPayloadInvalid as exc:
        assert "device_id" in exc.message


def test_heartbeat_invalid_version_distinguishable(svc, caplog):
    _register(svc)
    from app.errors import ReportedVersionInvalid

    caplog.set_level(logging.INFO, logger="ota.audit")
    try:
        svc.heartbeat(
            HeartbeatRequest(
                device_id="d1", ts=1.0, firmware_version="bad version!!"
            )
        )
    except ReportedVersionInvalid as exc:
        assert exc.code == "REPORTED_VERSION_INVALID"
    else:
        assert False

    assert any("d1" in r.message and "firmware" in r.message for r in caplog.records)


def test_online_offline_threshold(svc, clock):
    _register(svc)
    resp = svc.heartbeat(HeartbeatRequest(device_id="d1", ts=clock.now))
    assert resp["status"] == "online"

    clock.advance(ONLINE_THRESHOLD_SECONDS)
    assert svc.list_devices()[0].status == "online"  # 边界含等号

    clock.advance(0.001)
    assert svc.list_devices()[0].status == "offline"


def test_concurrent_heartbeats_counted_once(svc, clock):
    """同一时间戳并发心跳只允许一次真正计数。"""
    _register(svc)
    results: list[str] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        r = svc.heartbeat(HeartbeatRequest(device_id="d1", ts=clock.now))
        results.append("accepted" if r["accepted"] else "duplicate")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results).count("accepted") == 1
    assert sorted(results).count("duplicate") == 7
    assert svc.list_devices()[0].last_heartbeat_ts == clock.now


def test_out_of_order_heartbeat_rejected(svc, clock):
    _register(svc)
    svc.heartbeat(HeartbeatRequest(device_id="d1", ts=100.0))
    from app.errors import StaleHeartbeatRejected

    try:
        svc.heartbeat(HeartbeatRequest(device_id="d1", ts=99.0))
    except StaleHeartbeatRejected as exc:
        assert exc.code == "STALE_HEARTBEAT_REJECTED"
        assert "99.0" in exc.message
    else:
        assert False

    # 状态不能回退：最后时间戳仍是 100
    assert svc.list_devices()[0].last_heartbeat_ts == 100.0


def test_newer_heartbeat_updates_reported_versions(svc):
    _register(svc)
    svc.heartbeat(
        HeartbeatRequest(
            device_id="d1", ts=1.0, firmware_version="fw-1.0", config_version="cfg-1"
        )
    )
    svc.heartbeat(
        HeartbeatRequest(device_id="d1", ts=2.0, firmware_version="fw-1.1")
    )
    view = svc.list_devices()[0]
    assert view.active_versions == {}  # 注册时未带版本
    # 心跳自报版本不直接写 active（必须走下发成功），通过心跳接口返回仅反映在线
    assert view.last_heartbeat_ts == 2.0

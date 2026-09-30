"""HTTP 端到端：错误码可区分、乱序心跳、回滚可追踪。"""
from __future__ import annotations

import threading


def _hb(client, device_id, ts, **extra):
    return client.post(
        "/devices/heartbeat", json={"device_id": device_id, "ts": ts, **extra}
    )


def test_error_codes_over_http(client):
    # 未注册设备
    r = _hb(client, "ghost", 1.0)
    assert r.status_code == 404
    assert r.json()["code"] == "DEVICE_NOT_FOUND"

    client.post("/devices/register", json={"device_id": "d1"})

    # 缺字段
    r = client.post("/devices/heartbeat", json={"device_id": "d1"})
    assert r.status_code == 422
    assert r.json()["code"] == "HEARTBEAT_PAYLOAD_INVALID"

    # 非法版本
    r = _hb(client, "d1", 1.0, firmware_version="bad!!")
    assert r.status_code == 422
    assert r.json()["code"] == "REPORTED_VERSION_INVALID"

    # 乱序
    assert _hb(client, "d1", 10.0).status_code == 200
    r = _hb(client, "d1", 9.0)
    assert r.status_code == 409
    assert r.json()["code"] == "STALE_HEARTBEAT_REJECTED"


def test_deploy_validation_codes(client):
    client.post("/devices/register", json={"device_id": "d1"})

    r = client.post(
        "/deployments",
        json={"kind": "firmware", "version": "nope", "target_ids": ["d1"]},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "VERSION_NOT_FOUND"

    r = client.post(
        "/deployments", json={"kind": "firmware", "version": "nope"}
    )
    assert r.status_code == 422
    assert r.json()["code"] == "EMPTY_TARGET_SCOPE"

    client.post(
        "/versions", json={"kind": "firmware", "version": "v1", "content": "x"}
    )
    r = client.post(
        "/deployments",
        json={"kind": "firmware", "version": "v1", "target_ids": ["ghost"]},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "NO_MATCHING_TARGET"


def test_full_lifecycle_over_http(client):
    client.post(
        "/devices/register",
        json={"device_id": "d1", "firmware_version": "fw-1.0"},
    )
    client.post("/versions", json={"kind": "firmware", "version": "fw-2.0"})

    dep = client.post(
        "/deployments",
        json={"kind": "firmware", "version": "fw-2.0", "target_ids": ["d1"]},
    ).json()
    dep_id = dep["deployment_id"]
    assert dep["targets"][0]["state"] == "pending"

    # 冲突下发
    client.post("/versions", json={"kind": "firmware", "version": "fw-3.0"})
    r = client.post(
        "/deployments",
        json={"kind": "firmware", "version": "fw-3.0", "target_ids": ["d1"]},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "TARGET_VERSION_CONFLICT"

    # 设备上报成功
    r = client.post(
        f"/deployments/{dep_id}/results",
        json={"device_id": "d1", "kind": "firmware", "success": True},
    )
    assert r.json()["active_version"] == "fw-2.0"

    # 回滚
    rb = client.post(f"/deployments/{dep_id}/rollback").json()
    assert rb["results"][0]["state"] == "succeeded"
    assert rb["results"][0]["restored_version"] == "fw-1.0"

    # GET 可追踪
    got = client.get(f"/rollbacks/{rb['rollback_id']}").json()
    assert got["results"][0]["reason"].startswith("restored previous")

    assert client.get("/devices").json()[0]["active_versions"] == {
        "firmware": "fw-1.0"
    }


def test_concurrent_heartbeats_http(client):
    client.post("/devices/register", json={"device_id": "d1"})
    statuses: list[int] = []
    bodies: list[dict] = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        r = _hb(client, "d1", 5.0)
        statuses.append(r.status_code)
        bodies.append(r.json())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(200) == 6
    accepted = [b for b in bodies if b.get("accepted")]
    duplicates = [b for b in bodies if b.get("duplicate")]
    assert len(accepted) == 1
    assert len(duplicates) == 5

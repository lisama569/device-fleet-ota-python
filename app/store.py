"""线程安全的本地内存存储，模拟设备/版本/下发三张表。

- registry_lock 保护"注册表"级别的变更（设备注册、版本注册、建批次）。
- 每个设备一把锁，保护心跳与指令状态机，避免并发心跳重复计数/状态回退。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from app.models import (
    ArtifactKind,
    AssignmentState,
    DeploymentState,
    RollbackState,
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class DeviceRecord:
    device_id: str
    model: str
    last_heartbeat_ts: float | None = None
    # 心跳上报的版本（设备自报）
    reported: dict[str, str] = field(default_factory=dict)
    # 服务端确认的有效版本（指令成功后才更新）
    active: dict[str, str] = field(default_factory=dict)
    # kind -> (deployment_id, version) 在途指令
    pending: dict[str, tuple[str, str]] = field(default_factory=dict)
    # kind -> (rollback_id, deployment_id) 在途回滚
    rollback_in_flight: dict[str, tuple[str, str]] = field(default_factory=dict)
    # kind -> 上一有效版本（仅记录最近一次，用于回滚）
    previous: dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class VersionRecord:
    kind: ArtifactKind
    version: str
    content: str = ""


@dataclass
class AssignmentRecord:
    deployment_id: str
    device_id: str
    kind: ArtifactKind
    version: str
    state: AssignmentState = AssignmentState.PENDING
    reason: str = ""
    previous_version: str | None = None


@dataclass
class DeploymentRecord:
    deployment_id: str
    kind: ArtifactKind
    version: str
    assignments: dict[str, AssignmentRecord]  # device_id -> record
    state: DeploymentState = DeploymentState.PENDING
    created_ts: float = 0.0


@dataclass
class RollbackResult:
    device_id: str
    kind: ArtifactKind
    state: RollbackState = RollbackState.PENDING
    restored_version: str | None = None
    reason: str = ""


@dataclass
class RollbackRecord:
    rollback_id: str
    deployment_id: str
    results: dict[str, RollbackResult] = field(default_factory=dict)
    in_progress: bool = True


class Store:
    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self.registry_lock = threading.RLock()
        self.devices: dict[str, DeviceRecord] = {}
        self.versions: dict[tuple[ArtifactKind, str], VersionRecord] = {}
        self.deployments: dict[str, DeploymentRecord] = {}
        self.rollbacks: dict[str, RollbackRecord] = {}

    # 以下方法在后续步骤逐个实现
    def reset(self) -> None:
        with self.registry_lock:
            self.devices.clear()
            self.versions.clear()
            self.deployments.clear()
            self.rollbacks.clear()

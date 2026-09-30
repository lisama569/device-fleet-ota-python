"""领域模型与 API 请求/响应 schema。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ArtifactKind(str, Enum):
    FIRMWARE = "firmware"
    CONFIG = "config"


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class AssignmentState(str, Enum):
    """单设备指令状态机。"""

    PENDING = "pending"      # 已下发，等待设备执行
    SUCCEEDED = "succeeded"  # 设备上报应用成功，已成为有效版本
    FAILED = "failed"        # 设备上报应用失败
    SUPERSEDED = "superseded"  # 被新指令接管，未真正生效


class DeploymentState(str, Enum):
    PENDING = "pending"   # 批次尚有设备未终态
    COMPLETE = "complete"  # 全部设备到达终态（可能部分失败）


class RollbackState(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# ---------- 设备 ----------

class RegisterRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    model: str = Field(default="default", max_length=128)
    firmware_version: str | None = None
    config_version: str | None = None


class HeartbeatRequest(BaseModel):
    # 允许客户端缺字段，由服务层区分拒绝原因，因此不强制必填
    device_id: str | None = None
    ts: float | None = None
    firmware_version: str | None = None
    config_version: str | None = None


# ---------- 版本仓库 ----------

class RegisterVersionRequest(BaseModel):
    kind: ArtifactKind
    version: str = Field(min_length=1, max_length=128)
    content: str = ""


# ---------- 下发 ----------

class DeployRequest(BaseModel):
    kind: ArtifactKind
    version: str = Field(min_length=1, max_length=128)
    target_ids: list[str] | None = None       # 显式设备列表
    target_models: list[str] | None = None    # 按机型圈选


# ---------- 设备执行结果上报 ----------

class ResultReport(BaseModel):
    """设备上报某次下发的执行结果（模拟设备侧安装结果）。"""

    device_id: str | None = None
    kind: ArtifactKind | None = None
    success: bool | None = None
    reason: str = ""
    ts: float | None = None


# ---------- 响应视图 ----------

class AssignmentView(BaseModel):
    deployment_id: str
    device_id: str
    kind: ArtifactKind
    version: str
    state: AssignmentState
    reason: str = ""


class DeviceView(BaseModel):
    device_id: str
    model: str
    status: DeviceStatus
    last_heartbeat_ts: float | None
    active_versions: dict[str, str]
    pending: list[AssignmentView]


class DeploymentDeviceView(BaseModel):
    device_id: str
    state: AssignmentState
    active_version: str | None
    reason: str = ""


class DeploymentView(BaseModel):
    deployment_id: str
    kind: ArtifactKind
    version: str
    state: DeploymentState
    targets: list[DeploymentDeviceView]


class RollbackDeviceView(BaseModel):
    device_id: str
    kind: ArtifactKind
    state: RollbackState
    restored_version: str | None
    reason: str = ""


class RollbackView(BaseModel):
    rollback_id: str
    deployment_id: str
    results: list[RollbackDeviceView]

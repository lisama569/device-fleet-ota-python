"""业务错误码与统一异常。

每种拒绝原因都有独立 code，便于调用方区分处理，也便于测试断言。
"""
from __future__ import annotations


class OTAError(Exception):
    """所有可预期业务拒绝的基类。"""

    code: str = "INTERNAL_ERROR"
    http_status: int = 400

    def __init__(self, message: str = "", *, device_id: str | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.device_id = device_id

    def to_dict(self) -> dict:
        body: dict = {"code": self.code, "message": self.message}
        if self.device_id is not None:
            body["device_id"] = self.device_id
        return body


class DeviceAlreadyRegistered(OTAError):
    code = "DEVICE_ALREADY_REGISTERED"
    http_status = 409


class DeviceNotFound(OTAError):
    code = "DEVICE_NOT_FOUND"
    http_status = 404


class HeartbeatPayloadInvalid(OTAError):
    """心跳缺少必填字段。"""

    code = "HEARTBEAT_PAYLOAD_INVALID"
    http_status = 422


class ReportedVersionInvalid(OTAError):
    """心跳携带的版本号格式非法。"""

    code = "REPORTED_VERSION_INVALID"
    http_status = 422


class StaleHeartbeatRejected(OTAError):
    """乱序心跳：时间戳早于已接受的最新心跳。"""

    code = "STALE_HEARTBEAT_REJECTED"
    http_status = 409


class VersionNotFound(OTAError):
    """下发引用的固件/配置版本在仓库中不存在。"""

    code = "VERSION_NOT_FOUND"
    http_status = 404


class EmptyTargetScope(OTAError):
    """下发目标范围为空。"""

    code = "EMPTY_TARGET_SCOPE"
    http_status = 422


class NoMatchingTarget(OTAError):
    """目标范围解析后没有任何已注册设备。"""

    code = "NO_MATCHING_TARGET"
    http_status = 404


class TargetVersionConflict(OTAError):
    """目标设备上已有不同版本的同类型指令在途/生效。"""

    code = "TARGET_VERSION_CONFLICT"
    http_status = 409


class RollbackUnavailable(OTAError):
    """设备没有可回滚的上一有效版本。"""

    code = "ROLLBACK_UNAVAILABLE"
    http_status = 409


class RollbackInProgress(OTAError):
    """重复触发：该设备已有回滚在途。"""

    code = "ROLLBACK_IN_PROGRESS"
    http_status = 409


class DeploymentNotFound(OTAError):
    code = "DEPLOYMENT_NOT_FOUND"
    http_status = 404


class RollbackNotFound(OTAError):
    code = "ROLLBACK_NOT_FOUND"
    http_status = 404

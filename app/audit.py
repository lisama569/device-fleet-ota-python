"""审计日志：心跳判定、下发冲突、回滚结果均打印设备标识与判定依据。

测试中可通过 caplog 捕获 logger 名为 "ota.audit" 的记录。
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger("ota.audit")
_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [ota] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    _configured = True


def log_heartbeat(
    *,
    device_id: str,
    ts: float | None,
    reported: dict,
    decision: str,
    reason: str,
) -> None:
    logger.info(
        "heartbeat device_id=%s ts=%s reported_versions=%s decision=%s reason=%s",
        device_id,
        ts,
        dict(reported),
        decision,
        reason,
    )


def log_deploy(
    *,
    deployment_id: str,
    device_id: str,
    kind: str,
    version: str,
    decision: str,
    reason: str,
) -> None:
    logger.info(
        "deploy deployment_id=%s device_id=%s kind=%s version=%s decision=%s reason=%s",
        deployment_id,
        device_id,
        kind,
        version,
        decision,
        reason,
    )


def log_rollback(
    *,
    rollback_id: str,
    device_id: str,
    kind: str,
    decision: str,
    reason: str,
) -> None:
    logger.info(
        "rollback rollback_id=%s device_id=%s kind=%s decision=%s reason=%s",
        rollback_id,
        device_id,
        kind,
        decision,
        reason,
    )

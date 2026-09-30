"""FastAPI 路由：只做参数接收与错误码映射，业务规则在 service 层。"""
from __future__ import annotations

from fastapi import APIRouter

from app.config import service
from app.models import (
    DeployRequest,
    DeploymentView,
    DeviceView,
    HeartbeatRequest,
    RegisterRequest,
    RegisterVersionRequest,
    ResultReport,
    RollbackView,
)

router = APIRouter()


@router.post("/devices/register")
def register_device(req: RegisterRequest) -> dict:
    return service.register_device(req)


@router.post("/devices/heartbeat")
def heartbeat(req: HeartbeatRequest) -> dict:
    return service.heartbeat(req)


@router.post("/versions")
def register_version(req: RegisterVersionRequest) -> dict:
    return service.register_version(req)


@router.post("/deployments")
def create_deployment(req: DeployRequest) -> DeploymentView:
    return service.deploy(req)


@router.post("/deployments/{deployment_id}/results")
def report_result(deployment_id: str, req: ResultReport) -> dict:
    return service.report_result(req, deployment_id=deployment_id)


@router.post("/deployments/{deployment_id}/rollback")
def rollback(deployment_id: str) -> RollbackView:
    return service.rollback(deployment_id)


@router.get("/deployments/{deployment_id}")
def get_deployment(deployment_id: str) -> DeploymentView:
    return service.get_deployment(deployment_id)


@router.get("/rollbacks/{rollback_id}")
def get_rollback(rollback_id: str) -> RollbackView:
    return service.get_rollback(rollback_id)


@router.get("/devices")
def list_devices() -> list[DeviceView]:
    return service.list_devices()

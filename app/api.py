"""FastAPI 路由层：只做参数解析与错误映射，业务在 FleetService。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .service import FleetError, FleetService
from .store import FleetStore


class RegisterReq(BaseModel):
    device_id: str
    firmware_version: str
    config_version: str = ""


class HeartbeatReq(BaseModel):
    firmware_version: str = ""
    config_version: str = ""
    ts: float | None = Field(default=None, description="设备侧单调时间戳")


class VersionReq(BaseModel):
    version: str
    payload: dict = Field(default_factory=dict)


class RolloutReq(BaseModel):
    kind: str = Field(description="firmware 或 config")
    version: str
    targets: list[str] = Field(default_factory=list)


class ReportReq(BaseModel):
    success: bool
    reason: str = ""


def create_app(store: FleetStore | None = None) -> FastAPI:
    app = FastAPI(title="device-fleet-ota")
    service = FleetService(store or FleetStore())
    app.state.service = service

    @app.exception_handler(FleetError)
    async def fleet_error_handler(_: Request, exc: FleetError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"rejected": True, "reason": exc.reason.value, "detail": exc.detail},
        )

    @app.post("/devices/register")
    def register(req: RegisterReq) -> dict:
        return service.register_device(req.device_id, req.firmware_version, req.config_version)

    @app.post("/devices/{device_id}/heartbeat")
    def heartbeat(device_id: str, req: HeartbeatReq) -> dict:
        return service.heartbeat(device_id, req.firmware_version, req.config_version, req.ts)

    @app.get("/devices/{device_id}")
    def device_state(device_id: str) -> dict:
        return service.device_state(device_id)

    @app.post("/firmware")
    def add_firmware(req: VersionReq) -> dict:
        return service.add_firmware_version(req.version, req.payload)

    @app.post("/config")
    def add_config(req: VersionReq) -> dict:
        return service.add_config_version(req.version, req.payload)

    @app.post("/rollouts")
    def create_rollout(req: RolloutReq) -> dict:
        return service.create_rollout(req.kind, req.version, req.targets)

    @app.post("/instructions/{instruction_id}/report")
    def report_instruction(instruction_id: str, req: ReportReq) -> dict:
        return service.report_instruction(instruction_id, req.success, req.reason)

    @app.post("/rollouts/{rollout_id}/rollback")
    def rollback(rollout_id: str) -> dict:
        return service.rollback_rollout(rollout_id)

    return app

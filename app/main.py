"""FastAPI 应用入口。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router
from app.audit import configure_logging
from app.errors import OTAError


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Device Fleet OTA")
    app.include_router(router)

    @app.exception_handler(OTAError)
    async def handle_ota_error(_: Request, exc: OTAError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.get("/")
    def read_root() -> dict:
        return {"service": "device-fleet-ota", "status": "ok"}

    return app


app = create_app()

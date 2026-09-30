"""pytest 公共夹具：每个测试使用干净的 store 与可控时钟。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import app.config as config
from app.main import create_app
from app.service import OTAService
from app.store import Store


class MutableClock:
    """可手动推进的时钟，避免真实 sleep。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def svc(clock: MutableClock):
    store = Store(clock=clock)
    service = OTAService(store)
    # 让 HTTP 路由与被测服务共享同一实例
    config.store = store
    config.service = service
    import app.api as api

    api.service = service
    yield service
    api.service = config.service


@pytest.fixture
def client(svc) -> TestClient:
    return TestClient(create_app())

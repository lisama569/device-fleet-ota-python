"""全局默认实例（单进程内存存储）。"""
from __future__ import annotations

from app.service import OTAService
from app.store import Store

store = Store()
service = OTAService(store)

"""线程安全的本地存储（骨架：仅结构，无实现）。"""
from __future__ import annotations

import threading

from .models import Device, Instruction, Rollout


class FleetStore:
    """内存存储：设备、版本仓库、下发批次，全部操作在锁内完成。"""

    def __init__(self, online_threshold_sec: float = 30.0) -> None:
        self._lock = threading.RLock()
        self.online_threshold_sec = online_threshold_sec
        self.devices: dict[str, Device] = {}
        self.firmware_versions: dict[str, dict] = {}
        self.config_versions: dict[str, dict] = {}
        self.rollouts: dict[str, Rollout] = {}
        self.instructions: dict[str, Instruction] = {}
        self._seq = 0

    def next_id(self, prefix: str) -> str:
        """在锁内生成单调递增 ID，保证并发下唯一。"""
        with self._lock:
            self._seq += 1
            return f"{prefix}-{self._seq:06d}"

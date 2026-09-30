"""领域模型与枚举定义（骨架：仅类型，无实现逻辑）。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class RejectReason(str, Enum):
    """可区分的拒绝原因。"""

    UNKNOWN_DEVICE = "UNKNOWN_DEVICE"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    STALE_HEARTBEAT = "STALE_HEARTBEAT"
    VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
    EMPTY_TARGETS = "EMPTY_TARGETS"
    TARGET_OFFLINE = "TARGET_OFFLINE"
    CONFLICTING_INSTRUCTION = "CONFLICTING_INSTRUCTION"
    ROLLOUT_NOT_FOUND = "ROLLOUT_NOT_FOUND"
    INSTRUCTION_NOT_FOUND = "INSTRUCTION_NOT_FOUND"
    ROLLOUT_ALREADY_ROLLED_BACK = "ROLLOUT_ALREADY_ROLLED_BACK"


class InstructionKind(str, Enum):
    FIRMWARE = "firmware"
    CONFIG = "config"


class InstructionStatus(str, Enum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class RolloutStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass
class Device:
    device_id: str
    firmware_version: str = ""
    config_version: str = ""
    last_heartbeat_ts: float = 0.0
    heartbeat_count: int = 0
    pending_instruction: "Instruction | None" = None

    def is_online(self, now: float, threshold_sec: float) -> bool:
        return (now - self.last_heartbeat_ts) <= threshold_sec


@dataclass
class Instruction:
    instruction_id: str
    rollout_id: str
    device_id: str
    kind: InstructionKind
    target_version: str
    prev_version: str = ""
    status: InstructionStatus = InstructionStatus.PENDING
    reason: str = ""
    created_ts: float = field(default_factory=time.time)


@dataclass
class Rollout:
    rollout_id: str
    kind: InstructionKind
    version: str
    targets: list[str]
    status: RolloutStatus = RolloutStatus.IN_PROGRESS
    instructions: dict[str, Instruction] = field(default_factory=dict)
    rolled_back: bool = False
    rollback_report: dict[str, dict] = field(default_factory=dict)

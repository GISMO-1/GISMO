"""Data models for GISMO."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class FailureType(str, Enum):
    NONE = "NONE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_INPUT = "INVALID_INPUT"
    TOOL_ERROR = "TOOL_ERROR"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class ToolCallStatus(str, Enum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class QueueStatus(str, Enum):
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class Run:
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=_utc_now)
    label: str = ""
    metadata_json: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    run_id: str
    title: str
    description: str
    input_json: Dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid4()))
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    idempotency_key: str = ""
    input_hash: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    output_json: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    failure_type: FailureType = FailureType.NONE
    status_reason: Optional[str] = None

    def mark_running(self) -> None:
        self.status = TaskStatus.RUNNING
        self.failure_type = FailureType.NONE
        self.status_reason = None
        self.updated_at = _utc_now()

    def mark_succeeded(self, output: Dict[str, Any]) -> None:
        self.status = TaskStatus.SUCCEEDED
        self.output_json = output
        self.error = None
        self.failure_type = FailureType.NONE
        self.status_reason = None
        self.updated_at = _utc_now()

    def mark_failed(
        self,
        error: str,
        failure_type: FailureType = FailureType.SYSTEM_ERROR,
        status_reason: Optional[str] = None,
    ) -> None:
        self.status = TaskStatus.FAILED
        self.error = error
        self.failure_type = failure_type
        self.status_reason = status_reason
        self.updated_at = _utc_now()


@dataclass
class ToolCall:
    run_id: str
    task_id: str
    tool_name: str
    input_json: Dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid4()))
    started_at: datetime = field(default_factory=_utc_now)
    finished_at: Optional[datetime] = None
    output_json: Optional[Dict[str, Any]] = None
    status: ToolCallStatus = ToolCallStatus.STARTED
    error: Optional[str] = None
    attempt_number: int = 1
    failure_type: FailureType = FailureType.NONE

    def mark_succeeded(self, output: Dict[str, Any]) -> None:
        self.status = ToolCallStatus.SUCCEEDED
        self.output_json = output
        self.error = None
        self.failure_type = FailureType.NONE
        self.finished_at = _utc_now()

    def mark_failed(self, error: str, failure_type: FailureType = FailureType.SYSTEM_ERROR) -> None:
        self.status = ToolCallStatus.FAILED
        self.error = error
        self.failure_type = failure_type
        self.finished_at = _utc_now()


@dataclass
class QueueItem:
    command_text: str
    run_id: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid4()))
    status: QueueStatus = QueueStatus.QUEUED
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    attempt_count: int = 0
    max_retries: int = 3
    next_attempt_at: Optional[datetime] = None
    timeout_seconds: int = 300
    cancel_requested: bool = False
    last_error: Optional[str] = None


@dataclass
class DaemonHeartbeat:
    pid: int
    started_at: datetime
    last_seen: datetime
    version: Optional[str] = None

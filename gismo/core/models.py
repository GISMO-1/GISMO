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


class ToolCallStatus(str, Enum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


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
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    output_json: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def mark_running(self) -> None:
        self.status = TaskStatus.RUNNING
        self.updated_at = _utc_now()

    def mark_succeeded(self, output: Dict[str, Any]) -> None:
        self.status = TaskStatus.SUCCEEDED
        self.output_json = output
        self.error = None
        self.updated_at = _utc_now()

    def mark_failed(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.error = error
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

    def mark_succeeded(self, output: Dict[str, Any]) -> None:
        self.status = ToolCallStatus.SUCCEEDED
        self.output_json = output
        self.error = None
        self.finished_at = _utc_now()

    def mark_failed(self, error: str) -> None:
        self.status = ToolCallStatus.FAILED
        self.error = error
        self.finished_at = _utc_now()

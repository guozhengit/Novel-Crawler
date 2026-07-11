from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class TaskStatus(StrEnum):
    CREATED = "created"
    PROBING = "probing"
    WAITING_FOR_USER = "waiting_for_user"
    VALIDATING = "validating"
    READY = "ready"
    CRAWLING = "crawling"
    COMPLETED = "completed"
    PAUSED = "paused"
    RECOVERABLE_FAILED = "recoverable_failed"
    TERMINAL_FAILED = "terminal_failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.TERMINAL_FAILED, TaskStatus.CANCELLED}
)


_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.PROBING, TaskStatus.CANCELLED}),
    TaskStatus.PROBING: frozenset(
        {
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.VALIDATING,
            TaskStatus.RECOVERABLE_FAILED,
            TaskStatus.PAUSED,
            TaskStatus.TERMINAL_FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_FOR_USER: frozenset(
        {
            TaskStatus.VALIDATING,
            TaskStatus.RECOVERABLE_FAILED,
            TaskStatus.TERMINAL_FAILED,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.VALIDATING: frozenset(
        {
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.READY,
            TaskStatus.RECOVERABLE_FAILED,
            TaskStatus.PAUSED,
            TaskStatus.TERMINAL_FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.READY: frozenset(
        {TaskStatus.CRAWLING, TaskStatus.PAUSED, TaskStatus.TERMINAL_FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.CRAWLING: frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.PAUSED,
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.RECOVERABLE_FAILED,
            TaskStatus.TERMINAL_FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.PAUSED: frozenset(
        {
            TaskStatus.PROBING,
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.VALIDATING,
            TaskStatus.READY,
            TaskStatus.CRAWLING,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RECOVERABLE_FAILED: frozenset(
        {
            TaskStatus.PROBING,
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.VALIDATING,
            TaskStatus.READY,
            TaskStatus.CRAWLING,
            TaskStatus.TERMINAL_FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.TERMINAL_FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

ALLOWED_TRANSITIONS = MappingProxyType(_TRANSITIONS)


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    status: TaskStatus
    version: int
    created_at: str
    updated_at: str
    error_code: str | None = None
    resume_status: TaskStatus | None = None
    source_url: str = field(default="", repr=False)
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    error_message: str | None = field(default=None, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_safe_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_code": self.error_code,
            "resume_status": self.resume_status.value if self.resume_status is not None else None,
            "is_terminal": self.is_terminal,
        }


@dataclass(frozen=True)
class TaskEvent:
    event_id: int
    task_id: str
    from_status: TaskStatus | None
    to_status: TaskStatus
    task_version: int
    created_at: str
    error_code: str | None = None
    reason: str | None = field(default=None, repr=False)
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    error_message: str | None = field(default=None, repr=False)

    def to_safe_dict(self) -> dict[str, str | int | None]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "from_status": self.from_status.value if self.from_status is not None else None,
            "to_status": self.to_status.value,
            "task_version": self.task_version,
            "created_at": self.created_at,
            "error_code": self.error_code,
        }

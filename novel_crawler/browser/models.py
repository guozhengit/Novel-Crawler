"""Public, privacy-safe models for interactive browser verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from novel_crawler.acquisition.models import AcquiredPage


class VerificationStatus(StrEnum):
    WAITING = "waiting"
    READY = "ready"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@dataclass(frozen=True)
class VerificationTicket:
    """Safe handle for an active manual verification window."""

    token: str = field(repr=False)
    status: VerificationStatus = VerificationStatus.WAITING
    safe_origin: str = "<invalid-url>"
    expires_at: datetime | None = None
    attempt: int = 0


@dataclass(frozen=True)
class VerificationOutcome:
    """Result of continuing or cancelling a verification."""

    status: VerificationStatus
    safe_origin: str
    attempt: int = 0
    page: AcquiredPage | None = field(default=None, repr=False)


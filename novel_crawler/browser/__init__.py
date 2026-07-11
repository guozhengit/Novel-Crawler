"""Persistent, domain-isolated browser sessions."""

from .coordinator import BrowserAcquirer, VerificationCoordinator, VerificationRequired
from .driver import BrowserPageSnapshot, BrowserRequestPolicy, DefaultPlaywrightDriver, Driver, RequestDecision
from .models import VerificationOutcome, VerificationStatus, VerificationTicket
from .sessions import (
    BrowserSessionError,
    BrowserSessionInfo,
    BrowserSessionLease,
    BrowserSessionStatus,
    BrowserSessionStore,
    SessionConfirmationError,
    SessionConflictError,
    SessionLimitError,
    SessionLockTimeout,
)

__all__ = [
    "BrowserAcquirer",
    "BrowserPageSnapshot",
    "BrowserRequestPolicy",
    "BrowserSessionError",
    "BrowserSessionInfo",
    "BrowserSessionLease",
    "BrowserSessionStatus",
    "BrowserSessionStore",
    "DefaultPlaywrightDriver",
    "Driver",
    "RequestDecision",
    "SessionConfirmationError",
    "SessionConflictError",
    "SessionLimitError",
    "SessionLockTimeout",
    "VerificationCoordinator",
    "VerificationOutcome",
    "VerificationRequired",
    "VerificationStatus",
    "VerificationTicket",
]

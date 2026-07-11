"""Persistent, domain-isolated browser sessions."""

from .coordinator import BrowserAcquirer, BrowserCleanupRequired, VerificationCoordinator, VerificationRequired
from .driver import (
    BrowserContextWorker,
    BrowserPageSnapshot,
    BrowserRequestPolicy,
    DefaultPlaywrightDriver,
    Driver,
    DriverLaunchFailure,
    RequestDecision,
)
from .models import VerificationOutcome, VerificationStatus, VerificationTicket
from .proxy import PinnedSocksProxy, ProxyError
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
    "BrowserCleanupRequired",
    "BrowserContextWorker",
    "BrowserPageSnapshot",
    "BrowserRequestPolicy",
    "BrowserSessionError",
    "BrowserSessionInfo",
    "BrowserSessionLease",
    "BrowserSessionStatus",
    "BrowserSessionStore",
    "DefaultPlaywrightDriver",
    "Driver",
    "DriverLaunchFailure",
    "PinnedSocksProxy",
    "ProxyError",
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

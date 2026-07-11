"""Dependency-neutral verification signal shared by acquisition and adaptation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from novel_crawler.acquisition.http import AcquisitionError
from novel_crawler.acquisition.security import redact_url

if TYPE_CHECKING:
    from novel_crawler.browser.models import VerificationTicket


class VerificationRequired(RuntimeError):
    """Safe, actionable signal that a human browser step is required."""

    def __init__(
        self,
        code: str = "verification_required",
        ticket: VerificationTicket | None = None,
        *,
        original_url: str | None = None,
        safe_origin: str | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("verification error code is invalid")
        self.code = code
        self.ticket = ticket
        self.original_url = original_url
        self.safe_origin = safe_origin or (redact_url(original_url) if original_url is not None else "<invalid-url>")
        super().__init__(code)

    def __repr__(self) -> str:
        return f"VerificationRequired(code={self.code!r}, safe_origin={self.safe_origin!r}, ticket_present={self.ticket is not None!r})"


class BrowserCleanupRequired(AcquisitionError):
    """A headless context remains quarantined until its private token is retried."""

    def __init__(self, token: str, safe_origin: str) -> None:
        self.token = token
        super().__init__("browser_cleanup_failed", safe_origin, False)

    def __repr__(self) -> str:
        return "BrowserCleanupRequired(code='browser_cleanup_failed', token='<redacted>')"


__all__ = ["BrowserCleanupRequired", "VerificationRequired"]

"""HTTP-first browser fallback and bounded manual-verification coordination."""

from __future__ import annotations

import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

from novel_crawler.acquisition.classifier import PageClassifier, PageKind
from novel_crawler.acquisition.http import AcquisitionError, HttpPageAcquirer
from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.acquisition.security import UrlSafetyPolicy, redact_url
from novel_crawler.core.domains import canonical_domain

from .driver import BrowserContext, BrowserRequestPolicy, DefaultPlaywrightDriver, Driver
from .models import VerificationOutcome, VerificationStatus, VerificationTicket
from .sessions import BrowserSessionLease, BrowserSessionStore


class HttpAcquirer(Protocol):
    def fetch_page(self, url: str, **kwargs: object) -> AcquiredPage: ...


class VerificationRequired(RuntimeError):
    """Safe, actionable signal that a human browser step is required."""

    def __init__(self, code: str = "verification_required", ticket: VerificationTicket | None = None) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("verification error code is invalid")
        self.code = code
        self.ticket = ticket
        super().__init__(code)


@dataclass
class _ActiveVerification:
    token: str
    original_url: str = field(repr=False)
    safe_origin: str
    expires_at: datetime
    lease: BrowserSessionLease = field(repr=False)
    context: BrowserContext = field(repr=False)
    policy: BrowserRequestPolicy = field(repr=False)
    attempt: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _domain(url: str) -> str:
    host = urlsplit(url).hostname
    if host is None:
        raise VerificationRequired("verification_url_invalid")
    return canonical_domain(host)


class VerificationCoordinator:
    def __init__(
        self,
        sessions: BrowserSessionStore,
        *,
        driver: Driver | None = None,
        classifier: PageClassifier | None = None,
        safety_policy: UrlSafetyPolicy | None = None,
        ttl: timedelta = timedelta(minutes=10),
        max_active: int = 8,
        max_attempts: int = 2,
        clock: Callable[[], datetime] | None = None,
        allow_public_cdn_subresources: bool = False,
    ) -> None:
        if ttl <= timedelta(0) or max_active <= 0 or max_attempts <= 0:
            raise ValueError("verification limits must be positive")
        self.sessions = sessions
        self.driver = driver or DefaultPlaywrightDriver()
        self.classifier = classifier or PageClassifier()
        self.safety_policy = safety_policy or UrlSafetyPolicy()
        self.ttl = min(ttl, timedelta(minutes=10))
        self.max_active = max_active
        self.max_attempts = min(max_attempts, 2)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.allow_public_cdn_subresources = allow_public_cdn_subresources
        self._active: dict[str, _ActiveVerification] = {}
        self._guard = threading.Lock()

    def begin(self, url: str, *, task_key: str = "default") -> VerificationTicket:
        del task_key  # attempts are scoped to this active origin/task verification
        self.expire_sweep()
        with self._guard:
            if len(self._active) >= self.max_active:
                raise VerificationRequired("verification_capacity")
        policy = BrowserRequestPolicy(
            self.safety_policy,
            allow_public_cdn_subresources=self.allow_public_cdn_subresources,
        )
        try:
            domain = _domain(url)
            policy.lock(url)
            lease = self.sessions.acquire(domain)
        except VerificationRequired:
            raise
        except Exception:
            raise VerificationRequired("verification_start_failed") from None
        context: BrowserContext | None = None
        try:
            context = self.driver.launch(user_data_dir=lease.profile_path, headless=False, policy=policy)
            context.navigate(url)
            token = secrets.token_urlsafe(32)
            expires_at = self.clock() + self.ttl
            active = _ActiveVerification(token, url, redact_url(url), expires_at, lease, context, policy)
            with self._guard:
                if len(self._active) >= self.max_active:
                    raise VerificationRequired("verification_capacity")
                self._active[token] = active
            return VerificationTicket(token, VerificationStatus.WAITING, active.safe_origin, expires_at, 0)
        except VerificationRequired:
            if context is not None:
                self._safe_close_context(context)
            self._safe_close_lease(lease)
            raise
        except Exception:
            if context is not None:
                self._safe_close_context(context)
            self._safe_close_lease(lease)
            raise VerificationRequired("verification_start_failed") from None

    def continue_verification(self, token: str) -> VerificationOutcome:
        active = self._lookup(token)
        if not active.lock.acquire(blocking=False):
            raise VerificationRequired("verification_in_progress")
        try:
            with self._guard:
                if self._active.get(token) is not active:
                    raise VerificationRequired("verification_token_invalid")
            if self.clock() >= active.expires_at:
                return self._finish(active, VerificationStatus.TIMED_OUT)
            try:
                current = active.context.capture().to_acquired_page()
                classification = self.classifier.classify(current.snapshot)
                if classification.kind is PageKind.AUTH_OR_CHALLENGE:
                    active.attempt += 1
                    if active.attempt >= self.max_attempts:
                        return self._finish(active, VerificationStatus.FAILED)
                    return VerificationOutcome(VerificationStatus.WAITING, active.safe_origin, active.attempt)
                reloaded = active.context.navigate(active.original_url).to_acquired_page()
                if self.classifier.classify(reloaded.snapshot).kind is PageKind.AUTH_OR_CHALLENGE:
                    active.attempt += 1
                    if active.attempt >= self.max_attempts:
                        return self._finish(active, VerificationStatus.FAILED)
                    return VerificationOutcome(VerificationStatus.WAITING, active.safe_origin, active.attempt)
                return self._finish(active, VerificationStatus.COMPLETED, reloaded)
            except Exception:
                return self._finish(active, VerificationStatus.FAILED)
        finally:
            active.lock.release()

    def cancel(self, token: str) -> VerificationOutcome:
        active = self._lookup(token)
        with active.lock:
            with self._guard:
                if self._active.get(token) is not active:
                    raise VerificationRequired("verification_token_invalid")
            return self._finish(active, VerificationStatus.CANCELLED)

    def expire_sweep(self) -> int:
        now = self.clock()
        with self._guard:
            candidates = [active for active in self._active.values() if now >= active.expires_at]
        expired = 0
        for active in candidates:
            if active.lock.acquire(blocking=False):
                try:
                    with self._guard:
                        if self._active.get(active.token) is active:
                            self._active.pop(active.token, None)
                            expired += 1
                        else:
                            continue
                    self._close(active)
                finally:
                    active.lock.release()
        return expired

    def _lookup(self, token: str) -> _ActiveVerification:
        if not isinstance(token, str) or len(token) > 128:
            raise VerificationRequired("verification_token_invalid")
        with self._guard:
            active = self._active.get(token)
        if active is None:
            raise VerificationRequired("verification_token_invalid")
        return active

    def _finish(
        self,
        active: _ActiveVerification,
        status: VerificationStatus,
        page: AcquiredPage | None = None,
    ) -> VerificationOutcome:
        with self._guard:
            self._active.pop(active.token, None)
        self._close(active)
        return VerificationOutcome(status, active.safe_origin, active.attempt, page)

    def _close(self, active: _ActiveVerification) -> None:
        self._safe_close_context(active.context)
        self._safe_close_lease(active.lease)

    @staticmethod
    def _safe_close_context(context: BrowserContext) -> None:
        try:
            context.close()
        except Exception:
            pass

    @staticmethod
    def _safe_close_lease(lease: BrowserSessionLease) -> None:
        try:
            lease.close()
        except Exception:
            pass


class BrowserAcquirer:
    """Acquire with HTTP first, then a headless browser only for unknown shells."""

    def __init__(
        self,
        *,
        http: HttpAcquirer | None = None,
        classifier: PageClassifier | None = None,
        driver: Driver | None = None,
        sessions: BrowserSessionStore,
        coordinator: VerificationCoordinator | None = None,
        safety_policy: UrlSafetyPolicy | None = None,
        max_body_bytes: int = 10 * 1024 * 1024,
        allow_public_cdn_subresources: bool = False,
    ) -> None:
        self.http = http or HttpPageAcquirer()
        self.classifier = classifier or PageClassifier()
        self.driver = driver or DefaultPlaywrightDriver()
        self.sessions = sessions
        self.coordinator = coordinator
        self.safety_policy = safety_policy or UrlSafetyPolicy()
        self.max_body_bytes = min(max_body_bytes, 10 * 1024 * 1024)
        self.allow_public_cdn_subresources = allow_public_cdn_subresources

    def fetch_page(self, url: str) -> AcquiredPage:
        page = self.http.fetch_page(url)
        kind = self.classifier.classify(page.snapshot).kind
        if kind is PageKind.AUTH_OR_CHALLENGE:
            self._require_verification(url)
        if kind is not PageKind.UNKNOWN:
            return page
        policy = BrowserRequestPolicy(
            self.safety_policy,
            allow_public_cdn_subresources=self.allow_public_cdn_subresources,
        )
        lease: BrowserSessionLease | None = None
        context: BrowserContext | None = None
        try:
            policy.lock(url)
            lease = self.sessions.acquire(_domain(url))
            context = self.driver.launch(user_data_dir=lease.profile_path, headless=True, policy=policy)
            browser_page = context.navigate(url).to_acquired_page(max_body_bytes=self.max_body_bytes)
            if self.classifier.classify(browser_page.snapshot).kind is PageKind.AUTH_OR_CHALLENGE:
                self._safe_close(context, lease)
                context = None
                lease = None
                self._require_verification(url)
            return browser_page
        except VerificationRequired:
            raise
        except Exception:
            raise AcquisitionError("browser_failed", redact_url(url), True) from None
        finally:
            self._safe_close(context, lease)

    def fetch(self, url: str) -> PageSnapshot:
        return self.fetch_page(url).snapshot

    def _require_verification(self, url: str) -> None:
        ticket = self.coordinator.begin(url) if self.coordinator is not None else None
        raise VerificationRequired(ticket=ticket)

    @staticmethod
    def _safe_close(context: BrowserContext | None, lease: BrowserSessionLease | None) -> None:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if lease is not None:
            try:
                lease.close()
            except Exception:
                pass

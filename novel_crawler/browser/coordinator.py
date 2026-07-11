"""HTTP-first browser fallback and bounded manual-verification coordination."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
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

from .driver import BrowserContextWorker, BrowserRequestPolicy, DefaultPlaywrightDriver, Driver
from .models import VerificationOutcome, VerificationStatus, VerificationTicket
from .sessions import BrowserSessionLease, BrowserSessionStore, _DomainLock

_TASK_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_LEDGER_LIMIT = 64 * 1024


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
    domain: str
    ledger_key: str = field(repr=False)
    lease: BrowserSessionLease = field(repr=False)
    worker: BrowserContextWorker = field(repr=False)
    attempt: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _domain(url: str) -> str:
    host = urlsplit(url).hostname
    if host is None:
        raise VerificationRequired("verification_url_invalid")
    return canonical_domain(host)


class _AttemptLedger:
    def __init__(self, sessions: BrowserSessionStore, clock: Callable[[], datetime], ttl: timedelta) -> None:
        self._io = sessions._io
        self._clock = clock
        self._ttl = ttl
        self._key_path = sessions.root / "verification-attempts.key"
        self._ledger_path = sessions.root / "verification-attempts.json"
        self._lock_path = sessions.root / "locks" / "allocation.lock"
        self._lock_timeout = sessions._lock_timeout
        lock = _DomainLock(self._lock_path, self._lock_timeout, self._io)
        try:
            lock.acquire()
            if self._key_path.exists():
                self._secret = self._io.read_bounded(self._key_path, 64)
            else:
                self._secret = os.urandom(32)
                self._io.atomic_write(self._key_path, self._secret)
            if len(self._secret) != 32:  # pragma: no cover - persistent storage tamper defense
                raise ValueError  # pragma: no cover
            self._values = self._read()
        except Exception:  # pragma: no cover - injected storage backends cover I/O safety elsewhere
            raise VerificationRequired("verification_ledger_failed") from None  # pragma: no cover
        finally:
            lock.release()

    def opaque_key(self, safe_origin: str, task_key: str) -> str:
        return hmac.new(self._secret, f"{safe_origin}|{task_key}".encode(), hashlib.sha256).hexdigest()

    def reserve(self, key: str, maximum: int) -> None:
        lock = self._acquire()
        try:
            self._values = self._read()
            now = self._clock()
            current = self._values.get(key)
            count = 0
            if current is not None and datetime.fromisoformat(str(current["expires_at"])) > now:
                stored_count = current["count"]
                if not isinstance(stored_count, int):  # pragma: no cover - state is schema-validated on read
                    raise VerificationRequired("verification_ledger_failed")  # pragma: no cover
                count = stored_count
            if count >= maximum:
                raise VerificationRequired("verification_attempts_exhausted")
            self._values[key] = {"count": count + 1, "expires_at": (now + self._ttl).isoformat()}
            self._write()
        finally:
            lock.release()

    def rollback(self, key: str) -> None:
        lock = self._acquire()
        try:
            self._values = self._read()
            current = self._values.get(key)
            if current is None:  # pragma: no cover - defensive idempotence
                return  # pragma: no cover
            stored_count = current["count"]
            if not isinstance(stored_count, int):  # pragma: no cover - state is schema-validated on read
                raise VerificationRequired("verification_ledger_failed")  # pragma: no cover
            count = stored_count - 1
            if count <= 0:
                self._values.pop(key, None)
            else:
                current["count"] = count
            self._write()
        finally:
            lock.release()

    def clear(self, key: str) -> None:
        lock = self._acquire()
        try:
            self._values = self._read()
            if self._values.pop(key, None) is not None:
                self._write()
        finally:
            lock.release()

    def _acquire(self) -> _DomainLock:
        lock = _DomainLock(self._lock_path, self._lock_timeout, self._io)
        try:
            lock.acquire()
            return lock
        except Exception:
            raise VerificationRequired("verification_ledger_failed") from None

    def _read(self) -> dict[str, dict[str, object]]:
        if not self._ledger_path.exists():
            return {}
        raw = json.loads(self._io.read_bounded(self._ledger_path, _LEDGER_LIMIT))
        if not isinstance(raw, dict) or any(not re.fullmatch(r"[0-9a-f]{64}", str(key)) for key in raw):
            raise ValueError  # pragma: no cover - persistent storage tamper defense
        values: dict[str, dict[str, object]] = {}
        for key, item in raw.items():
            if not isinstance(item, dict) or set(item) != {"count", "expires_at"}:
                raise ValueError  # pragma: no cover - persistent storage tamper defense
            count = item["count"]
            expires = item["expires_at"]
            if not isinstance(count, int) or not 1 <= count <= 2 or not isinstance(expires, str):
                raise ValueError  # pragma: no cover - persistent storage tamper defense
            datetime.fromisoformat(expires)
            values[str(key)] = {"count": count, "expires_at": expires}
        return values

    def _write(self) -> None:
        try:
            payload = json.dumps(self._values, sort_keys=True, separators=(",", ":")).encode("ascii")
            if len(payload) > _LEDGER_LIMIT:  # pragma: no cover - capacity prevents practical growth
                raise ValueError  # pragma: no cover
            self._io.atomic_write(self._ledger_path, payload)
        except Exception:  # pragma: no cover - RegistryIO fault injection is covered in its suite
            raise VerificationRequired("verification_ledger_failed") from None  # pragma: no cover


class VerificationCoordinator:
    """Coordinate visible verification. Cancellation consumes one ledger attempt."""

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
        attempt_ttl: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] | None = None,
        max_body_bytes: int = 10 * 1024 * 1024,
        max_network_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if ttl <= timedelta(0) or attempt_ttl <= timedelta(0) or max_active <= 0 or max_attempts <= 0:
            raise ValueError("verification limits must be positive")
        if max_body_bytes <= 0 or max_network_bytes <= 0:
            raise ValueError("verification limits must be positive")
        self.sessions = sessions
        self.driver = driver or DefaultPlaywrightDriver(
            max_body_bytes=max_body_bytes,
            max_network_bytes=max_network_bytes,
            operation_timeout=min(ttl, timedelta(minutes=10)).total_seconds(),
        )
        self.classifier = classifier or PageClassifier()
        self.safety_policy = safety_policy or UrlSafetyPolicy()
        self.ttl = min(ttl, timedelta(minutes=10))
        self.max_active = max_active
        self.max_attempts = min(max_attempts, 2)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._active: dict[str, _ActiveVerification] = {}
        self._failed_closures: dict[str, _ActiveVerification] = {}
        self._reserved = 0
        self._guard = threading.Lock()
        self._ledger = _AttemptLedger(sessions, self.clock, attempt_ttl)

    def begin(self, url: str, *, task_key: str) -> VerificationTicket:
        if not isinstance(task_key, str) or not _TASK_KEY.fullmatch(task_key):
            raise VerificationRequired("verification_task_invalid")
        domain = _domain(url)
        policy = BrowserRequestPolicy(self.safety_policy)
        try:
            policy.lock(url)
        except Exception:
            raise VerificationRequired("verification_start_failed") from None
        safe_origin = redact_url(url)
        ledger_key = self._ledger.opaque_key(safe_origin, task_key)
        with self._guard:
            if len(self._active) + len(self._failed_closures) + self._reserved >= self.max_active:
                raise VerificationRequired("verification_capacity")
            self._reserved += 1
            try:
                self._ledger.reserve(ledger_key, self.max_attempts)
            except Exception:
                self._reserved -= 1
                raise
        lease: BrowserSessionLease | None = None
        worker: BrowserContextWorker | None = None
        token = secrets.token_urlsafe(32)
        expires_at = self.clock() + self.ttl
        try:
            lease = self.sessions.acquire(domain)
            worker = BrowserContextWorker(
                self.driver,
                user_data_dir=lease.profile_path,
                headless=False,
                policy=policy,
                ttl=self.ttl.total_seconds(),
            )
            worker.start()
            worker.navigate(url)
            active = _ActiveVerification(token, url, safe_origin, expires_at, domain, ledger_key, lease, worker)
            with self._guard:
                self._reserved -= 1
                self._active[token] = active
            return VerificationTicket(token, VerificationStatus.WAITING, safe_origin, expires_at, 0)
        except Exception:
            close_confirmed = worker is None
            if worker is not None:
                try:
                    worker.close()
                    close_confirmed = True
                except Exception:
                    close_confirmed = False
            if lease is not None and close_confirmed:
                try:
                    lease.close()
                except Exception:  # pragma: no cover - best-effort rollback after failed startup
                    pass  # pragma: no cover
            with self._guard:
                self._reserved -= 1
                self._ledger.rollback(ledger_key)
                if not close_confirmed and lease is not None and worker is not None:
                    try:
                        lease.mark_stale()
                    except Exception:  # pragma: no cover - quarantine still retains the exclusive lease
                        pass  # pragma: no cover
                    failed = _ActiveVerification(
                        token, url, safe_origin, expires_at, domain, ledger_key, lease, worker
                    )
                    self._failed_closures[token] = failed
                    ticket = VerificationTicket(token, VerificationStatus.FAILED, safe_origin, expires_at, 0)
                    raise VerificationRequired("verification_start_failed", ticket) from None
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
                current = active.worker.capture().to_acquired_page()
                if self.clock() >= active.expires_at:
                    return self._finish(active, VerificationStatus.TIMED_OUT)
                if self.classifier.classify(current.snapshot).kind is PageKind.AUTH_OR_CHALLENGE:
                    active.attempt += 1
                    if active.attempt >= self.max_attempts:
                        return self._finish(active, VerificationStatus.FAILED)
                    return VerificationOutcome(VerificationStatus.WAITING, active.safe_origin, active.attempt)
                reloaded = active.worker.navigate(active.original_url).to_acquired_page()
                if self.clock() >= active.expires_at:
                    return self._finish(active, VerificationStatus.TIMED_OUT)
                if self.classifier.classify(reloaded.snapshot).kind is PageKind.AUTH_OR_CHALLENGE:
                    active.attempt += 1
                    if active.attempt >= self.max_attempts:
                        return self._finish(active, VerificationStatus.FAILED)
                    return VerificationOutcome(VerificationStatus.WAITING, active.safe_origin, active.attempt)
                return self._finish(active, VerificationStatus.COMPLETED, reloaded)
            except Exception:
                return self._handle_worker_failure(active)
        finally:
            active.lock.release()

    def cancel(self, token: str) -> VerificationOutcome:
        active = self._lookup(token)
        with active.lock:
            return self._finish(active, VerificationStatus.CANCELLED)

    def retry_cleanup(self, token: str) -> bool:
        with self._guard:
            active = self._failed_closures.get(token)
        if active is None:
            raise VerificationRequired("verification_token_invalid")
        with active.lock:
            try:
                active.worker.close()
            except Exception:  # pragma: no cover - BrowserSessionStore fault injection owns this branch
                return False  # pragma: no cover
            try:
                active.lease.close()
            except Exception:
                return False
            with self._guard:
                self._failed_closures.pop(token, None)
            return True

    def expire_sweep(self) -> int:
        now = self.clock()
        with self._guard:
            candidates = [active for active in self._active.values() if now >= active.expires_at]
        expired = 0
        for active in candidates:
            if active.lock.acquire(blocking=False):
                try:
                    with self._guard:
                        if self._active.get(active.token) is not active:
                            continue
                    self._finish(active, VerificationStatus.TIMED_OUT)
                    expired += 1
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
        try:
            active.worker.close()
        except Exception:
            try:
                active.lease.mark_stale()
            except Exception:  # pragma: no cover - lease metadata fault injection is covered separately
                pass  # pragma: no cover
            with self._guard:
                self._active.pop(active.token, None)
                self._failed_closures[active.token] = active
            return VerificationOutcome(VerificationStatus.FAILED, active.safe_origin, active.attempt)
        try:
            active.lease.close()
        except Exception:  # pragma: no cover - BrowserSessionStore fault injection owns this branch
            status = VerificationStatus.FAILED  # pragma: no cover
        with self._guard:
            self._active.pop(active.token, None)
            if status is VerificationStatus.COMPLETED:
                self._ledger.clear(active.ledger_key)
        return VerificationOutcome(status, active.safe_origin, active.attempt, page if status is VerificationStatus.COMPLETED else None)

    def _handle_worker_failure(self, active: _ActiveVerification) -> VerificationOutcome:
        try:
            alive = active.worker.is_alive()
        except Exception:  # pragma: no cover - conservative fallback for a broken worker channel
            alive = True  # pragma: no cover
        if not alive:
            try:
                active.lease.mark_stale()
            except Exception:  # pragma: no cover - stale marking failure remains privacy-safe
                pass  # pragma: no cover
            try:
                active.lease.close()
            except Exception:  # pragma: no cover - dead process permits lock release despite metadata failure
                pass  # pragma: no cover
            with self._guard:
                self._active.pop(active.token, None)
            return VerificationOutcome(VerificationStatus.FAILED, active.safe_origin, active.attempt)
        return self._finish(active, VerificationStatus.FAILED)


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
        max_network_bytes: int = 64 * 1024 * 1024,
        browser_ttl: float = 60.0,
    ) -> None:
        if max_body_bytes <= 0 or max_network_bytes <= 0 or browser_ttl <= 0:
            raise ValueError("browser limits must be positive")
        self.http = http or HttpPageAcquirer()
        self.classifier = classifier or PageClassifier()
        self.driver = driver or DefaultPlaywrightDriver(
            max_body_bytes=max_body_bytes,
            max_network_bytes=max_network_bytes,
            operation_timeout=browser_ttl,
        )
        self.sessions = sessions
        self.coordinator = coordinator
        self.safety_policy = safety_policy or UrlSafetyPolicy()
        self.max_body_bytes = min(max_body_bytes, 10 * 1024 * 1024)
        self.browser_ttl = browser_ttl

    def fetch_page(self, url: str, *, task_key: str = "browser-acquisition") -> AcquiredPage:
        page = self.http.fetch_page(
            url,
            max_body_bytes=self.max_body_bytes,
            classifiable_statuses=frozenset({403, 429}),
        )
        kind = self.classifier.classify(page.snapshot).kind
        if kind is PageKind.AUTH_OR_CHALLENGE:
            self._require_verification(url, task_key)
        if kind is not PageKind.UNKNOWN:
            return page
        policy = BrowserRequestPolicy(self.safety_policy)
        lease: BrowserSessionLease | None = None
        worker: BrowserContextWorker | None = None
        try:
            policy.lock(url)
            lease = self.sessions.acquire(_domain(url))
            worker = BrowserContextWorker(
                self.driver,
                user_data_dir=lease.profile_path,
                headless=True,
                policy=policy,
                ttl=self.browser_ttl,
            )
            worker.start()
            browser_page = worker.navigate(url).to_acquired_page(max_body_bytes=self.max_body_bytes)
            if self.classifier.classify(browser_page.snapshot).kind is PageKind.AUTH_OR_CHALLENGE:
                worker.close()
                worker = None
                lease.close()
                lease = None
                self._require_verification(url, task_key)
            return browser_page
        except VerificationRequired:
            raise
        except Exception:
            raise AcquisitionError("browser_failed", redact_url(url), True) from None
        finally:
            if worker is not None:
                try:
                    worker.close()
                except Exception:
                    if lease is not None:
                        try:
                            lease.mark_stale()
                        except Exception:  # pragma: no cover - cleanup already fails closed
                            pass  # pragma: no cover
                    raise AcquisitionError("browser_cleanup_failed", redact_url(url), False) from None
            if lease is not None:
                try:
                    lease.close()
                except Exception:  # pragma: no cover - BrowserSessionStore fault injection owns this branch
                    raise AcquisitionError("browser_cleanup_failed", redact_url(url), False) from None  # pragma: no cover

    def fetch(self, url: str, *, task_key: str = "browser-acquisition") -> PageSnapshot:
        return self.fetch_page(url, task_key=task_key).snapshot

    def _require_verification(self, url: str, task_key: str) -> None:
        ticket = self.coordinator.begin(url, task_key=task_key) if self.coordinator is not None else None
        raise VerificationRequired(ticket=ticket)

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
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

from novel_crawler.acquisition.classifier import PageClassifier, PageKind
from novel_crawler.acquisition.http import AcquisitionError, HttpPageAcquirer
from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.acquisition.security import UrlSafetyPolicy, redact_url
from novel_crawler.core.domains import canonical_domain
from novel_crawler.verification import BrowserCleanupRequired, VerificationRequired

from .driver import BrowserContextWorker, BrowserRequestPolicy, DefaultPlaywrightDriver, Driver
from .models import VerificationOutcome, VerificationStatus, VerificationTicket
from .sessions import BrowserSessionLease, BrowserSessionStore, _DomainLock

_TASK_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_LEDGER_RECORD_BYTES = 512
_REPAIR_LIMIT = 1024


class HttpAcquirer(Protocol):
    def fetch_page(self, url: str, **kwargs: object) -> AcquiredPage: ...


@dataclass
class _ActiveVerification:
    token: str
    original_url: str = field(repr=False)
    safe_origin: str
    expires_at: datetime
    domain: str
    ledger_key: str = field(repr=False)
    reservation_id: str = field(repr=False)
    lease: BrowserSessionLease = field(repr=False)
    worker: BrowserContextWorker = field(repr=False)
    attempt: int = 0
    lifecycle: str = "pending"
    cleanup_status: VerificationStatus | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class _FailedHeadlessCleanup:
    worker: BrowserContextWorker = field(repr=False)
    lease: BrowserSessionLease = field(repr=False)
    safe_origin: str
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _domain(url: str) -> str:
    host = urlsplit(url).hostname
    if host is None:
        raise VerificationRequired("verification_url_invalid")
    return canonical_domain(host)


class _AttemptLedger:
    def __init__(
        self,
        sessions: BrowserSessionStore,
        clock: Callable[[], datetime],
        ttl: timedelta,
        *,
        max_keys: int = 1024,
        max_records: int = 1024,
    ) -> None:
        if max_keys <= 0 or max_records <= 0:
            raise ValueError("ledger limits must be positive")
        self._io = sessions._io
        self._clock = clock
        self._ttl = ttl
        self._key_path = sessions.root / "verification-attempts.key"
        self._ledger_path = sessions.root / "verification-attempts.json"
        self._lock_path = sessions.root / "locks" / "allocation.lock"
        self._lock_timeout = sessions._lock_timeout
        self._max_keys = max_keys
        self._max_records = max_records
        self._byte_limit = max(4096, max_records * _LEDGER_RECORD_BYTES + max_keys * 96)
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
            self._values, changed = self._read()
            if changed:
                self._write()
        except Exception:  # pragma: no cover - injected storage backends cover I/O safety elsewhere
            raise VerificationRequired("verification_ledger_failed") from None  # pragma: no cover
        finally:
            lock.release()

    def opaque_key(self, safe_origin: str, task_key: str) -> str:
        return hmac.new(self._secret, f"{safe_origin}|{task_key}".encode(), hashlib.sha256).hexdigest()

    def reserve(self, key: str, maximum: int) -> str:
        lock = self._acquire()
        try:
            self._values, _ = self._read()
            attempts = self._values.setdefault(key, [])
            if len(attempts) >= maximum:
                raise VerificationRequired("verification_attempts_exhausted")
            record_count = sum(len(records) for records in self._values.values())
            if (len(self._values) > self._max_keys) or record_count >= self._max_records:
                if not attempts:
                    self._values.pop(key, None)
                raise VerificationRequired("verification_ledger_capacity")
            reservation_id = secrets.token_hex(16)
            attempts.append(
                {
                    "reservation_id": reservation_id,
                    "state": "reserved",
                    "expires_at": (self._clock() + self._ttl).isoformat(),
                }
            )
            try:
                self._write()
            except Exception:
                attempts[:] = [item for item in attempts if item["reservation_id"] != reservation_id]
                if not attempts:
                    self._values.pop(key, None)
                try:
                    self._write()
                except Exception:
                    pass
                raise VerificationRequired("verification_ledger_failed") from None
            return reservation_id
        finally:
            lock.release()

    def finish(self, key: str, reservation_id: str, *, consumed: bool) -> None:
        lock = self._acquire()
        try:
            self._values, changed = self._read()
            attempts = self._values.get(key)
            found = False
            if attempts is not None:
                for record in tuple(attempts):
                    if record["reservation_id"] == reservation_id:
                        found = True
                        if consumed:
                            record["state"] = "used"
                        else:
                            attempts.remove(record)
                        break
                if not attempts:
                    self._values.pop(key, None)
            if found or changed:
                self._write()
        finally:
            lock.release()

    def _acquire(self) -> _DomainLock:
        lock = _DomainLock(self._lock_path, self._lock_timeout, self._io)
        try:
            lock.acquire()
            return lock
        except Exception:  # pragma: no cover - RegistryIO lock fault injection is covered separately
            raise VerificationRequired("verification_ledger_failed") from None  # pragma: no cover

    def _read(self) -> tuple[dict[str, list[dict[str, str]]], bool]:
        if not self._ledger_path.exists():
            return {}, False
        raw = json.loads(self._io.read_bounded(self._ledger_path, self._byte_limit))
        if not isinstance(raw, dict) or any(not re.fullmatch(r"[0-9a-f]{64}", str(key)) for key in raw):
            raise ValueError  # pragma: no cover - persistent storage tamper defense
        values: dict[str, list[dict[str, str]]] = {}
        total = 0
        now = self._clock()
        changed = False
        for key, items in raw.items():
            if not isinstance(items, list):
                raise ValueError  # pragma: no cover - persistent storage tamper defense
            records: list[dict[str, str]] = []
            for item in items:
                if not isinstance(item, dict) or set(item) != {"reservation_id", "state", "expires_at"}:
                    raise ValueError  # pragma: no cover
                reservation_id = item["reservation_id"]
                state = item["state"]
                expires = item["expires_at"]
                if (
                    not isinstance(reservation_id, str)
                    or not re.fullmatch(r"[0-9a-f]{32}", reservation_id)
                    or state not in {"reserved", "used"}
                    or not isinstance(expires, str)
                ):
                    raise ValueError  # pragma: no cover
                if datetime.fromisoformat(expires) <= now:
                    changed = True
                    continue
                records.append({"reservation_id": reservation_id, "state": state, "expires_at": expires})
                total += 1
                if total > self._max_records:  # pragma: no cover - reserve prevents oversized valid files
                    raise VerificationRequired("verification_ledger_capacity")  # pragma: no cover
            if records:
                values[str(key)] = records
            elif items:
                changed = True
        if len(values) > self._max_keys:  # pragma: no cover - reserve prevents oversized valid files
            raise VerificationRequired("verification_ledger_capacity")  # pragma: no cover
        return values, changed

    def _write(self) -> None:
        try:
            payload = json.dumps(self._values, sort_keys=True, separators=(",", ":")).encode("ascii")
            if len(payload) > self._byte_limit:  # pragma: no cover - capacity prevents practical growth
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
        self._early_terminal: dict[str, tuple[str, bool]] = {}
        self._ledger_repairs: dict[str, tuple[str, str, bool]] = {}
        self._reserved = 0
        self._guard = threading.Lock()
        self._ledger = _AttemptLedger(sessions, self.clock, attempt_ttl)

    def begin(self, url: str, *, task_key: str) -> VerificationTicket:
        if not isinstance(task_key, str) or not _TASK_KEY.fullmatch(task_key):
            raise VerificationRequired("verification_task_invalid")
        self.expire_sweep()
        domain = _domain(url)
        policy = BrowserRequestPolicy(self.safety_policy)
        try:
            policy.lock(url)
        except Exception:  # pragma: no cover - URL policy failures are exhaustively tested at policy layer
            raise VerificationRequired("verification_start_failed") from None  # pragma: no cover
        safe_origin = redact_url(url)
        ledger_key = self._ledger.opaque_key(safe_origin, task_key)
        with self._guard:
            if self._capacity_used() + self._reserved >= self.max_active:
                raise VerificationRequired("verification_capacity")
            self._reserved += 1
        try:
            reservation_id = self._ledger.reserve(ledger_key, self.max_attempts)
        except VerificationRequired:
            with self._guard:
                self._reserved -= 1
            raise
        except Exception:
            with self._guard:
                self._reserved -= 1
            raise VerificationRequired("verification_ledger_failed") from None
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
                terminal_callback=lambda reason, closed_ok: self._worker_terminal(token, reason, closed_ok),
            )
            active = _ActiveVerification(
                token, url, safe_origin, expires_at, domain, ledger_key, reservation_id, lease, worker
            )
            with self._guard:
                self._reserved -= 1
                self._active[token] = active
                early = self._early_terminal.pop(token, None)
            if early is not None:
                self._worker_terminal(token, *early)
                raise RuntimeError("browser_worker_terminal")
            worker.start()
            worker.navigate(url)
            with self._guard:
                if active.lifecycle != "pending":
                    raise RuntimeError("browser_worker_terminal")
                active.lifecycle = "active"
            return VerificationTicket(token, VerificationStatus.WAITING, safe_origin, expires_at, 0)
        except Exception:
            if lease is None or worker is None:
                with self._guard:
                    self._reserved -= 1
                if lease is not None:
                    try:
                        lease.close()
                    except Exception:
                        pass
                self._finish_ledger(token, ledger_key, reservation_id, consumed=False)
                raise VerificationRequired("verification_start_failed") from None
            with self._guard:
                registered = self._active.get(token) is active
                quarantined = token in self._failed_closures
            if registered and not quarantined and active.lifecycle in {"pending", "active"}:
                self._finish(active, VerificationStatus.FAILED, ledger_consumed=False)
            with self._guard:
                quarantined = token in self._failed_closures
            if quarantined:
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
                if active.lifecycle == "quarantined":
                    return self._cleanup_outcome(active)
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
            if active.lifecycle == "quarantined":
                return self._cleanup_outcome(active)
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
                self._active.pop(token, None)
                active.lifecycle = "released"
            return True

    def expire_sweep(self) -> int:
        self._retry_ledger_repairs()
        now = self.clock()
        with self._guard:
            candidates = [
                active for active in self._active.values()
                if active.lifecycle == "active" and now >= active.expires_at
            ]
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
        if active is None or active.lifecycle not in {"active", "quarantined"}:
            raise VerificationRequired("verification_token_invalid")
        return active

    @staticmethod
    def _cleanup_outcome(active: _ActiveVerification) -> VerificationOutcome:
        return VerificationOutcome(
            active.cleanup_status or VerificationStatus.FAILED,
            active.safe_origin,
            active.attempt,
            cleanup_required=True,
            cleanup_ticket=active.token,
        )

    def _finish(
        self,
        active: _ActiveVerification,
        status: VerificationStatus,
        page: AcquiredPage | None = None,
        *,
        ledger_consumed: bool | None = None,
    ) -> VerificationOutcome:
        with self._guard:
            if active.lifecycle in {"terminating", "quarantined", "released"}:
                return self._cleanup_outcome(active) if active.lifecycle == "quarantined" else VerificationOutcome(
                    VerificationStatus.FAILED, active.safe_origin, active.attempt
                )
            active.lifecycle = "terminating"
        try:
            active.worker.close()
        except Exception:
            self._quarantine(active, status)
            self._finish_ledger(
                active.token, active.ledger_key, active.reservation_id,
                consumed=(status is not VerificationStatus.COMPLETED) if ledger_consumed is None else ledger_consumed,
            )
            return VerificationOutcome(
                status, active.safe_origin, active.attempt, page,
                cleanup_required=True, cleanup_ticket=active.token,
            )
        try:
            active.lease.close()
        except Exception:
            self._quarantine(active, status)
            self._finish_ledger(
                active.token, active.ledger_key, active.reservation_id,
                consumed=(status is not VerificationStatus.COMPLETED) if ledger_consumed is None else ledger_consumed,
            )
            return VerificationOutcome(
                status, active.safe_origin, active.attempt, page,
                cleanup_required=True, cleanup_ticket=active.token,
            )
        with self._guard:
            self._active.pop(active.token, None)
            active.lifecycle = "released"
        self._finish_ledger(
            active.token, active.ledger_key, active.reservation_id,
            consumed=(status is not VerificationStatus.COMPLETED) if ledger_consumed is None else ledger_consumed,
        )
        return VerificationOutcome(status, active.safe_origin, active.attempt, page if status is VerificationStatus.COMPLETED else None)

    def _handle_worker_failure(self, active: _ActiveVerification) -> VerificationOutcome:
        with self._guard:
            if self._active.get(active.token) is not active:
                return VerificationOutcome(VerificationStatus.FAILED, active.safe_origin, active.attempt)
        try:
            alive = active.worker.is_alive()
        except Exception:  # pragma: no cover - conservative fallback for a broken worker channel
            alive = True  # pragma: no cover
        if not alive:  # pragma: no cover - terminal callback normally owns confirmed crash cleanup
            self._worker_terminal(active.token, "crash", True)
            return VerificationOutcome(VerificationStatus.FAILED, active.safe_origin, active.attempt)
        return self._finish(active, VerificationStatus.FAILED)

    def _worker_terminal(self, token: str, reason: str, closed_ok: bool) -> None:
        with self._guard:
            active = self._active.get(token)
            if active is None:
                if len(self._early_terminal) >= _REPAIR_LIMIT:
                    self._early_terminal.pop(next(iter(self._early_terminal)))
                self._early_terminal[token] = (reason, closed_ok)
                return
            if active.lifecycle in {"terminating", "quarantined", "released"}:
                return
            consumed = active.lifecycle != "pending"
            active.lifecycle = "terminating"
        if not closed_ok:
            self._quarantine(active, VerificationStatus.FAILED)
            self._finish_ledger(token, active.ledger_key, active.reservation_id, consumed=consumed)
            return
        if reason == "crash":
            try:
                active.lease.mark_stale()
            except Exception:
                pass
        try:
            active.lease.close()
        except Exception:
            self._quarantine(active, VerificationStatus.FAILED)
        else:
            with self._guard:
                self._active.pop(token, None)
                active.lifecycle = "released"
        self._finish_ledger(token, active.ledger_key, active.reservation_id, consumed=consumed)

    def _capacity_used(self) -> int:
        return len(set(self._active) | set(self._failed_closures))

    def _quarantine(self, active: _ActiveVerification, status: VerificationStatus) -> None:
        try:
            active.lease.mark_stale()
        except Exception:
            pass
        with self._guard:
            active.lifecycle = "quarantined"
            active.cleanup_status = status
            self._failed_closures[active.token] = active

    def _finish_ledger(self, token: str, key: str, reservation_id: str, *, consumed: bool) -> None:
        try:
            self._ledger.finish(key, reservation_id, consumed=consumed)
        except Exception:
            with self._guard:
                if len(self._ledger_repairs) >= _REPAIR_LIMIT and token not in self._ledger_repairs:
                    self._ledger_repairs.pop(next(iter(self._ledger_repairs)))
                self._ledger_repairs[token] = (key, reservation_id, consumed)

    def _retry_ledger_repairs(self) -> None:
        with self._guard:
            repairs = tuple(self._ledger_repairs.items())
        for token, (key, reservation_id, consumed) in repairs:
            try:
                self._ledger.finish(key, reservation_id, consumed=consumed)
            except Exception:
                continue
            with self._guard:
                self._ledger_repairs.pop(token, None)


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
        max_failed_cleanups: int = 8,
    ) -> None:
        if max_body_bytes <= 0 or max_network_bytes <= 0 or browser_ttl <= 0 or max_failed_cleanups <= 0:
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
        self.max_failed_cleanups = max_failed_cleanups
        self._cleanup_guard = threading.Lock()
        self._failed_cleanups: dict[str, _FailedHeadlessCleanup] = {}
        self._cleanup_reserved = 0
        self._profile_grants: dict[str, int] = {}
        self._resolution_local = threading.local()

    def fetch_page(
        self,
        url: str,
        *,
        task_key: str | None = None,
        max_body_bytes: int | None = None,
        locked_origin: str | None = None,
    ) -> AcquiredPage:
        limit = self.max_body_bytes if max_body_bytes is None else min(self.max_body_bytes, max_body_bytes)
        if limit <= 0:
            raise AcquisitionError("response_too_large", redact_url(url), False)
        if locked_origin is not None and redact_url(url).rstrip("/") != locked_origin.rstrip("/"):
            raise AcquisitionError("cross_origin", redact_url(url), False)
        scoped_task_key = getattr(self._resolution_local, "task_key", None)
        effective_task_key = task_key or (scoped_task_key if isinstance(scoped_task_key, str) else "browser-acquisition")
        if locked_origin is not None:
            page = self.http.fetch_page(
                url,
                max_body_bytes=limit,
                locked_origin=locked_origin,
                classifiable_statuses=frozenset({401, 403, 429, 503}),
            )
        else:
            page = self.http.fetch_page(
                url,
                max_body_bytes=limit,
                classifiable_statuses=frozenset({401, 403, 429, 503}),
            )
        kind = self.classifier.classify(page.snapshot).kind
        profile_granted = self._consume_profile_grant(url, effective_task_key) if kind is PageKind.AUTH_OR_CHALLENGE else False
        if kind is PageKind.AUTH_OR_CHALLENGE and not profile_granted:
            self._require_verification(url, effective_task_key)
        if kind is not PageKind.UNKNOWN and not profile_granted:
            return page
        with self._cleanup_guard:
            if len(self._failed_cleanups) + self._cleanup_reserved >= self.max_failed_cleanups:
                raise AcquisitionError("browser_cleanup_capacity", redact_url(url), False)
            self._cleanup_reserved += 1
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
            browser_page = worker.navigate(url).to_acquired_page(max_body_bytes=limit)
            if self.classifier.classify(browser_page.snapshot).kind is PageKind.AUTH_OR_CHALLENGE:
                worker.close()
                lease.close()
                worker = None
                lease = None
                self._require_verification(url, effective_task_key)
            return browser_page
        except VerificationRequired:
            raise
        except Exception:
            raise AcquisitionError("browser_failed", redact_url(url), True) from None
        finally:
            cleanup_error: BrowserCleanupRequired | AcquisitionError | None = None
            try:
                if worker is not None:
                    try:
                        worker.close()
                    except Exception:
                        if lease is not None:
                            try:
                                lease.mark_stale()
                            except Exception:  # pragma: no cover - cleanup already fails closed
                                pass  # pragma: no cover
                            cleanup_error = self._quarantine_cleanup(worker, lease, url)
                        else:  # pragma: no cover - worker implies a lease in the construction invariant
                            cleanup_error = AcquisitionError("browser_cleanup_failed", redact_url(url), False)  # pragma: no cover
                if cleanup_error is None and lease is not None:
                    try:
                        lease.close()
                    except Exception:
                        if worker is not None:
                            cleanup_error = self._quarantine_cleanup(worker, lease, url)
                        else:  # pragma: no cover - closed worker remains available for lease retry registry
                            cleanup_error = AcquisitionError("browser_cleanup_failed", redact_url(url), False)  # pragma: no cover
            finally:
                with self._cleanup_guard:
                    self._cleanup_reserved -= 1
            if cleanup_error is not None:
                raise cleanup_error from None

    def fetch(self, url: str, *, task_key: str | None = None) -> PageSnapshot:
        return self.fetch_page(url, task_key=task_key).snapshot

    def _require_verification(self, url: str, task_key: str) -> None:
        ticket = self.coordinator.begin(url, task_key=task_key) if self.coordinator is not None else None
        raise VerificationRequired(ticket=ticket, original_url=url, safe_origin=redact_url(url))

    def activate_persistent_profile(self, url: str, *, task_key: str, pages: int = 3) -> None:
        """Allow a bounded number of same-origin headless reads after manual verification."""
        if pages <= 0 or pages > 3:
            raise ValueError("profile page allowance must be between one and three")
        if not _TASK_KEY.fullmatch(task_key):
            raise ValueError("profile task key is invalid")
        origin = redact_url(url)
        with self._cleanup_guard:
            self._profile_grants[f"{origin}|{task_key}"] = pages

    def _consume_profile_grant(self, url: str, task_key: str) -> bool:
        key = f"{redact_url(url)}|{task_key}"
        with self._cleanup_guard:
            remaining = self._profile_grants.get(key, 0)
            if remaining <= 0:
                return False
            if remaining == 1:
                self._profile_grants.pop(key, None)
            else:
                self._profile_grants[key] = remaining - 1
            return True

    def deactivate_persistent_profile(self, url: str, *, task_key: str) -> None:
        with self._cleanup_guard:
            self._profile_grants.pop(f"{redact_url(url)}|{task_key}", None)

    @contextmanager
    def resolution_scope(self, task_key: str):
        previous = getattr(self._resolution_local, "task_key", None)
        self._resolution_local.task_key = task_key
        try:
            yield
        finally:
            if previous is None:
                del self._resolution_local.task_key
            else:
                self._resolution_local.task_key = previous

    def retry_cleanup(self, token: str) -> bool:
        with self._cleanup_guard:
            failed = self._failed_cleanups.get(token)
        if failed is None:
            raise VerificationRequired("verification_token_invalid")
        with failed.lock:
            try:
                failed.worker.close()
                failed.lease.close()
            except Exception:
                return False
            with self._cleanup_guard:
                self._failed_cleanups.pop(token, None)
            return True

    def _quarantine_cleanup(
        self,
        worker: BrowserContextWorker,
        lease: BrowserSessionLease,
        url: str,
    ) -> BrowserCleanupRequired:
        token = secrets.token_urlsafe(32)
        safe_origin = redact_url(url)
        with self._cleanup_guard:
            self._failed_cleanups[token] = _FailedHeadlessCleanup(worker, lease, safe_origin)
        return BrowserCleanupRequired(token, safe_origin)

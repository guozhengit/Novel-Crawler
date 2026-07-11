"""Adapt configuration resolution around bounded interactive verification."""

from __future__ import annotations

import json
import secrets
import threading
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from novel_crawler.acquisition.security import redact_url
from novel_crawler.adaptation.config_manager import ConfigResolution, ResolutionKind
from novel_crawler.verification import BrowserCleanupRequired

from .coordinator import BrowserAcquirer, VerificationCoordinator, VerificationRequired
from .models import VerificationOutcome, VerificationStatus, VerificationTicket


class _Manager(Protocol):
    def resolve(self, url: str) -> ConfigResolution: ...


class _Acquirer(Protocol):
    def activate_persistent_profile(self, url: str, *, task_key: str, pages: int) -> None: ...
    def deactivate_persistent_profile(self, url: str, *, task_key: str) -> None: ...
    def retry_cleanup(self, token: str) -> bool: ...


class _Coordinator(Protocol):
    def begin(self, url: str, *, task_key: str) -> VerificationTicket: ...
    def continue_verification(self, token: str) -> VerificationOutcome: ...
    def cancel(self, token: str) -> VerificationOutcome: ...
    def expire_sweep(self) -> int: ...
    def retry_cleanup(self, token: str) -> bool: ...


class AdaptiveResult:
    """Immutable resolution wrapper with explicit access to sensitive handles."""

    __slots__ = ("_cleanup_source", "_cleanup_ticket", "_resolution", "_ticket")

    def __init__(
        self,
        resolution: ConfigResolution,
        ticket: VerificationTicket | None = None,
        *,
        cleanup_ticket: str | None = None,
        cleanup_source: str | None = None,
    ) -> None:
        if resolution.kind is ResolutionKind.WAITING_FOR_USER:
            if ticket is None or cleanup_ticket is not None:
                raise ValueError("waiting results require a ticket")
        elif resolution.kind is ResolutionKind.CLEANUP_REQUIRED:
            if ticket is not None or not cleanup_ticket or cleanup_source not in {"headless", "visible"}:
                raise ValueError("cleanup results require a private cleanup handle and safe source")
        elif ticket is not None or cleanup_ticket is not None or cleanup_source is not None:
            raise ValueError("terminal results cannot contain private handles")
        object.__setattr__(self, "_resolution", resolution)
        object.__setattr__(self, "_ticket", ticket)
        object.__setattr__(self, "_cleanup_ticket", cleanup_ticket)
        object.__setattr__(self, "_cleanup_source", cleanup_source)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("AdaptiveResult is immutable")

    resolution = property(lambda self: self._resolution)
    kind = property(lambda self: self._resolution.kind)
    config = property(lambda self: self._resolution.config)
    confirmation_token = property(lambda self: self._resolution.confirmation_token)
    reason_ids = property(lambda self: self._resolution.reason_ids)
    ticket = property(lambda self: self._ticket)
    cleanup_ticket = property(lambda self: self._cleanup_ticket)
    cleanup_source = property(lambda self: self._cleanup_source)

    def __repr__(self) -> str:
        return (
            f"AdaptiveResult(kind={self.kind.value!r}, config_present={self.config is not None!r}, "
            f"confirmation_required={self.confirmation_token is not None!r}, ticket_present={self.ticket is not None!r}, "
            f"cleanup_required={self.cleanup_ticket is not None!r}, cleanup_source={self.cleanup_source!r}, "
            f"reason_ids={self.reason_ids!r})"
        )

    def to_dict(self) -> dict[str, object]:
        value = self.resolution.to_dict()
        value["ticket_present"] = self.ticket is not None
        value["cleanup_required"] = self.cleanup_ticket is not None
        value["cleanup_source"] = self.cleanup_source
        if self.ticket is not None:
            value["safe_origin"] = self.ticket.safe_origin
            value["attempt"] = self.ticket.attempt
            value["expires_at"] = self.ticket.expires_at.isoformat() if self.ticket.expires_at is not None else None
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass
class _ResumeContext:
    ticket: VerificationTicket
    request_url: str = field(repr=False)
    challenge_url: str = field(repr=False)
    task_key: str = field(repr=False)
    request_key: tuple[str, str] = field(repr=False)
    generation: int = 0
    last_result: AdaptiveResult | None = None
    terminal: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class _PendingResolve:
    condition: threading.Condition = field(repr=False)
    done: bool = False
    result: AdaptiveResult | None = None


@dataclass
class _CleanupContext:
    source: str
    operation: str
    request_url: str | None = field(default=None, repr=False)
    task_key: str | None = field(default=None, repr=False)
    request_key: tuple[str, str] | None = field(default=None, repr=False)
    resume: _ResumeContext | None = field(default=None, repr=False)
    terminal_kind: ResolutionKind | None = None
    result_after_cleanup: AdaptiveResult | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class AdaptiveBrowserService:
    """Run config adaptation, pausing safely when browser verification is required."""

    def __init__(
        self,
        config_manager: _Manager,
        browser_acquirer: BrowserAcquirer | _Acquirer,
        verification_coordinator: VerificationCoordinator | _Coordinator,
    ) -> None:
        self.config_manager = config_manager
        self.browser_acquirer = browser_acquirer
        self.verification_coordinator = verification_coordinator
        self._guard = threading.RLock()
        self._contexts: dict[str, _ResumeContext] = {}
        self._requests: dict[tuple[str, str], str] = {}
        self._cleanup_tickets: dict[str, VerificationTicket] = {}
        self._cleanup_contexts: dict[str, _CleanupContext] = {}
        self._cleanup_requests: dict[tuple[str, str], str] = {}
        self._inflight: dict[tuple[str, str], _PendingResolve] = {}
        self._terminal: OrderedDict[str, tuple[datetime, AdaptiveResult]] = OrderedDict()
        self._terminal_ttl = timedelta(minutes=10)
        self._max_terminal = 1024
        self._wire_acquirer(config_manager, browser_acquirer)
        if isinstance(browser_acquirer, BrowserAcquirer):
            browser_acquirer.coordinator = None

    def resolve(self, url: str, task_key: str) -> AdaptiveResult:
        key = (redact_url(url), task_key)
        with self._guard:
            cleanup_token = self._cleanup_requests.get(key)
            if cleanup_token is not None and cleanup_token in self._cleanup_contexts:
                return self._cleanup_required(cleanup_token, self._cleanup_contexts[cleanup_token].source)
            token = self._requests.get(key)
            if token is not None and (context := self._contexts.get(token)) is not None:
                return context.last_result or self._waiting(context.ticket)
            pending = self._inflight.get(key)
            if pending is not None:
                while not pending.done:
                    pending.condition.wait()
                assert pending.result is not None
                return pending.result
            pending = _PendingResolve(threading.Condition(self._guard))
            self._inflight[key] = pending
        try:
            result = self._resolve_once(url, task_key, key)
        except Exception:
            result = self._failure("adaptive_resolve_failed")
        with self._guard:
            pending.result = result
            pending.done = True
            self._inflight.pop(key, None)
            pending.condition.notify_all()
        return result

    def _resolve_once(self, url: str, task_key: str, key: tuple[str, str]) -> AdaptiveResult:
        try:
            return AdaptiveResult(self._resolve_manager(url, task_key))
        except BrowserCleanupRequired as cleanup:
            with self._guard:
                self._cleanup_contexts[cleanup.token] = _CleanupContext("headless", "headless", url, task_key, key)
                self._cleanup_requests[key] = cleanup.token
            return self._cleanup_required(cleanup.token, "headless")
        except VerificationRequired as required:
            original = required.original_url or url
            try:
                ticket = required.ticket or self.verification_coordinator.begin(original, task_key=task_key)
            except VerificationRequired as exc:
                if exc.ticket is not None and exc.ticket.status is VerificationStatus.FAILED:
                    return self._hold_start_cleanup(exc.ticket, key)
                return self._failure(exc.code)
            if ticket.status is not VerificationStatus.WAITING:
                return self._hold_start_cleanup(ticket, key)
            context = _ResumeContext(ticket, url, original, task_key, key)
            result = self._waiting(ticket)
            context.last_result = result
            with self._guard:
                self._contexts[ticket.token] = context
                self._requests[key] = ticket.token
            return result

    def continue_verification(self, ticket: VerificationTicket | str) -> AdaptiveResult:
        token = self._token(ticket)
        with self._guard:
            terminal = self._terminal_result(token)
            if terminal is not None:
                return terminal
            context = self._contexts.get(token)
            if context is None:
                return self._failure("verification_token_invalid")
            observed_generation = ticket.attempt if isinstance(ticket, VerificationTicket) else context.generation
        with context.lock:
            if context.last_result is not None and context.last_result.kind is ResolutionKind.CLEANUP_REQUIRED:
                return context.last_result
            if context.terminal:
                assert context.last_result is not None
                return context.last_result
            if context.generation != observed_generation:
                assert context.last_result is not None
                return context.last_result
            try:
                outcome = self.verification_coordinator.continue_verification(token)
            except VerificationRequired as exc:
                if context.ticket.expires_at is not None and context.ticket.expires_at <= datetime.now(UTC):
                    result = self._from_status(VerificationStatus.TIMED_OUT)
                else:
                    result = self._failure(exc.code)
                return self._finish(context, result)
            context.generation += 1
            if outcome.cleanup_required:
                terminal_kind = ResolutionKind.TIMED_OUT if (
                    context.ticket.expires_at is not None and context.ticket.expires_at <= datetime.now(UTC)
                ) else ResolutionKind.VERIFICATION_FAILED
                return self._hold_visible_cleanup(
                    context,
                    outcome.cleanup_ticket,
                    resume=outcome.page is not None,
                    terminal_kind=terminal_kind,
                )
            if outcome.status is VerificationStatus.WAITING:
                updated = VerificationTicket(
                    token,
                    VerificationStatus.WAITING,
                    outcome.safe_origin,
                    context.ticket.expires_at,
                    outcome.attempt,
                )
                context.ticket = updated
                context.last_result = self._waiting(updated)
                return context.last_result
            if outcome.status is VerificationStatus.COMPLETED:
                result = self._resume_verified(context)
                return result if result.kind is ResolutionKind.CLEANUP_REQUIRED else self._finish(context, result)
            return self._finish(context, self._from_status(outcome.status))

    resume = continue_verification

    def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
        token = self._token(ticket)
        with self._guard:
            terminal = self._terminal_result(token)
            if terminal is not None:
                return terminal
            context = self._contexts.get(token)
            if context is None:
                return self._failure("verification_token_invalid")
        with context.lock:
            if context.last_result is not None and context.last_result.kind is ResolutionKind.CLEANUP_REQUIRED:
                return context.last_result
            if context.terminal:
                assert context.last_result is not None
                return context.last_result
            try:
                outcome = self.verification_coordinator.cancel(token)
            except VerificationRequired as exc:
                result = self._failure(exc.code)
            else:
                if outcome.cleanup_required:
                    return self._hold_visible_cleanup(
                        context, outcome.cleanup_ticket, resume=False, terminal_kind=ResolutionKind.CANCELLED
                    )
                result = self._from_status(outcome.status)
            return self._finish(context, result)

    def expire_sweep(self) -> int:
        now = datetime.now(UTC)
        with self._guard:
            self._purge_terminal()
            expired = [context for context in self._contexts.values() if context.ticket.expires_at is not None and context.ticket.expires_at <= now]
        count = 0
        for context in expired:
            before = context.terminal
            self.continue_verification(context.ticket)
            count += int(not before and context.terminal)
        self.verification_coordinator.expire_sweep()
        return count

    def retry_cleanup(self, ticket: VerificationTicket | str) -> AdaptiveResult:
        token = self._token(ticket)
        with self._guard:
            cleanup = self._cleanup_contexts.get(token)
        if cleanup is None:
            return self._terminal_result(token) or self._failure("verification_token_invalid")
        with cleanup.lock:
            with self._guard:
                if self._cleanup_contexts.get(token) is not cleanup:
                    return self._terminal_result(token) or self._failure("verification_token_invalid")
            try:
                if cleanup.operation == "visible":
                    cleaned = self.verification_coordinator.retry_cleanup(token)
                elif cleanup.operation == "profile":
                    assert cleanup.request_url is not None and cleanup.task_key is not None
                    self.browser_acquirer.deactivate_persistent_profile(cleanup.request_url, task_key=cleanup.task_key)
                    cleaned = True
                else:
                    cleaned = self.browser_acquirer.retry_cleanup(token)
                    if cleaned and cleanup.operation == "headless_profile":
                        cleanup.operation = "profile"
                        assert cleanup.request_url is not None and cleanup.task_key is not None
                        self.browser_acquirer.deactivate_persistent_profile(cleanup.request_url, task_key=cleanup.task_key)
            except VerificationRequired as exc:
                return self._cleanup_required(token, cleanup.source, exc.code)
            except Exception:
                return self._cleanup_required(token, cleanup.source, "cleanup_retry_failed")
            if not cleaned:
                return self._cleanup_required(token, cleanup.source)
            with self._guard:
                self._cleanup_tickets.pop(token, None)
                self._cleanup_contexts.pop(token, None)
                if cleanup.request_key is not None:
                    self._cleanup_requests.pop(cleanup.request_key, None)
            if cleanup.result_after_cleanup is not None and cleanup.resume is not None:
                result = self._finish(cleanup.resume, cleanup.result_after_cleanup)
            elif cleanup.resume is not None:
                if cleanup.terminal_kind is not None:
                    result = self._finish(cleanup.resume, AdaptiveResult(ConfigResolution(cleanup.terminal_kind)))
                else:
                    resumed = self._resume_verified(cleanup.resume)
                    result = resumed if resumed.kind is ResolutionKind.CLEANUP_REQUIRED else self._finish(cleanup.resume, resumed)
            elif cleanup.request_url is not None and cleanup.task_key is not None:
                result = self.resolve(cleanup.request_url, cleanup.task_key)
            else:
                result = self._failure("verification_cleanup_completed")
            self._cache_terminal(token, result)
            return result

    def _hold_visible_cleanup(
        self,
        context: _ResumeContext,
        cleanup_ticket: str | None,
        *,
        resume: bool,
        terminal_kind: ResolutionKind,
    ) -> AdaptiveResult:
        if cleanup_ticket is None:
            return self._finish(context, self._failure("verification_cleanup_signal_invalid"))
        with self._guard:
            self._cleanup_tickets[cleanup_ticket] = context.ticket
            self._cleanup_contexts[cleanup_ticket] = _CleanupContext(
                "visible", "visible", resume=context, terminal_kind=None if resume else terminal_kind
            )
        result = self._cleanup_required(cleanup_ticket, "visible")
        context.last_result = result
        return result

    def _hold_start_cleanup(self, ticket: VerificationTicket, request_key: tuple[str, str]) -> AdaptiveResult:
        with self._guard:
            self._cleanup_tickets[ticket.token] = ticket
            self._cleanup_contexts[ticket.token] = _CleanupContext(
                "visible", "visible", request_key=request_key, terminal_kind=ResolutionKind.VERIFICATION_FAILED
            )
            self._cleanup_requests[request_key] = ticket.token
        return self._cleanup_required(ticket.token, "visible")

    def _resume_verified(self, context: _ResumeContext) -> AdaptiveResult:
        try:
            self.browser_acquirer.activate_persistent_profile(context.challenge_url, task_key=context.task_key, pages=3)
        except Exception:
            try:
                self.browser_acquirer.deactivate_persistent_profile(context.challenge_url, task_key=context.task_key)
            except Exception:
                return self._hold_profile_cleanup(context, self._failure("verification_profile_failed"))
            return self._failure("verification_profile_failed")
        try:
            resolved = self._resolve_manager(context.request_url, context.task_key)
        except BrowserCleanupRequired as cleanup:
            operation = "headless"
            try:
                self.browser_acquirer.deactivate_persistent_profile(context.challenge_url, task_key=context.task_key)
            except Exception:
                operation = "headless_profile"
            with self._guard:
                self._cleanup_contexts[cleanup.token] = _CleanupContext(
                    "headless",
                    operation,
                    context.challenge_url if operation == "headless_profile" else None,
                    context.task_key if operation == "headless_profile" else None,
                    resume=context,
                )
            result = self._cleanup_required(cleanup.token, "headless")
            context.last_result = result
            return result
        except VerificationRequired:
            result = self._failure("verification_persisted_challenge")
        except Exception:
            result = self._failure("verification_resume_failed")
        else:
            result = AdaptiveResult(resolved)
        try:
            self.browser_acquirer.deactivate_persistent_profile(context.challenge_url, task_key=context.task_key)
        except Exception:
            return self._hold_profile_cleanup(context, result)
        return result

    def _hold_profile_cleanup(self, context: _ResumeContext, result_after_cleanup: AdaptiveResult) -> AdaptiveResult:
        cleanup_token = secrets.token_urlsafe(32)
        with self._guard:
            self._cleanup_contexts[cleanup_token] = _CleanupContext(
                "headless",
                "profile",
                context.challenge_url,
                context.task_key,
                resume=context,
                result_after_cleanup=result_after_cleanup,
            )
        cleanup_result = self._cleanup_required(cleanup_token, "headless", "profile_cleanup_required")
        context.last_result = cleanup_result
        return cleanup_result

    def _finish(self, context: _ResumeContext, result: AdaptiveResult) -> AdaptiveResult:
        context.last_result = result
        context.terminal = True
        with self._guard:
            token = context.ticket.token
            self._contexts.pop(token, None)
            self._requests.pop(context.request_key, None)
            self._cache_terminal(token, result)
        return result

    def _cache_terminal(self, token: str, result: AdaptiveResult) -> None:
        with self._guard:
            self._purge_terminal()
            self._terminal[token] = (datetime.now(UTC) + self._terminal_ttl, result)
            while len(self._terminal) > self._max_terminal:
                self._terminal.popitem(last=False)

    def _terminal_result(self, token: str) -> AdaptiveResult | None:
        self._purge_terminal()
        stored = self._terminal.get(token)
        return stored[1] if stored is not None else None

    def _purge_terminal(self) -> None:
        now = datetime.now(UTC)
        for token, (expires_at, _) in tuple(self._terminal.items()):
            if expires_at <= now:
                self._terminal.pop(token, None)

    def _resolve_manager(self, url: str, task_key: str) -> ConfigResolution:
        scope = getattr(self.browser_acquirer, "resolution_scope", None)
        guard = scope(task_key) if callable(scope) else nullcontext()
        with guard:
            return self.config_manager.resolve(url)

    @staticmethod
    def _waiting(ticket: VerificationTicket) -> AdaptiveResult:
        resolution = ConfigResolution(ResolutionKind.WAITING_FOR_USER, reason_ids=("verification_required",))
        return AdaptiveResult(resolution, ticket)

    @staticmethod
    def _failure(reason: str, ticket: VerificationTicket | None = None) -> AdaptiveResult:
        del ticket
        return AdaptiveResult(ConfigResolution(ResolutionKind.VERIFICATION_FAILED, reason_ids=(reason,)))

    @staticmethod
    def _cleanup_required(token: str, source: str, reason: str = "cleanup_required") -> AdaptiveResult:
        return AdaptiveResult(
            ConfigResolution(ResolutionKind.CLEANUP_REQUIRED, reason_ids=(reason,)),
            cleanup_ticket=token,
            cleanup_source=source,
        )

    @staticmethod
    def _from_status(status: VerificationStatus) -> AdaptiveResult:
        mapping = {
            VerificationStatus.CANCELLED: ResolutionKind.CANCELLED,
            VerificationStatus.TIMED_OUT: ResolutionKind.TIMED_OUT,
            VerificationStatus.FAILED: ResolutionKind.VERIFICATION_FAILED,
        }
        kind = mapping.get(status, ResolutionKind.VERIFICATION_FAILED)
        return AdaptiveResult(ConfigResolution(kind, reason_ids=(f"verification_{status.value}",)))

    @staticmethod
    def _token(ticket: VerificationTicket | str) -> str:
        return ticket.token if isinstance(ticket, VerificationTicket) else ticket

    @staticmethod
    def _wire_acquirer(manager: object, acquirer: object) -> None:
        for name in ("probe", "revalidator"):
            collaborator = getattr(manager, name, None)
            if collaborator is not None and hasattr(collaborator, "acquirer"):
                collaborator.acquirer = acquirer


__all__ = ["AdaptiveBrowserService", "AdaptiveResult"]

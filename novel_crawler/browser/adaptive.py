"""Adapt configuration resolution around bounded interactive verification."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from novel_crawler.acquisition.security import redact_url
from novel_crawler.adaptation.config_manager import ConfigResolution, ResolutionKind

from .coordinator import BrowserAcquirer, VerificationCoordinator, VerificationRequired
from .models import VerificationOutcome, VerificationStatus, VerificationTicket


class _Manager(Protocol):
    def resolve(self, url: str) -> ConfigResolution: ...


class _Acquirer(Protocol):
    def activate_persistent_profile(self, url: str, *, task_key: str, pages: int) -> None: ...
    def deactivate_persistent_profile(self, url: str, *, task_key: str) -> None: ...


class _Coordinator(Protocol):
    def begin(self, url: str, *, task_key: str) -> VerificationTicket: ...
    def continue_verification(self, token: str) -> VerificationOutcome: ...
    def cancel(self, token: str) -> VerificationOutcome: ...
    def expire_sweep(self) -> int: ...
    def retry_cleanup(self, token: str) -> bool: ...


class AdaptiveResult:
    """Immutable resolution wrapper with explicit access to sensitive handles."""

    __slots__ = ("_resolution", "_ticket")

    def __init__(self, resolution: ConfigResolution, ticket: VerificationTicket | None = None) -> None:
        if resolution.kind is ResolutionKind.WAITING_FOR_USER:
            if ticket is None:
                raise ValueError("waiting results require a ticket")
        elif ticket is not None and resolution.kind is not ResolutionKind.VERIFICATION_FAILED:
            raise ValueError("only waiting and failed verification results may contain a ticket")
        object.__setattr__(self, "_resolution", resolution)
        object.__setattr__(self, "_ticket", ticket)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("AdaptiveResult is immutable")

    resolution = property(lambda self: self._resolution)
    kind = property(lambda self: self._resolution.kind)
    config = property(lambda self: self._resolution.config)
    confirmation_token = property(lambda self: self._resolution.confirmation_token)
    reason_ids = property(lambda self: self._resolution.reason_ids)
    ticket = property(lambda self: self._ticket)

    def __repr__(self) -> str:
        return (
            f"AdaptiveResult(kind={self.kind.value!r}, config_present={self.config is not None!r}, "
            f"confirmation_required={self.confirmation_token is not None!r}, ticket_present={self.ticket is not None!r}, "
            f"reason_ids={self.reason_ids!r})"
        )

    def to_dict(self) -> dict[str, object]:
        value = self.resolution.to_dict()
        value["ticket_present"] = self.ticket is not None
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
        self._terminal: OrderedDict[str, tuple[datetime, AdaptiveResult]] = OrderedDict()
        self._terminal_ttl = timedelta(minutes=10)
        self._max_terminal = 1024
        self._wire_acquirer(config_manager, browser_acquirer)
        if isinstance(browser_acquirer, BrowserAcquirer):
            browser_acquirer.coordinator = None

    def resolve(self, url: str, task_key: str) -> AdaptiveResult:
        key = (redact_url(url), task_key)
        with self._guard:
            token = self._requests.get(key)
            if token is not None and (context := self._contexts.get(token)) is not None:
                return context.last_result or self._waiting(context.ticket)
        try:
            return AdaptiveResult(self._resolve_manager(url, task_key))
        except VerificationRequired as required:
            original = required.original_url or url
            with self._guard:
                token = self._requests.get(key)
                if token is not None and (context := self._contexts.get(token)) is not None:
                    return context.last_result or self._waiting(context.ticket)
                try:
                    ticket = required.ticket or self.verification_coordinator.begin(original, task_key=task_key)
                except VerificationRequired as exc:
                    return self._failure(exc.code)
                if ticket.status is not VerificationStatus.WAITING:
                    retry_cleanup = getattr(self.verification_coordinator, "retry_cleanup", None)
                    if callable(retry_cleanup):
                        cleaned = False
                        for _ in range(2):
                            try:
                                cleaned = bool(retry_cleanup(ticket.token))
                            except VerificationRequired:
                                break
                            if cleaned:
                                break
                        if not cleaned:
                            self._cleanup_tickets[ticket.token] = ticket
                            return self._failure("verification_cleanup_pending", ticket)
                    return self._from_status(ticket.status)
                context = _ResumeContext(ticket, url, original, task_key, key)
                result = self._waiting(ticket)
                context.last_result = result
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
                del outcome
                try:
                    self.browser_acquirer.activate_persistent_profile(context.challenge_url, task_key=context.task_key, pages=3)
                    try:
                        resolved = self._resolve_manager(context.request_url, context.task_key)
                    except VerificationRequired:
                        result = self._failure("verification_persisted_challenge")
                    except Exception:
                        result = self._failure("verification_resume_failed")
                    else:
                        result = AdaptiveResult(resolved)
                except Exception:
                    result = self._failure("verification_profile_failed")
                finally:
                    deactivate = getattr(self.browser_acquirer, "deactivate_persistent_profile", None)
                    if callable(deactivate):
                        try:
                            deactivate(context.challenge_url, task_key=context.task_key)
                        except Exception:
                            result = self._failure("verification_profile_cleanup_failed")
                return self._finish(context, result)
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
            if context.terminal:
                assert context.last_result is not None
                return context.last_result
            try:
                outcome = self.verification_coordinator.cancel(token)
            except VerificationRequired as exc:
                result = self._failure(exc.code)
            else:
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
            pending = self._cleanup_tickets.get(token)
        if pending is None:
            return self._failure("verification_token_invalid")
        try:
            cleaned = self.verification_coordinator.retry_cleanup(token)
        except VerificationRequired as exc:
            return self._failure(exc.code, pending)
        if not cleaned:
            return self._failure("verification_cleanup_pending", pending)
        with self._guard:
            self._cleanup_tickets.pop(token, None)
        return self._failure("verification_cleanup_completed")

    def _finish(self, context: _ResumeContext, result: AdaptiveResult) -> AdaptiveResult:
        context.last_result = result
        context.terminal = True
        with self._guard:
            token = context.ticket.token
            self._contexts.pop(token, None)
            self._requests.pop(context.request_key, None)
            self._purge_terminal()
            self._terminal[token] = (datetime.now(UTC) + self._terminal_ttl, result)
            while len(self._terminal) > self._max_terminal:
                self._terminal.popitem(last=False)
        return result

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
        return AdaptiveResult(ConfigResolution(ResolutionKind.VERIFICATION_FAILED, reason_ids=(reason,)), ticket)

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

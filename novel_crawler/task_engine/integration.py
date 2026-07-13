"""Privacy-safe bridge between adaptive acquisition and persistent task state."""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from novel_crawler.adaptation.config_manager import ConfigResolution, ResolutionKind
from novel_crawler.browser.adaptive import AdaptiveResult
from novel_crawler.browser.models import VerificationTicket
from novel_crawler.task_engine.models import TERMINAL_STATUSES, TaskRecord, TaskStatus
from novel_crawler.task_engine.repository import (
    InvalidTaskTransition,
    TaskRepository,
    TaskVersionConflict,
)

_UNSAFE_SELECTOR = re.compile(r"(?:https?://|[\x00-\x1f\x7f])", re.I)


class _Manager(Protocol):
    def confirm(
        self, token: str, selector_overrides: Mapping[str, str] | None = None
    ) -> ConfigResolution: ...

    def cancel(self, token: str) -> bool: ...


class _Adaptive(Protocol):
    config_manager: _Manager

    def resolve(self, url: str, task_key: str) -> AdaptiveResult: ...
    def continue_verification(self, ticket: VerificationTicket | str) -> AdaptiveResult: ...
    def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult: ...
    def retry_cleanup(self, ticket: VerificationTicket | str) -> AdaptiveResult: ...
    def expire_sweep(self) -> int: ...


class InteractionKind(StrEnum):
    VERIFICATION = "verification"
    CONFIRMATION = "confirmation"
    CLEANUP = "cleanup"
    CANCEL_PENDING = "cancel_pending"


@dataclass(frozen=True)
class InteractionSummary:
    kind: InteractionKind
    safe_origin: str = "<not-applicable>"
    attempt: int = 0
    expires_at: str | None = None
    cleanup_source: str | None = None
    verification_required: bool = False
    confirmation_required: bool = False
    cleanup_required: bool = False

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "safe_origin": self.safe_origin,
            "attempt": self.attempt,
            "expires_at": self.expires_at,
            "cleanup_source": self.cleanup_source,
            "verification_required": self.verification_required,
            "confirmation_required": self.confirmation_required,
            "cleanup_required": self.cleanup_required,
        }


@dataclass
class _PrivateInteraction:
    kind: InteractionKind
    handle: VerificationTicket | str = field(repr=False)
    summary: InteractionSummary
    deadline: float = field(repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    original_kind: InteractionKind | None = field(default=None, repr=False)


@dataclass
class _ProbeFlight:
    condition: threading.Condition = field(repr=False)
    done: bool = False


@dataclass(frozen=True)
class _CancelAttempt:
    outcome: AdaptiveResult | None = None
    failed: bool = False


_RECOVERABLE_ERRORS = {
    ResolutionKind.TRANSIENT_FAILURE: "adaptation_transient_failure",
    ResolutionKind.TIMED_OUT: "verification_timed_out",
    ResolutionKind.VERIFICATION_FAILED: "verification_failed",
    ResolutionKind.CLEANUP_REQUIRED: "interaction_cleanup_required",
}


class AdaptiveTaskController:
    """Own bounded ephemeral handles while persisting only safe task outcomes."""

    def __init__(
        self,
        repository: TaskRepository,
        adaptive_service: _Adaptive,
        *,
        max_interactions: int = 256,
        interaction_ttl_seconds: float = 600.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        scheduler: Callable[[str], bool] | None = None,
    ) -> None:
        if isinstance(max_interactions, bool) or not isinstance(max_interactions, int) or not 1 <= max_interactions <= 10_000:
            raise ValueError("max_interactions must be between 1 and 10000")
        if not 1 <= interaction_ttl_seconds <= 86_400:
            raise ValueError("interaction_ttl_seconds must be between 1 and 86400")
        self._repository = repository
        self._adaptive = adaptive_service
        self._max_interactions = max_interactions
        self._max_emergency_cleanup = max_interactions
        self._ttl = interaction_ttl_seconds
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._scheduler = scheduler
        self._owner_id = uuid.uuid4().hex
        self._owner_epoch = time.time_ns()
        self._lock = threading.RLock()
        self._interactions: OrderedDict[str, _PrivateInteraction] = OrderedDict()
        self._late_resume_status: dict[str, TaskStatus] = {}
        self._emergency_cleanup_ids: set[str] = set()
        self._probe_flights: dict[str, _ProbeFlight] = {}
        self.recovered_orphans = self._recover_orphaned_waiting()

    def bind_scheduler(self, scheduler: Callable[[str], bool]) -> None:
        if not callable(scheduler):
            raise TypeError("scheduler must be callable")
        with self._lock:
            self._scheduler = scheduler

    def adopt_acquisition_result(self, task_id: str, outcome: AdaptiveResult) -> TaskRecord:
        """Adopt a late browser handle without persisting the private token."""
        if outcome.kind not in {
            ResolutionKind.WAITING_FOR_USER,
            ResolutionKind.CLEANUP_REQUIRED,
            ResolutionKind.VERIFICATION_FAILED,
        }:
            raise ValueError("late acquisition result is invalid")
        current = self._repository.get_task(task_id)
        if current.status not in {TaskStatus.VALIDATING, TaskStatus.CRAWLING}:
            return self._compensate_unadopted(current, outcome)
        with self._lock:
            self._late_resume_status[task_id] = current.status
        return self._apply_result(current, outcome)

    def capture_acquisition_result(
        self,
        task_id: str,
        expected_version: int,
        capture: Callable[[], AdaptiveResult],
    ) -> TaskRecord:
        """Reserve capacity, capture a private handle, then CAS-adopt or abort it."""
        reserved = self._reserve_capacity(task_id)
        try:
            before = self._repository.get_task(task_id)
            eligible = (
                before.version == expected_version
                and before.status in {TaskStatus.VALIDATING, TaskStatus.CRAWLING}
            )
            outcome = capture()
            after = self._repository.get_task(task_id)
            if (
                not reserved
                or not eligible
                or after.version != expected_version
                or after.status is not before.status
            ):
                return self._compensate_unadopted(after, outcome)
            with self._lock:
                self._late_resume_status[task_id] = after.status
            return self._apply_result(after, outcome)
        finally:
            self._release_reservation(task_id)

    def _compensate_unadopted(
        self, current: TaskRecord, outcome: AdaptiveResult
    ) -> TaskRecord:
        try:
            if outcome.kind is ResolutionKind.WAITING_FOR_USER:
                assert outcome.ticket is not None
                compensated = self._adaptive.cancel(outcome.ticket)
            elif outcome.kind is ResolutionKind.CLEANUP_REQUIRED:
                assert outcome.cleanup_ticket is not None
                compensated = self._adaptive.retry_cleanup(outcome.cleanup_ticket)
            else:
                return current
        except Exception:
            if outcome.kind is ResolutionKind.WAITING_FOR_USER:
                return self._retain_failed_late_cancel(current, outcome)
            compensated = outcome
        if compensated.kind is not ResolutionKind.CLEANUP_REQUIRED:
            return current
        self._store_result_handle(current.task_id, compensated)
        if current.status in TERMINAL_STATUSES:
            return current
        try:
            return self._repository.require_cleanup(
                current.task_id,
                expected_version=current.version,
                error_code="interaction_cleanup_required",
            )
        except (TaskVersionConflict, InvalidTaskTransition):
            return self._repository.get_task(current.task_id)

    def _retain_failed_late_cancel(
        self, current: TaskRecord, outcome: AdaptiveResult
    ) -> TaskRecord:
        assert outcome.ticket is not None
        with self._lock:
            if (
                current.task_id not in self._emergency_cleanup_ids
                and len(self._emergency_cleanup_ids) >= self._max_emergency_cleanup
            ):
                raise RuntimeError("emergency_cleanup_capacity")
            self._emergency_cleanup_ids.add(current.task_id)
        expiry = self._wall_clock() + timedelta(seconds=self._ttl)
        interaction = _PrivateInteraction(
            InteractionKind.VERIFICATION,
            outcome.ticket,
            InteractionSummary(
                InteractionKind.VERIFICATION,
                outcome.ticket.safe_origin,
                outcome.ticket.attempt,
                expiry.isoformat(),
                verification_required=True,
            ),
            self._monotonic() + self._ttl,
        )
        return self._enter_cancel_pending(current.task_id, current, interaction)

    def __repr__(self) -> str:
        with self._lock:
            counts = {kind.value: 0 for kind in InteractionKind}
            for interaction in self._interactions.values():
                counts[interaction.kind.value] += 1
        return f"AdaptiveTaskController(active_count={sum(counts.values())}, kinds={counts!r})"

    def probe_handler(self, _context: object, task: TaskRecord) -> None:
        current = self._repository.get_task(task.task_id)
        if (
            task.status is not TaskStatus.PROBING
            or current.status is not TaskStatus.PROBING
            or current.version != task.version
        ):
            return None
        with self._lock:
            existing = self._probe_flights.get(task.task_id)
            if existing is not None:
                while not existing.done:
                    existing.condition.wait()
                return None
            flight = _ProbeFlight(threading.Condition(self._lock))
            self._probe_flights[task.task_id] = flight
        if not self._reserve_capacity(task.task_id):
            self._transition(task, TaskStatus.RECOVERABLE_FAILED, "interaction_capacity_exceeded")
            self._finish_probe(task.task_id, flight)
            return None
        try:
            latest = self._repository.get_task(task.task_id)
            if latest.status is not TaskStatus.PROBING or latest.version != task.version:
                return None
            try:
                outcome = self._adaptive.resolve(task.source_url, task.task_id)
            except Exception:
                outcome = AdaptiveResult(
                    ConfigResolution(
                        ResolutionKind.VERIFICATION_FAILED,
                        reason_ids=("adaptive_service_failed",),
                    )
                )
                self._apply_result(task, outcome, error_override="adaptive_service_failed")
                return None
            self._apply_result(task, outcome)
            return None
        finally:
            self._release_reservation(task.task_id)
            self._finish_probe(task.task_id, flight)

    def _finish_probe(self, task_id: str, flight: _ProbeFlight) -> None:
        with self._lock:
            flight.done = True
            self._probe_flights.pop(task_id, None)
            flight.condition.notify_all()

    def interaction(self, task_id: str) -> InteractionSummary | None:
        self._clear_if_terminal(task_id)
        with self._lock:
            interaction = self._interactions.get(task_id)
            return interaction.summary if interaction is not None else None

    def close(self) -> bool:
        """Release owned leases after safely cancelling or cleaning local interactions."""
        with self._lock:
            task_ids = tuple(self._interactions)
        complete = True
        for task_id in task_ids:
            if task_id.startswith("\0"):
                continue
            with self._lock:
                interaction = self._interactions.get(task_id)
            if interaction is None:
                continue
            if interaction.kind in {InteractionKind.CLEANUP, InteractionKind.CANCEL_PENDING}:
                result = self.retry_cleanup(task_id)
                if self.interaction(task_id) is not None:
                    complete = False
                    continue
                del result
            else:
                attempt = self._best_effort_cancel(interaction)
                if attempt.failed:
                    current = self._repository.get_task(task_id)
                    self._enter_cancel_pending(task_id, current, interaction)
                    complete = False
                    continue
                if attempt.outcome is not None and attempt.outcome.kind is ResolutionKind.CLEANUP_REQUIRED:
                    current = self._repository.get_task(task_id)
                    self._apply_result(current, attempt.outcome)
                    complete = False
                    continue
            self._release_lease(task_id)
            self._drop(task_id)
        return complete

    def continue_verification(self, task_id: str) -> TaskRecord:
        return self._act(task_id, InteractionKind.VERIFICATION, self._adaptive.continue_verification)

    def confirm_config(
        self, task_id: str, selector_overrides: Mapping[str, str] | None = None
    ) -> TaskRecord:
        overrides = _validate_overrides(selector_overrides)

        def confirm(handle: VerificationTicket | str) -> AdaptiveResult:
            token = handle.token if isinstance(handle, VerificationTicket) else handle
            return AdaptiveResult(self._adaptive.config_manager.confirm(token, overrides))

        return self._act(task_id, InteractionKind.CONFIRMATION, confirm)

    def cancel_interaction(self, task_id: str) -> TaskRecord:
        with self._lock:
            interaction = self._interactions.get(task_id)
        if interaction is None:
            return self._repository.get_task(task_id)
        with interaction.lock:
            with self._lock:
                if self._interactions.get(task_id) is not interaction:
                    return self._repository.get_task(task_id)
            if not self._owns_lease(task_id):
                return self._repository.get_task(task_id)
            leased = self._repository.get_task(task_id)
            if leased.status is TaskStatus.WAITING_FOR_USER:
                try:
                    if not self._repository.renew_interaction_lease(
                        task_id,
                        expected_version=leased.version,
                        owner_id=self._owner_id,
                        owner_epoch=self._owner_epoch,
                        expires_at=self._lease_expiry(),
                    ):
                        return self._repository.get_task(task_id)
                except (TaskVersionConflict, InvalidTaskTransition):
                    return self._repository.get_task(task_id)
            try:
                if interaction.kind is InteractionKind.VERIFICATION:
                    outcome = self._adaptive.cancel(interaction.handle)
                    current = self._repository.get_task(task_id)
                    return self._apply_result(current, outcome)
                elif interaction.kind is InteractionKind.CONFIRMATION:
                    token = interaction.handle.token if isinstance(interaction.handle, VerificationTicket) else interaction.handle
                    self._adaptive.config_manager.cancel(token)
                else:
                    return self._repository.get_task(task_id)
            except Exception:
                current = self._repository.get_task(task_id)
                return self._enter_cancel_pending(task_id, current, interaction)
            current = self._repository.get_task(task_id)
            result = self._transition(current, TaskStatus.CANCELLED, "interaction_cancelled")
            self._release_lease(task_id)
            self._drop(task_id)
            return result

    def retry_cleanup(self, task_id: str) -> TaskRecord:
        with self._lock:
            interaction = self._interactions.get(task_id)
        if interaction is None or interaction.kind not in {
            InteractionKind.CLEANUP,
            InteractionKind.CANCEL_PENDING,
        }:
            return self._repository.get_task(task_id)
        with interaction.lock:
            with self._lock:
                if self._interactions.get(task_id) is not interaction:
                    return self._repository.get_task(task_id)
            if interaction.kind is InteractionKind.CANCEL_PENDING:
                return self._retry_cancel_pending_locked(task_id, interaction)
            return self._retry_cleanup_locked(task_id, interaction)

    retry_cancel_cleanup = retry_cleanup

    def _enter_cancel_pending(
        self,
        task_id: str,
        current: TaskRecord,
        interaction: _PrivateInteraction,
    ) -> TaskRecord:
        self._store_cancel_pending(task_id, interaction)
        if current.status in TERMINAL_STATUSES:
            return current
        try:
            return self._repository.require_cleanup(
                task_id,
                expected_version=current.version,
                error_code="interaction_cleanup_required",
            )
        except (TaskVersionConflict, InvalidTaskTransition):
            return self._repository.get_task(task_id)

    def _retry_cancel_pending_locked(
        self, task_id: str, interaction: _PrivateInteraction
    ) -> TaskRecord:
        try:
            outcome = self._cancel_pending_operation(interaction)
        except Exception:
            interaction.deadline = self._monotonic() + self._ttl
            return self._repository.get_task(task_id)
        current = self._repository.get_task(task_id)
        if outcome.kind is ResolutionKind.CLEANUP_REQUIRED:
            self._store_result_handle(task_id, outcome)
            return current
        if current.cleanup_required:
            try:
                resumed = self._repository.complete_cleanup_gate(
                    task_id, expected_version=current.version
                )
            except (TaskVersionConflict, InvalidTaskTransition):
                return self._repository.get_task(task_id)
            self._drop(task_id)
            return self._apply_result(resumed, outcome)
        self._drop(task_id)
        return self._apply_result(current, outcome)

    def _cancel_pending_operation(self, interaction: _PrivateInteraction) -> AdaptiveResult:
        if interaction.original_kind is InteractionKind.CONFIRMATION:
            token = (
                interaction.handle.token
                if isinstance(interaction.handle, VerificationTicket)
                else interaction.handle
            )
            # False is the ConfigManager's idempotent "already absent" result.
            self._adaptive.config_manager.cancel(token)
            return AdaptiveResult(ConfigResolution(ResolutionKind.CANCELLED))
        return self._adaptive.cancel(interaction.handle)

    def sweep(self) -> int:
        try:
            self._adaptive.expire_sweep()
        except Exception:
            pass
        with self._lock:
            active_ids = tuple(self._interactions)
        for task_id in active_ids:
            if task_id.startswith("\0"):
                continue
            try:
                terminal = self._repository.get_task(task_id).status in TERMINAL_STATUSES
            except Exception:
                terminal = True
            if terminal:
                with self._lock:
                    interaction = self._interactions.get(task_id)
                if interaction is not None and interaction.kind in {
                    InteractionKind.CLEANUP,
                    InteractionKind.CANCEL_PENDING,
                }:
                    with interaction.lock:
                        if interaction.kind is InteractionKind.CANCEL_PENDING:
                            self._retry_cancel_pending_locked(task_id, interaction)
                        else:
                            self._retry_cleanup_locked(task_id, interaction)
                elif interaction is not None:
                    attempt = self._best_effort_cancel(interaction)
                    if attempt.failed:
                        current = self._repository.get_task(task_id)
                        self._enter_cancel_pending(task_id, current, interaction)
                    elif attempt.outcome is not None and attempt.outcome.kind is ResolutionKind.CLEANUP_REQUIRED:
                        self._store_result_handle(task_id, attempt.outcome)
                        with self._lock:
                            cleanup = self._interactions.get(task_id)
                        if cleanup is not None:
                            with cleanup.lock:
                                self._retry_cleanup_locked(task_id, cleanup)
                    else:
                        self._release_lease(task_id)
                        self._drop(task_id)
        now = self._monotonic()
        with self._lock:
            expired = [
                (task_id, interaction)
                for task_id, interaction in self._interactions.items()
                if interaction.deadline <= now
            ]
        swept = 0
        for task_id, interaction in expired:
            with interaction.lock:
                with self._lock:
                    if self._interactions.get(task_id) is not interaction:
                        continue
                if interaction.kind in {InteractionKind.CLEANUP, InteractionKind.CANCEL_PENDING}:
                    if interaction.kind is InteractionKind.CANCEL_PENDING:
                        self._retry_cancel_pending_locked(task_id, interaction)
                    else:
                        self._retry_cleanup_locked(task_id, interaction)
                    swept += 1
                    continue
                cancel_attempt = self._best_effort_cancel(interaction)
                task = self._repository.get_task(task_id)
                if cancel_attempt.failed:
                    self._enter_cancel_pending(task_id, task, interaction)
                    swept += 1
                    continue
                if (
                    cancel_attempt.outcome is not None
                    and cancel_attempt.outcome.kind is ResolutionKind.CLEANUP_REQUIRED
                ):
                    self._apply_result(task, cancel_attempt.outcome)
                    swept += 1
                    continue
                if task.status is TaskStatus.WAITING_FOR_USER:
                    self._release_lease(task_id)
                    try:
                        self._repository.recover_lost_interaction(
                            task_id,
                            expected_version=task.version,
                            error_code="interaction_expired",
                            now=self._wall_clock().isoformat(),
                        )
                    except (TaskVersionConflict, InvalidTaskTransition):
                        pass
                self._drop(task_id)
                swept += 1
        return swept

    def _retry_cleanup_locked(
        self, task_id: str, interaction: _PrivateInteraction
    ) -> TaskRecord:
        try:
            outcome = self._adaptive.retry_cleanup(interaction.handle)
        except Exception:
            interaction.deadline = self._monotonic() + self._ttl
            return self._repository.get_task(task_id)
        current = self._repository.get_task(task_id)
        if outcome.kind is ResolutionKind.CLEANUP_REQUIRED:
            self._store_result_handle(task_id, outcome)
            return current
        if current.cleanup_required:
            try:
                resumed = self._repository.complete_cleanup_gate(
                    task_id, expected_version=current.version
                )
            except (TaskVersionConflict, InvalidTaskTransition):
                return self._repository.get_task(task_id)
            self._drop(task_id)
            return self._apply_result(resumed, outcome)
        return self._apply_result(current, outcome)

    def _act(
        self,
        task_id: str,
        expected_kind: InteractionKind,
        operation: Callable[[VerificationTicket | str], AdaptiveResult],
    ) -> TaskRecord:
        with self._lock:
            interaction = self._interactions.get(task_id)
        if interaction is None or interaction.kind is not expected_kind:
            return self._repository.get_task(task_id)
        with interaction.lock:
            with self._lock:
                if self._interactions.get(task_id) is not interaction:
                    return self._repository.get_task(task_id)
            current = self._repository.get_task(task_id)
            if current.status is not TaskStatus.WAITING_FOR_USER:
                return current
            if not self._owns_lease(task_id):
                return current
            try:
                outcome = operation(interaction.handle)
            except Exception:
                outcome = AdaptiveResult(
                    ConfigResolution(
                        ResolutionKind.VERIFICATION_FAILED,
                        reason_ids=("interaction_operation_failed",),
                    )
                )
            return self._apply_result(current, outcome)

    def _apply_result(
        self,
        task: TaskRecord,
        outcome: AdaptiveResult,
        *,
        error_override: str | None = None,
    ) -> TaskRecord:
        kind = outcome.kind
        if kind in {ResolutionKind.REUSED, ResolutionKind.REGISTERED}:
            with self._lock:
                target = self._late_resume_status.pop(
                    task.task_id, TaskStatus.VALIDATING
                )
            result = task if task.status is target else self._transition(task, target, None)
            self._release_lease(task.task_id)
            self._drop(task.task_id)
            return self._schedule_or_fail(result)
        if kind in {ResolutionKind.WAITING_FOR_USER, ResolutionKind.CONFIRMATION_REQUIRED}:
            expires_at = self._lease_expiry()
            try:
                if task.status is TaskStatus.WAITING_FOR_USER:
                    if not self._repository.renew_interaction_lease(
                        task.task_id,
                        expected_version=task.version,
                        owner_id=self._owner_id,
                        owner_epoch=self._owner_epoch,
                        expires_at=expires_at,
                    ):
                        return self._repository.get_task(task.task_id)
                    transitioned = task
                else:
                    transitioned = self._repository.transition_to_waiting_with_lease(
                        task.task_id,
                        expected_version=task.version,
                        owner_id=self._owner_id,
                        owner_epoch=self._owner_epoch,
                        expires_at=expires_at,
                    )
            except (TaskVersionConflict, InvalidTaskTransition):
                self._store_result_handle(task.task_id, outcome)
                with self._lock:
                    abandoned = self._interactions.get(task.task_id)
                if abandoned is not None:
                    cancel_attempt = self._best_effort_cancel(abandoned)
                    if (
                        cancel_attempt.outcome is not None
                        and cancel_attempt.outcome.kind is ResolutionKind.CLEANUP_REQUIRED
                    ):
                        self._store_result_handle(task.task_id, cancel_attempt.outcome)
                    elif cancel_attempt.failed:
                        self._store_cancel_pending(task.task_id, abandoned)
                    else:
                        self._drop(task.task_id)
                return self._repository.get_task(task.task_id)
            self._store_result_handle(task.task_id, outcome)
            return transitioned
        if kind is ResolutionKind.REJECTED:
            with self._lock:
                self._late_resume_status.pop(task.task_id, None)
            result = self._transition(task, TaskStatus.TERMINAL_FAILED, "adaptation_rejected")
            self._release_lease(task.task_id)
            self._drop(task.task_id)
            return result
        if kind is ResolutionKind.CANCELLED:
            with self._lock:
                self._late_resume_status.pop(task.task_id, None)
            result = self._transition(task, TaskStatus.CANCELLED, "interaction_cancelled")
            self._release_lease(task.task_id)
            self._drop(task.task_id)
            return result
        error = error_override or _RECOVERABLE_ERRORS.get(kind, "adaptive_resolution_invalid")
        if kind is ResolutionKind.CLEANUP_REQUIRED:
            self._store_result_handle(task.task_id, outcome)
            if task.status is TaskStatus.WAITING_FOR_USER:
                self._release_lease(task.task_id)
            try:
                return self._repository.require_cleanup(
                    task.task_id,
                    expected_version=task.version,
                    error_code=error,
                )
            except (TaskVersionConflict, InvalidTaskTransition):
                return self._repository.get_task(task.task_id)
        elif task.status is TaskStatus.WAITING_FOR_USER:
            self._drop(task.task_id)
        if task.status is TaskStatus.WAITING_FOR_USER:
            self._release_lease(task.task_id)
            try:
                return self._repository.recover_lost_interaction(
                    task.task_id,
                    expected_version=task.version,
                    error_code=error,
                    now=self._wall_clock().isoformat(),
                )
            except (TaskVersionConflict, InvalidTaskTransition):
                return self._repository.get_task(task.task_id)
        with self._lock:
            self._late_resume_status.pop(task.task_id, None)
        return self._transition(task, TaskStatus.RECOVERABLE_FAILED, error)

    def _schedule_or_fail(self, task: TaskRecord) -> TaskRecord:
        if task.status not in {TaskStatus.PROBING, TaskStatus.VALIDATING, TaskStatus.CRAWLING}:
            return task
        with self._lock:
            scheduler = self._scheduler
        if scheduler is None:
            return task
        try:
            scheduler(task.task_id)
            return task
        except Exception:
            try:
                return self._repository.transition(
                    task.task_id,
                    TaskStatus.RECOVERABLE_FAILED,
                    expected_version=task.version,
                    reason="adaptive_executor_handoff_failed",
                    error_code="executor_unavailable",
                )
            except (TaskVersionConflict, InvalidTaskTransition):
                return self._repository.get_task(task.task_id)

    def _transition(
        self, task: TaskRecord, target: TaskStatus, error_code: str | None
    ) -> TaskRecord:
        try:
            return self._repository.transition(
                task.task_id,
                target,
                expected_version=task.version,
                reason="adaptive_task_resolution",
                error_code=error_code,
            )
        except (TaskVersionConflict, InvalidTaskTransition):
            return self._repository.get_task(task.task_id)

    def _store_result_handle(self, task_id: str, outcome: AdaptiveResult) -> None:
        now = self._monotonic()
        wall_now = self._wall_clock()
        expiry = wall_now + timedelta(seconds=self._ttl)
        if outcome.kind is ResolutionKind.WAITING_FOR_USER:
            assert outcome.ticket is not None
            ticket_expiry = outcome.ticket.expires_at
            summary = InteractionSummary(
                InteractionKind.VERIFICATION,
                outcome.ticket.safe_origin,
                outcome.ticket.attempt,
                ticket_expiry.isoformat() if ticket_expiry is not None else expiry.isoformat(),
                verification_required=True,
            )
            handle: VerificationTicket | str = outcome.ticket
            if ticket_expiry is not None:
                remaining = max(0.0, (ticket_expiry - wall_now).total_seconds())
                deadline = now + min(self._ttl, remaining)
            else:
                deadline = now + self._ttl
        elif outcome.kind is ResolutionKind.CONFIRMATION_REQUIRED:
            assert outcome.confirmation_token is not None
            summary = InteractionSummary(
                InteractionKind.CONFIRMATION,
                expires_at=expiry.isoformat(),
                confirmation_required=True,
            )
            handle = outcome.confirmation_token
            deadline = now + self._ttl
        else:
            assert outcome.kind is ResolutionKind.CLEANUP_REQUIRED and outcome.cleanup_ticket is not None
            summary = InteractionSummary(
                InteractionKind.CLEANUP,
                expires_at=expiry.isoformat(),
                cleanup_source=outcome.cleanup_source,
                cleanup_required=True,
            )
            handle = outcome.cleanup_ticket
            deadline = now + self._ttl
        with self._lock:
            self._interactions.pop("\0" + task_id, None)
            old = self._interactions.get(task_id)
            lock = old.lock if old is not None else threading.Lock()
            self._interactions[task_id] = _PrivateInteraction(
                summary.kind, handle, summary, deadline, lock
            )
            self._interactions.move_to_end(task_id)

    def _store_cancel_pending(
        self, task_id: str, interaction: _PrivateInteraction
    ) -> None:
        expiry = self._wall_clock() + timedelta(seconds=self._ttl)
        summary = InteractionSummary(
            InteractionKind.CANCEL_PENDING,
            safe_origin=interaction.summary.safe_origin,
            attempt=interaction.summary.attempt,
            expires_at=expiry.isoformat(),
            cleanup_source="visible",
            cleanup_required=True,
        )
        with self._lock:
            self._interactions[task_id] = _PrivateInteraction(
                InteractionKind.CANCEL_PENDING,
                interaction.handle,
                summary,
                self._monotonic() + self._ttl,
                interaction.lock,
                original_kind=(
                    interaction.original_kind
                    if interaction.kind is InteractionKind.CANCEL_PENDING
                    else interaction.kind
                ),
            )
            self._interactions.move_to_end(task_id)

    def _reserve_capacity(self, task_id: str) -> bool:
        self.sweep()
        with self._lock:
            if task_id in self._interactions:
                return True
            if "\0" + task_id in self._interactions:
                return True
            if len(self._interactions) >= self._max_interactions:
                return False
            key = "\0" + task_id
            placeholder = InteractionSummary(InteractionKind.CONFIRMATION)
            self._interactions[key] = _PrivateInteraction(
                InteractionKind.CONFIRMATION, "", placeholder, float("inf")
            )
            return True

    def _release_reservation(self, task_id: str) -> None:
        with self._lock:
            self._interactions.pop("\0" + task_id, None)

    def _drop(self, task_id: str) -> None:
        with self._lock:
            self._interactions.pop(task_id, None)
            self._emergency_cleanup_ids.discard(task_id)

    def _lease_expiry(self) -> str:
        return (self._wall_clock() + timedelta(seconds=self._ttl)).isoformat()

    def _owns_lease(self, task_id: str) -> bool:
        return self._repository.owns_interaction_lease(
            task_id,
            owner_id=self._owner_id,
            owner_epoch=self._owner_epoch,
            now=self._wall_clock().isoformat(),
        )

    def _release_lease(self, task_id: str) -> None:
        self._repository.release_interaction_lease(
            task_id, owner_id=self._owner_id, owner_epoch=self._owner_epoch
        )

    def _clear_if_terminal(self, task_id: str) -> None:
        if self._repository.get_task(task_id).status in TERMINAL_STATUSES:
            with self._lock:
                interaction = self._interactions.get(task_id)
            if interaction is None or interaction.kind not in {
                InteractionKind.CLEANUP,
                InteractionKind.CANCEL_PENDING,
            }:
                self._drop(task_id)

    def _recover_orphaned_waiting(self) -> int:
        count = 0
        while True:
            now = self._wall_clock().isoformat()
            waiting = self._repository.list_orphaned_waiting(now=now)
            if not waiting:
                break
            changed = 0
            for task in waiting:
                try:
                    self._repository.recover_lost_interaction(
                        task.task_id,
                        expected_version=task.version,
                        error_code="interaction_session_lost",
                        now=now,
                    )
                except (TaskVersionConflict, InvalidTaskTransition):
                    continue
                count += 1
                changed += 1
            if changed == 0:
                break
        return count

    def _best_effort_cancel(self, interaction: _PrivateInteraction) -> _CancelAttempt:
        try:
            if interaction.kind in {
                InteractionKind.VERIFICATION,
                InteractionKind.CANCEL_PENDING,
            }:
                if interaction.kind is InteractionKind.CANCEL_PENDING:
                    return _CancelAttempt(self._cancel_pending_operation(interaction))
                return _CancelAttempt(self._adaptive.cancel(interaction.handle))
            elif interaction.kind is InteractionKind.CONFIRMATION:
                token = interaction.handle.token if isinstance(interaction.handle, VerificationTicket) else interaction.handle
                self._adaptive.config_manager.cancel(token)
        except Exception:
            return _CancelAttempt(failed=True)
        return _CancelAttempt()


def _validate_overrides(overrides: Mapping[str, str] | None) -> dict[str, str]:
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping) or len(overrides) > 16:
        raise ValueError("selector_overrides_invalid")
    clean: dict[str, str] = {}
    for key, value in overrides.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[a-z][a-z_]{0,31}", key)
            or not isinstance(value, str)
            or not value
            or len(value) > 512
            or _UNSAFE_SELECTOR.search(value)
        ):
            raise ValueError("selector_overrides_invalid")
        clean[key] = value
    return clean


__all__ = ["AdaptiveTaskController", "InteractionKind", "InteractionSummary"]

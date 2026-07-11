from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from novel_crawler.adaptation.config_manager import ConfigResolution, ResolutionKind
from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.browser.adaptive import AdaptiveResult
from novel_crawler.browser.models import VerificationStatus, VerificationTicket
from novel_crawler.task_engine.executor import BackgroundTaskExecutor, ExecutorClosed
from novel_crawler.task_engine.integration import AdaptiveTaskController, InteractionKind
from novel_crawler.task_engine.models import TaskStatus
from novel_crawler.task_engine.repository import TaskRepository


class FakeManager:
    def __init__(self, result: ConfigResolution | None = None) -> None:
        self.result = result or ConfigResolution(ResolutionKind.REGISTERED, config=cast(SiteConfig, object()))
        self.confirmed: list[tuple[str, dict[str, str]]] = []
        self.cancelled: list[str] = []

    def confirm(self, token: str, selector_overrides: dict[str, str] | None = None) -> ConfigResolution:
        self.confirmed.append((token, dict(selector_overrides or {})))
        return self.result

    def cancel(self, token: str) -> bool:
        self.cancelled.append(token)
        return True


class FakeAdaptive:
    def __init__(self, *results: AdaptiveResult) -> None:
        self.results = deque(results)
        self.config_manager = FakeManager()
        self.continued: list[str] = []
        self.cancelled: list[str] = []
        self.cleaned: list[str] = []

    def resolve(self, _url: str, _task_id: str) -> AdaptiveResult:
        return self.results.popleft()

    def continue_verification(self, ticket: VerificationTicket | str) -> AdaptiveResult:
        token = ticket.token if isinstance(ticket, VerificationTicket) else ticket
        self.continued.append(token)
        return self.results.popleft()

    def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
        token = ticket.token if isinstance(ticket, VerificationTicket) else ticket
        self.cancelled.append(token)
        return AdaptiveResult(ConfigResolution(ResolutionKind.CANCELLED))

    def retry_cleanup(self, ticket: VerificationTicket | str) -> AdaptiveResult:
        token = ticket.token if isinstance(ticket, VerificationTicket) else ticket
        self.cleaned.append(token)
        return self.results.popleft()

    def expire_sweep(self) -> int:
        return 0


def result(kind: ResolutionKind, *, token: str = "private-token", source: str = "visible") -> AdaptiveResult:
    if kind in {ResolutionKind.REUSED, ResolutionKind.REGISTERED}:
        return AdaptiveResult(ConfigResolution(kind, config=cast(SiteConfig, object())))
    if kind is ResolutionKind.CONFIRMATION_REQUIRED:
        return AdaptiveResult(ConfigResolution(kind, confirmation_token=token))
    if kind is ResolutionKind.WAITING_FOR_USER:
        ticket = VerificationTicket(
            token,
            VerificationStatus.WAITING,
            "https://example.test",
            datetime.now(UTC) + timedelta(minutes=3),
            2,
        )
        return AdaptiveResult(ConfigResolution(kind), ticket)
    if kind is ResolutionKind.CLEANUP_REQUIRED:
        return AdaptiveResult(ConfigResolution(kind), cleanup_ticket=token, cleanup_source=source)
    return AdaptiveResult(ConfigResolution(kind, reason_ids=("safe_reason",)))


def probing(repo: TaskRepository, url: str = "https://example.test/book") -> Any:
    task = repo.create_task(url)
    return repo.transition(task.task_id, TaskStatus.PROBING, expected_version=task.version)


def validating(repo: TaskRepository) -> Any:
    task = probing(repo)
    return repo.transition(task.task_id, TaskStatus.VALIDATING, expected_version=task.version)


def crawling(repo: TaskRepository) -> Any:
    task = validating(repo)
    ready = repo.transition(task.task_id, TaskStatus.READY, expected_version=task.version)
    return repo.transition(task.task_id, TaskStatus.CRAWLING, expected_version=ready.version)


@pytest.mark.parametrize(
    ("kind", "expected", "error"),
    [
        (ResolutionKind.REUSED, TaskStatus.VALIDATING, None),
        (ResolutionKind.REGISTERED, TaskStatus.VALIDATING, None),
        (ResolutionKind.REJECTED, TaskStatus.TERMINAL_FAILED, "adaptation_rejected"),
        (ResolutionKind.TRANSIENT_FAILURE, TaskStatus.RECOVERABLE_FAILED, "adaptation_transient_failure"),
        (ResolutionKind.TIMED_OUT, TaskStatus.RECOVERABLE_FAILED, "verification_timed_out"),
        (ResolutionKind.VERIFICATION_FAILED, TaskStatus.RECOVERABLE_FAILED, "verification_failed"),
        (ResolutionKind.CANCELLED, TaskStatus.CANCELLED, "interaction_cancelled"),
    ],
)
def test_probe_handler_maps_terminal_resolution_kinds(
    tmp_path: Path, kind: ResolutionKind, expected: TaskStatus, error: str | None
) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, FakeAdaptive(result(kind)))
        assert controller.probe_handler(None, task) is None
        current = repo.get_task(task.task_id)
        assert current.status is expected
        assert current.error_code == error


@pytest.mark.parametrize(
    ("kind", "interaction_kind"),
    [
        (ResolutionKind.WAITING_FOR_USER, InteractionKind.VERIFICATION),
        (ResolutionKind.CONFIRMATION_REQUIRED, InteractionKind.CONFIRMATION),
    ],
)
def test_waiting_handles_are_private_and_summary_is_safe(
    tmp_path: Path, kind: ResolutionKind, interaction_kind: InteractionKind
) -> None:
    secret = "super-private-token-123"
    db = tmp_path / "tasks.db"
    with TaskRepository(db) as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, FakeAdaptive(result(kind, token=secret)))
        controller.probe_handler(None, task)
        summary = controller.interaction(task.task_id)
        assert summary is not None and summary.kind is interaction_kind
        encoded = repr(controller) + repr(summary) + json.dumps(summary.to_safe_dict())
        assert secret not in encoded
        assert "https://example.test" in encoded if kind is ResolutionKind.WAITING_FOR_USER else True
        assert secret not in json.dumps(repo.get_task(task.task_id).to_safe_dict())
        assert secret not in json.dumps([event.to_safe_dict() for event in repo.list_events(task.task_id)])
    raw = db.read_bytes()
    assert secret.encode() not in raw


def test_late_verification_handle_is_adopted_and_continuable(tmp_path: Path) -> None:
    secret = "late-private-ticket"
    adaptive = FakeAdaptive(result(ResolutionKind.REUSED))
    with TaskRepository(tmp_path / "late-waiting.db") as repo:
        task = crawling(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        waiting = result(ResolutionKind.WAITING_FOR_USER, token=secret)
        adopted = controller.adopt_acquisition_result(task.task_id, waiting)
        assert adopted.status is TaskStatus.WAITING_FOR_USER
        assert controller.interaction(task.task_id).kind is InteractionKind.VERIFICATION  # type: ignore[union-attr]
        continued = controller.continue_verification(task.task_id)
        assert continued.status is TaskStatus.CRAWLING
        assert adaptive.continued == [secret]
        assert secret.encode() not in (tmp_path / "late-waiting.db").read_bytes()


def test_late_cleanup_handle_sets_gate_and_is_retryable(tmp_path: Path) -> None:
    secret = "late-private-cleanup"
    adaptive = FakeAdaptive(result(ResolutionKind.REUSED))
    with TaskRepository(tmp_path / "late-cleanup.db") as repo:
        task = crawling(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        cleanup = result(ResolutionKind.CLEANUP_REQUIRED, token=secret, source="headless")
        adopted = controller.adopt_acquisition_result(task.task_id, cleanup)
        assert adopted.status is TaskStatus.RECOVERABLE_FAILED
        assert adopted.cleanup_required is True
        retried = controller.retry_cleanup(task.task_id)
        assert retried.status is TaskStatus.CRAWLING
        assert retried.cleanup_required is False
        assert adaptive.cleaned == [secret]
        assert secret.encode() not in (tmp_path / "late-cleanup.db").read_bytes()


def test_late_cleanup_race_with_pause_is_compensated_without_orphan(tmp_path: Path) -> None:
    secret = "raced-private-cleanup"
    adaptive = FakeAdaptive(result(ResolutionKind.REUSED))
    with TaskRepository(tmp_path / "late-race.db") as repo:
        active = crawling(repo)
        paused = repo.transition(
            active.task_id,
            TaskStatus.PAUSED,
            expected_version=active.version,
            reason="concurrent_pause",
        )
        controller = AdaptiveTaskController(repo, adaptive)
        cleanup = result(ResolutionKind.CLEANUP_REQUIRED, token=secret, source="headless")
        adopted = controller.adopt_acquisition_result(paused.task_id, cleanup)
        assert adopted.status is TaskStatus.PAUSED
        assert controller.interaction(paused.task_id) is None
        assert adaptive.cleaned == [secret]
        assert secret.encode() not in (tmp_path / "late-race.db").read_bytes()


def test_failed_late_cleanup_compensation_gates_paused_task(tmp_path: Path) -> None:
    secret = "raced-private-cleanup"
    adaptive = FakeAdaptive(result(ResolutionKind.CLEANUP_REQUIRED, token=secret, source="headless"))
    with TaskRepository(tmp_path / "late-race-gated.db") as repo:
        active = crawling(repo)
        paused = repo.transition(active.task_id, TaskStatus.PAUSED, expected_version=active.version)
        controller = AdaptiveTaskController(repo, adaptive)
        cleanup = result(ResolutionKind.CLEANUP_REQUIRED, token=secret, source="headless")
        adopted = controller.adopt_acquisition_result(paused.task_id, cleanup)
        assert adopted.status is TaskStatus.RECOVERABLE_FAILED
        assert adopted.cleanup_required is True
        assert adopted.resume_status is TaskStatus.CRAWLING
        assert controller.interaction(paused.task_id).kind is InteractionKind.CLEANUP  # type: ignore[union-attr]
        assert secret.encode() not in (tmp_path / "late-race-gated.db").read_bytes()


def test_capture_race_with_pause_aborts_new_cleanup_handle(tmp_path: Path) -> None:
    secret = "capture-race-cleanup"
    adaptive = FakeAdaptive(result(ResolutionKind.REUSED))
    with TaskRepository(tmp_path / "capture-pause.db") as repo:
        task = crawling(repo)
        controller = AdaptiveTaskController(repo, adaptive)

        def capture():
            repo.transition(task.task_id, TaskStatus.PAUSED, expected_version=task.version)
            return result(ResolutionKind.CLEANUP_REQUIRED, token=secret, source="headless")

        current = controller.capture_acquisition_result(task.task_id, task.version, capture)
        assert current.status is TaskStatus.PAUSED
        assert adaptive.cleaned == [secret]
        assert controller.interaction(task.task_id) is None
        assert secret.encode() not in (tmp_path / "capture-pause.db").read_bytes()


def test_capture_capacity_one_aborts_second_concurrent_handle(tmp_path: Path) -> None:
    first_secret = "first-capacity-ticket"
    second_secret = "second-capacity-cleanup"
    entered = threading.Event()
    release = threading.Event()
    adaptive = FakeAdaptive(result(ResolutionKind.REUSED))
    with TaskRepository(tmp_path / "capture-capacity.db") as repo:
        first = crawling(repo)
        second = crawling(repo)
        controller = AdaptiveTaskController(repo, adaptive, max_interactions=1)

        def first_capture():
            entered.set()
            assert release.wait(5)
            return result(ResolutionKind.WAITING_FOR_USER, token=first_secret)

        thread = threading.Thread(
            target=lambda: controller.capture_acquisition_result(
                first.task_id, first.version, first_capture
            )
        )
        thread.start()
        assert entered.wait(2)
        current = controller.capture_acquisition_result(
            second.task_id,
            second.version,
            lambda: result(
                ResolutionKind.CLEANUP_REQUIRED,
                token=second_secret,
                source="headless",
            ),
        )
        assert current.status is TaskStatus.CRAWLING
        assert adaptive.cleaned == [second_secret]
        release.set()
        thread.join(5)
        assert repo.get_task(first.task_id).status is TaskStatus.WAITING_FOR_USER
        raw = (tmp_path / "capture-capacity.db").read_bytes()
        assert first_secret.encode() not in raw and second_secret.encode() not in raw


def test_continue_verification_is_concurrently_idempotent(tmp_path: Path) -> None:
    secret = "verification-secret"
    adaptive = FakeAdaptive(result(ResolutionKind.WAITING_FOR_USER, token=secret), result(ResolutionKind.REUSED))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        answers: list[TaskStatus] = []
        barrier = threading.Barrier(2)

        def run() -> None:
            barrier.wait()
            answers.append(controller.continue_verification(task.task_id).status)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert answers == [TaskStatus.VALIDATING, TaskStatus.VALIDATING]
        assert adaptive.continued == [secret]


def test_verification_can_wait_multiple_steps_then_time_out(tmp_path: Path) -> None:
    first = result(ResolutionKind.WAITING_FOR_USER, token="first-token")
    second = result(ResolutionKind.WAITING_FOR_USER, token="second-token")
    adaptive = FakeAdaptive(first, second, result(ResolutionKind.TIMED_OUT))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        waiting = controller.continue_verification(task.task_id)
        assert waiting.status is TaskStatus.WAITING_FOR_USER
        assert controller.interaction(task.task_id).attempt == 2  # type: ignore[union-attr]
        timed_out = controller.continue_verification(task.task_id)
        assert timed_out.status is TaskStatus.RECOVERABLE_FAILED
        assert timed_out.error_code == "verification_timed_out"
        assert timed_out.resume_status is TaskStatus.PROBING
        assert adaptive.continued == ["first-token", "second-token"]


def test_confirmation_applies_validated_overrides_and_is_one_shot(tmp_path: Path) -> None:
    secret = "confirmation-secret"
    adaptive = FakeAdaptive(result(ResolutionKind.CONFIRMATION_REQUIRED, token=secret))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        current = controller.confirm_config(task.task_id, {"content": "main article"})
        assert current.status is TaskStatus.VALIDATING
        assert adaptive.config_manager.confirmed == [(secret, {"content": "main article"})]
        assert controller.confirm_config(task.task_id, {"content": "other"}).status is TaskStatus.VALIDATING
        with pytest.raises(ValueError, match="selector_overrides_invalid"):
            controller.confirm_config(task.task_id, {"content": "https://secret.test/?token=x"})


def test_confirmation_allows_css_words_that_merely_resemble_credentials(tmp_path: Path) -> None:
    secret = "confirmation-secret"
    adaptive = FakeAdaptive(result(ResolutionKind.CONFIRMATION_REQUIRED, token=secret))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        selectors = {
            "content": ".token-list .secret-story",
            "clean_selector": "[data-cookie-banner]",
        }
        assert controller.confirm_config(task.task_id, selectors).status is TaskStatus.VALIDATING
        assert adaptive.config_manager.confirmed == [(secret, selectors)]


def test_cleanup_handle_survives_recoverable_state_and_retries_via_probing(tmp_path: Path) -> None:
    secret = "cleanup-secret"
    adaptive = FakeAdaptive(result(ResolutionKind.CLEANUP_REQUIRED, token=secret), result(ResolutionKind.REUSED))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        failed = repo.get_task(task.task_id)
        assert failed.status is TaskStatus.RECOVERABLE_FAILED
        assert failed.cleanup_required is True
        assert controller.interaction(task.task_id).cleanup_source == "visible"  # type: ignore[union-attr]
        retried = controller.retry_cleanup(task.task_id)
        assert retried.status is TaskStatus.VALIDATING
        assert retried.cleanup_required is False
        assert adaptive.cleaned == [secret]
        assert controller.interaction(task.task_id) is None


def test_cleanup_gate_survives_controller_restart_without_private_handle(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    secret = "restart-cleanup-secret"
    adaptive = FakeAdaptive(result(ResolutionKind.CLEANUP_REQUIRED, token=secret))
    with TaskRepository(path) as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        failed = repo.get_task(task.task_id)
        assert failed.cleanup_required is True
    with TaskRepository(path) as repo:
        restarted = AdaptiveTaskController(repo, FakeAdaptive())
        failed = repo.get_task(task.task_id)
        assert failed.cleanup_required is True
        assert failed.status is TaskStatus.RECOVERABLE_FAILED
        assert restarted.interaction(task.task_id) is None
        assert secret.encode() not in path.read_bytes()


def test_restart_recovers_orphan_waiting_with_constrained_resume_target(tmp_path: Path) -> None:
    db = tmp_path / "tasks.db"
    with TaskRepository(db) as repo:
        task = probing(repo)
        waiting = repo.transition(task.task_id, TaskStatus.WAITING_FOR_USER, expected_version=task.version)
        controller = AdaptiveTaskController(repo, FakeAdaptive())
        recovered = repo.get_task(task.task_id)
        assert recovered.status is TaskStatus.RECOVERABLE_FAILED
        assert recovered.error_code == "interaction_session_lost"
        assert recovered.resume_status is TaskStatus.PROBING
        assert controller.recovered_orphans == 1
        with pytest.raises(ValueError):
            repo.recover_lost_interaction(
                task.task_id, expected_version=recovered.version, error_code="arbitrary"
            )
        assert waiting.version + 1 == recovered.version


def test_cancel_interaction_clears_handle_and_wins_pause_race(tmp_path: Path) -> None:
    secret = "cancel-secret"
    adaptive = FakeAdaptive(result(ResolutionKind.WAITING_FOR_USER, token=secret))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        paused = repo.transition(task.task_id, TaskStatus.PAUSED, expected_version=repo.get_task(task.task_id).version)
        assert controller.cancel_interaction(task.task_id).status is TaskStatus.CANCELLED
        assert paused.status is TaskStatus.PAUSED
        assert adaptive.cancelled == [secret]
        assert controller.interaction(task.task_id) is None


def test_capacity_and_ttl_are_bounded_and_expiry_is_recoverable(tmp_path: Path) -> None:
    now = [100.0]
    adaptive = FakeAdaptive(
        result(ResolutionKind.WAITING_FOR_USER, token="first-secret"),
        result(ResolutionKind.WAITING_FOR_USER, token="unused-secret"),
    )
    with TaskRepository(tmp_path / "tasks.db") as repo:
        first = probing(repo, "https://one.test")
        second = probing(repo, "https://two.test")
        controller = AdaptiveTaskController(
            repo, adaptive, max_interactions=1, interaction_ttl_seconds=5, monotonic=lambda: now[0]
        )
        controller.probe_handler(None, first)
        controller.probe_handler(None, second)
        assert repo.get_task(second.task_id).error_code == "interaction_capacity_exceeded"
        now[0] += 6
        assert controller.sweep() == 1
        expired = repo.get_task(first.task_id)
        assert expired.status is TaskStatus.RECOVERABLE_FAILED
        assert expired.error_code == "interaction_expired"
        assert expired.resume_status is TaskStatus.PROBING


def test_capacity_counts_inflight_reservations(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class Blocking(FakeAdaptive):
        def resolve(self, _url: str, _task_id: str) -> AdaptiveResult:
            entered.set()
            assert release.wait(2)
            return result(ResolutionKind.WAITING_FOR_USER, token="first-secret")

    with TaskRepository(tmp_path / "tasks.db") as repo:
        first = probing(repo, "https://one.test")
        second = probing(repo, "https://two.test")
        controller = AdaptiveTaskController(repo, Blocking(), max_interactions=1)
        worker = threading.Thread(target=controller.probe_handler, args=(None, first))
        worker.start()
        assert entered.wait(1)
        controller.probe_handler(None, second)
        release.set()
        worker.join()
        assert repo.get_task(second.task_id).error_code == "interaction_capacity_exceeded"
        assert controller.interaction(first.task_id) is not None


def test_cleanup_expiry_retries_and_never_drops_required_handle(tmp_path: Path) -> None:
    now = [50.0]
    secret = "cleanup-secret"
    adaptive = FakeAdaptive(
        result(ResolutionKind.CLEANUP_REQUIRED, token=secret),
        result(ResolutionKind.CLEANUP_REQUIRED, token=secret),
    )
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(
            repo, adaptive, interaction_ttl_seconds=2, monotonic=lambda: now[0]
        )
        controller.probe_handler(None, task)
        now[0] += 3
        controller.sweep()
        assert controller.interaction(task.task_id) is not None
        assert repo.get_task(task.task_id).status is TaskStatus.RECOVERABLE_FAILED
        assert adaptive.cleaned == [secret]


def test_sweep_clears_handle_after_external_terminal_transition(tmp_path: Path) -> None:
    adaptive = FakeAdaptive(result(ResolutionKind.WAITING_FOR_USER, token="terminal-secret"))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        current = repo.get_task(task.task_id)
        repo.transition(task.task_id, TaskStatus.CANCELLED, expected_version=current.version)
        controller.sweep()
        assert controller.interaction(task.task_id) is None


def test_adaptive_exception_is_redacted_to_stable_failure(tmp_path: Path) -> None:
    class Broken(FakeAdaptive):
        def resolve(self, _url: str, _task_id: str) -> AdaptiveResult:
            raise RuntimeError("secret-token=do-not-store")

    db = tmp_path / "tasks.db"
    with TaskRepository(db) as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, Broken())
        controller.probe_handler(None, task)
        assert repo.get_task(task.task_id).error_code == "adaptive_service_failed"
    assert b"do-not-store" not in db.read_bytes()


def test_constructor_and_override_boundaries(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repo:
        with pytest.raises(ValueError, match="max_interactions"):
            AdaptiveTaskController(repo, FakeAdaptive(), max_interactions=0)
        with pytest.raises(ValueError, match="interaction_ttl"):
            AdaptiveTaskController(repo, FakeAdaptive(), interaction_ttl_seconds=0)
        task = probing(repo)
        adaptive = FakeAdaptive(result(ResolutionKind.CONFIRMATION_REQUIRED))
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        assert controller.confirm_config(task.task_id, None).status is TaskStatus.VALIDATING
        assert adaptive.config_manager.confirmed == [("private-token", {})]
        with pytest.raises(ValueError, match="selector_overrides_invalid"):
            controller.confirm_config(task.task_id, cast(Any, [("content", "main")]))


def test_non_probing_handler_and_missing_actions_are_noops(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repo:
        created = repo.create_task("https://example.test")
        controller = AdaptiveTaskController(repo, FakeAdaptive())
        assert controller.probe_handler(None, created) is None
        assert controller.cancel_interaction(created.task_id).status is TaskStatus.CREATED
        assert controller.retry_cleanup(created.task_id).status is TaskStatus.CREATED
        assert controller.continue_verification(created.task_id).status is TaskStatus.CREATED


def test_confirmation_cancel_is_private_and_idempotent(tmp_path: Path) -> None:
    secret = "confirmation-cancel-secret"
    adaptive = FakeAdaptive(result(ResolutionKind.CONFIRMATION_REQUIRED, token=secret))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        assert controller.cancel_interaction(task.task_id).status is TaskStatus.CANCELLED
        assert adaptive.config_manager.cancelled == [secret]
        assert controller.cancel_interaction(task.task_id).status is TaskStatus.CANCELLED


def test_cleanup_cannot_be_cancelled_or_retried_by_wrong_action(tmp_path: Path) -> None:
    adaptive = FakeAdaptive(result(ResolutionKind.CLEANUP_REQUIRED, token="cleanup-secret"))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        assert controller.cancel_interaction(task.task_id).status is TaskStatus.RECOVERABLE_FAILED
        assert controller.continue_verification(task.task_id).status is TaskStatus.RECOVERABLE_FAILED
        assert controller.interaction(task.task_id) is not None


def test_paused_waiting_does_not_consume_handle(tmp_path: Path) -> None:
    secret = "paused-secret"
    adaptive = FakeAdaptive(result(ResolutionKind.WAITING_FOR_USER, token=secret))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        waiting = repo.get_task(task.task_id)
        repo.transition(task.task_id, TaskStatus.PAUSED, expected_version=waiting.version)
        assert controller.continue_verification(task.task_id).status is TaskStatus.PAUSED
        assert adaptive.continued == []
        assert controller.interaction(task.task_id) is not None


def test_cancel_race_during_resolve_cleans_new_private_handle(tmp_path: Path) -> None:
    secret = "race-secret"
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)

        class Cancelling(FakeAdaptive):
            def resolve(self, _url: str, _task_id: str) -> AdaptiveResult:
                current = repo.get_task(task.task_id)
                repo.transition(task.task_id, TaskStatus.CANCELLED, expected_version=current.version)
                return result(ResolutionKind.WAITING_FOR_USER, token=secret)

        adaptive = Cancelling()
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        assert repo.get_task(task.task_id).status is TaskStatus.CANCELLED
        assert controller.interaction(task.task_id) is None
        assert adaptive.cancelled == [secret]


def test_operation_exception_and_cleanup_retry_exception_are_safe(tmp_path: Path) -> None:
    class BrokenManager(FakeManager):
        def confirm(self, token: str, selector_overrides: dict[str, str] | None = None) -> ConfigResolution:
            raise RuntimeError(f"private {token}")

    adaptive = FakeAdaptive(result(ResolutionKind.CONFIRMATION_REQUIRED, token="manager-secret"))
    adaptive.config_manager = BrokenManager()
    with TaskRepository(tmp_path / "manager.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        failed = controller.confirm_config(task.task_id)
        assert failed.error_code == "verification_failed"

    class BrokenCleanup(FakeAdaptive):
        def retry_cleanup(self, ticket: VerificationTicket | str) -> AdaptiveResult:
            raise RuntimeError("cleanup secret")

    broken = BrokenCleanup(result(ResolutionKind.CLEANUP_REQUIRED, token="cleanup-secret"))
    with TaskRepository(tmp_path / "cleanup.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, broken, interaction_ttl_seconds=1)
        controller.probe_handler(None, task)
        assert controller.retry_cleanup(task.task_id).status is TaskStatus.RECOVERABLE_FAILED
        assert repo.get_task(task.task_id).cleanup_required is True
        assert controller.interaction(task.task_id) is not None


def test_cancel_maps_adaptive_cleanup_result_without_losing_handle(tmp_path: Path) -> None:
    class CleanupOnCancel(FakeAdaptive):
        def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
            token = ticket.token if isinstance(ticket, VerificationTicket) else ticket
            self.cancelled.append(token)
            return result(ResolutionKind.CLEANUP_REQUIRED, token="cleanup-after-cancel")

    adaptive = CleanupOnCancel(result(ResolutionKind.WAITING_FOR_USER, token="verify-secret"))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        cancelled = controller.cancel_interaction(task.task_id)
        assert cancelled.status is TaskStatus.RECOVERABLE_FAILED
        assert cancelled.error_code == "interaction_cleanup_required"
        summary = controller.interaction(task.task_id)
        assert summary is not None and summary.kind is InteractionKind.CLEANUP
        assert adaptive.cancelled == ["verify-secret"]


def test_expiry_maps_cancel_cleanup_and_retains_retry_handle(tmp_path: Path) -> None:
    now = [10.0]

    class CleanupOnCancel(FakeAdaptive):
        def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
            return result(ResolutionKind.CLEANUP_REQUIRED, token="expiry-cleanup-secret")

    adaptive = CleanupOnCancel(result(ResolutionKind.WAITING_FOR_USER, token="expiry-verify-secret"))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(
            repo, adaptive, interaction_ttl_seconds=1, monotonic=lambda: now[0]
        )
        controller.probe_handler(None, task)
        now[0] += 2
        assert controller.sweep() == 1
        failed = repo.get_task(task.task_id)
        assert failed.status is TaskStatus.RECOVERABLE_FAILED
        assert failed.error_code == "interaction_cleanup_required"
        assert controller.interaction(task.task_id).kind is InteractionKind.CLEANUP  # type: ignore[union-attr]


def test_external_cancel_race_keeps_cleanup_handle_until_cleanup_succeeds(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)

        class CancelAndCleanup(FakeAdaptive):
            def resolve(self, _url: str, _task_id: str) -> AdaptiveResult:
                current = repo.get_task(task.task_id)
                repo.transition(task.task_id, TaskStatus.CANCELLED, expected_version=current.version)
                return result(ResolutionKind.WAITING_FOR_USER, token="new-verification-secret")

            def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
                return result(ResolutionKind.CLEANUP_REQUIRED, token="race-cleanup-secret")

        controller = AdaptiveTaskController(repo, CancelAndCleanup())
        controller.probe_handler(None, task)
        summary = controller.interaction(task.task_id)
        assert summary is not None and summary.kind is InteractionKind.CLEANUP


def test_unexpired_foreign_interaction_lease_is_not_recovered(tmp_path: Path) -> None:
    now = [datetime(2030, 1, 1, tzinfo=UTC)]
    adaptive = FakeAdaptive(result(ResolutionKind.WAITING_FOR_USER, token="owner-one-secret"))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        first = AdaptiveTaskController(repo, adaptive, wall_clock=lambda: now[0])
        first.probe_handler(None, task)
        second = AdaptiveTaskController(repo, FakeAdaptive(), wall_clock=lambda: now[0])
        assert second.recovered_orphans == 0
        assert repo.get_task(task.task_id).status is TaskStatus.WAITING_FOR_USER
        assert "owner" not in json.dumps(repo.get_task(task.task_id).to_safe_dict())
        now[0] += timedelta(minutes=11)
        third = AdaptiveTaskController(repo, FakeAdaptive(), wall_clock=lambda: now[0])
        recovered = repo.get_task(task.task_id)
        assert third.recovered_orphans == 1
        assert recovered.status is TaskStatus.RECOVERABLE_FAILED
        assert recovered.resume_status is TaskStatus.PROBING


def test_controller_close_releases_lease_for_safe_restart_recovery(tmp_path: Path) -> None:
    adaptive = FakeAdaptive(result(ResolutionKind.WAITING_FOR_USER, token="close-secret"))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        first = AdaptiveTaskController(repo, adaptive)
        first.probe_handler(None, task)
        first.close()
        second = AdaptiveTaskController(repo, FakeAdaptive())
        assert second.recovered_orphans == 1
        assert repo.get_task(task.task_id).resume_status is TaskStatus.PROBING


def test_probe_calls_are_singleflight_and_resolve_once(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = [0]

    class Blocking(FakeAdaptive):
        def resolve(self, _url: str, _task_id: str) -> AdaptiveResult:
            calls[0] += 1
            entered.set()
            assert release.wait(2)
            return result(ResolutionKind.REUSED)

    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, Blocking())
        threads = [threading.Thread(target=controller.probe_handler, args=(None, task)) for _ in range(2)]
        threads[0].start()
        assert entered.wait(1)
        threads[1].start()
        release.set()
        for thread in threads:
            thread.join()
        assert calls == [1]
        assert repo.get_task(task.task_id).status is TaskStatus.VALIDATING


def test_controller_handoff_runs_real_executor_to_ready_and_completed(tmp_path: Path) -> None:
    adaptive = FakeAdaptive(result(ResolutionKind.REUSED))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = repo.create_task("https://example.test")
        controller = AdaptiveTaskController(repo, adaptive)

        def validate(_context, _task):  # type: ignore[no-untyped-def]
            return TaskStatus.READY

        def crawl(_context, _task):  # type: ignore[no-untyped-def]
            return TaskStatus.COMPLETED

        with BackgroundTaskExecutor(
            repo,
            {
                TaskStatus.PROBING: controller.probe_handler,
                TaskStatus.VALIDATING: validate,
                TaskStatus.CRAWLING: crawl,
            },
            max_workers=1,
        ) as executor:
            controller.bind_scheduler(executor.schedule_active)
            assert executor.submit(task.task_id)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and repo.get_task(task.task_id).status is not TaskStatus.COMPLETED:
                time.sleep(0.01)
            assert repo.get_task(task.task_id).status is TaskStatus.COMPLETED


def test_schedule_failure_becomes_precisely_resumable(tmp_path: Path) -> None:
    def unavailable(_task_id: str) -> bool:
        raise ExecutorClosed("closed")

    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, FakeAdaptive(result(ResolutionKind.REUSED)), scheduler=unavailable)
        controller.probe_handler(None, task)
        failed = repo.get_task(task.task_id)
        assert failed.status is TaskStatus.RECOVERABLE_FAILED
        assert failed.resume_status is TaskStatus.VALIDATING
        assert failed.error_code == "executor_unavailable"


def test_controller_control_flow_exception_propagates_and_singleflight_recovers(tmp_path: Path) -> None:
    class StopsOnce(FakeAdaptive):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def resolve(self, _url: str, _task_id: str) -> AdaptiveResult:
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt()
            return result(ResolutionKind.REUSED)

    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        adaptive = StopsOnce()
        controller = AdaptiveTaskController(repo, adaptive)
        with pytest.raises(KeyboardInterrupt):
            controller.probe_handler(None, task)
        assert repo.get_task(task.task_id).status is TaskStatus.PROBING
        controller.probe_handler(None, repo.get_task(task.task_id))
        assert repo.get_task(task.task_id).status is TaskStatus.VALIDATING
        assert adaptive.calls == 2


def test_scheduler_binding_and_close_cleanup_boundaries(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repo:
        controller = AdaptiveTaskController(repo, FakeAdaptive())
        with pytest.raises(TypeError, match="scheduler"):
            controller.bind_scheduler(cast(Any, None))

    cleanup = FakeAdaptive(
        result(ResolutionKind.CLEANUP_REQUIRED, token="close-cleanup"),
        result(ResolutionKind.CLEANUP_REQUIRED, token="close-cleanup"),
    )
    with TaskRepository(tmp_path / "cleanup.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, cleanup)
        controller.probe_handler(None, task)
        assert controller.close() is False
        assert controller.interaction(task.task_id) is not None

    cleaned = FakeAdaptive(
        result(ResolutionKind.CLEANUP_REQUIRED, token="close-cleanup-success"),
        result(ResolutionKind.REUSED),
    )
    with TaskRepository(tmp_path / "cleaned.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, cleaned)
        controller.probe_handler(None, task)
        assert controller.close() is True
        assert repo.get_task(task.task_id).status is TaskStatus.VALIDATING


def test_terminal_cancel_pending_can_retry_without_clearing_fake_gate(tmp_path: Path) -> None:
    class FailsOnce(FakeAdaptive):
        def __init__(self) -> None:
            super().__init__(result(ResolutionKind.WAITING_FOR_USER, token="terminal-pending"))
            self.calls = 0

        def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("cancel unavailable")
            return result(ResolutionKind.CANCELLED)

    adaptive = FailsOnce()
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        waiting = repo.get_task(task.task_id)
        repo.transition(task.task_id, TaskStatus.CANCELLED, expected_version=waiting.version)
        assert controller.cancel_interaction(task.task_id).status is TaskStatus.CANCELLED
        assert controller.interaction(task.task_id).kind is InteractionKind.CANCEL_PENDING  # type: ignore[union-attr]
        assert controller.retry_cancel_cleanup(task.task_id).status is TaskStatus.CANCELLED
        assert controller.interaction(task.task_id) is None


def test_terminal_sweep_follows_cancel_cleanup_ticket_to_completion(tmp_path: Path) -> None:
    class CleanupCancel(FakeAdaptive):
        def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
            return result(ResolutionKind.CLEANUP_REQUIRED, token="terminal-real-cleanup")

    adaptive = CleanupCancel(
        result(ResolutionKind.WAITING_FOR_USER, token="terminal-verification"),
        result(ResolutionKind.REUSED),
    )
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        waiting = repo.get_task(task.task_id)
        repo.transition(task.task_id, TaskStatus.CANCELLED, expected_version=waiting.version)
        controller.sweep()
        assert adaptive.cleaned == ["terminal-real-cleanup"]
        assert controller.interaction(task.task_id) is None


def test_close_retains_cleanup_returned_by_verification_cancel(tmp_path: Path) -> None:
    class CleanupCancel(FakeAdaptive):
        def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
            return result(ResolutionKind.CLEANUP_REQUIRED, token="close-visible-cleanup")

    adaptive = CleanupCancel(result(ResolutionKind.WAITING_FOR_USER, token="close-verify"))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        assert controller.close() is False
        assert controller.interaction(task.task_id).kind is InteractionKind.CLEANUP  # type: ignore[union-attr]


def test_cancel_exception_is_recoverable_and_releases_waiting_lease(tmp_path: Path) -> None:
    class BrokenCancel(FakeAdaptive):
        def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
            raise RuntimeError("private cancel failure")

    adaptive = BrokenCancel(result(ResolutionKind.WAITING_FOR_USER, token="cancel-secret"))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        failed = controller.cancel_interaction(task.task_id)
        assert failed.status is TaskStatus.RECOVERABLE_FAILED
        assert failed.error_code == "interaction_cleanup_required"
        assert failed.resume_status is TaskStatus.PROBING
        assert failed.cleanup_required is True
        summary = controller.interaction(task.task_id)
        assert summary is not None and summary.cleanup_required is True


def test_cancel_pending_retries_exceptions_then_completes_without_token_persistence(tmp_path: Path) -> None:
    secret = "cancel-pending-private-token"

    class FlakyCancel(FakeAdaptive):
        def __init__(self) -> None:
            super().__init__(result(ResolutionKind.WAITING_FOR_USER, token=secret))
            self.cancel_attempts = 0

        def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
            self.cancel_attempts += 1
            if self.cancel_attempts < 3:
                raise RuntimeError(f"private {secret}")
            return result(ResolutionKind.CANCELLED)

    path = tmp_path / "tasks.db"
    adaptive = FlakyCancel()
    with TaskRepository(path) as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        assert controller.cancel_interaction(task.task_id).cleanup_required is True
        assert controller.retry_cleanup(task.task_id).cleanup_required is True
        cancelled = controller.retry_cleanup(task.task_id)
        assert cancelled.status is TaskStatus.CANCELLED
        assert cancelled.cleanup_required is False
        assert controller.interaction(task.task_id) is None
    assert secret.encode() not in path.read_bytes()


def test_confirmation_cancel_pending_retries_only_config_manager_channel(tmp_path: Path) -> None:
    secret = "confirmation-cancel-private-token"

    class FlakyManager(FakeManager):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def cancel(self, token: str) -> bool:
            self.attempts += 1
            self.cancelled.append(token)
            if self.attempts < 3:
                raise RuntimeError(f"private {token}")
            return True

    path = tmp_path / "tasks.db"
    adaptive = FakeAdaptive(result(ResolutionKind.CONFIRMATION_REQUIRED, token=secret))
    manager = FlakyManager()
    adaptive.config_manager = manager
    with TaskRepository(path) as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        assert controller.cancel_interaction(task.task_id).cleanup_required is True
        assert controller.retry_cancel_cleanup(task.task_id).cleanup_required is True
        cancelled = controller.retry_cancel_cleanup(task.task_id)
        assert cancelled.status is TaskStatus.CANCELLED
        assert cancelled.cleanup_required is False
        assert adaptive.cancelled == []
        assert manager.cancelled == [secret, secret, secret]
        safe = repr(controller) + json.dumps(cancelled.to_safe_dict())
        assert secret not in safe
    assert secret.encode() not in path.read_bytes()


def test_confirmation_cancel_false_is_idempotent_terminal_completion(tmp_path: Path) -> None:
    secret = "already-absent-confirmation"

    class MissingManager(FakeManager):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def cancel(self, token: str) -> bool:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("temporarily unavailable")
            self.cancelled.append(token)
            return False

    adaptive = FakeAdaptive(result(ResolutionKind.CONFIRMATION_REQUIRED, token=secret))
    manager = MissingManager()
    adaptive.config_manager = manager
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        assert controller.cancel_interaction(task.task_id).cleanup_required is True
        assert controller.retry_cancel_cleanup(task.task_id).status is TaskStatus.CANCELLED
        assert adaptive.cancelled == []
        assert manager.cancelled == [secret]


def test_confirmation_cancel_failure_in_expiry_and_close_stays_pending(tmp_path: Path) -> None:
    class BrokenManager(FakeManager):
        def cancel(self, token: str) -> bool:
            raise RuntimeError(f"private {token}")

    now = [20.0]
    expiring = FakeAdaptive(result(ResolutionKind.CONFIRMATION_REQUIRED, token="expiry-confirmation"))
    expiring.config_manager = BrokenManager()
    with TaskRepository(tmp_path / "expiry-confirmation.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(
            repo, expiring, interaction_ttl_seconds=1, monotonic=lambda: now[0]
        )
        controller.probe_handler(None, task)
        now[0] += 2
        controller.sweep()
        assert repo.get_task(task.task_id).cleanup_required is True
        assert controller.interaction(task.task_id).kind is InteractionKind.CANCEL_PENDING  # type: ignore[union-attr]
        assert expiring.cancelled == []

    closing = FakeAdaptive(result(ResolutionKind.CONFIRMATION_REQUIRED, token="close-confirmation"))
    closing.config_manager = BrokenManager()
    with TaskRepository(tmp_path / "close-confirmation.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, closing)
        controller.probe_handler(None, task)
        assert controller.close() is False
        assert repo.get_task(task.task_id).cleanup_required is True
        assert controller.interaction(task.task_id).kind is InteractionKind.CANCEL_PENDING  # type: ignore[union-attr]
        assert closing.cancelled == []


def test_cancel_pending_switches_to_real_cleanup_ticket_then_retries_cleanup(tmp_path: Path) -> None:
    class CleanupCancel(FakeAdaptive):
        def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
            return result(ResolutionKind.CLEANUP_REQUIRED, token="real-cleanup-private")

    adaptive = CleanupCancel(
        result(ResolutionKind.WAITING_FOR_USER, token="verification-private"),
        result(ResolutionKind.REUSED),
    )
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        gated = controller.cancel_interaction(task.task_id)
        assert gated.cleanup_required is True
        summary = controller.interaction(task.task_id)
        assert summary is not None and summary.kind is InteractionKind.CLEANUP
        completed = controller.retry_cleanup(task.task_id)
        assert completed.status is TaskStatus.VALIDATING
        assert adaptive.cleaned == ["real-cleanup-private"]


def test_cancel_pending_retry_can_yield_cleanup_before_cleanup_completion(tmp_path: Path) -> None:
    class PendingThenCleanup(FakeAdaptive):
        def __init__(self) -> None:
            super().__init__(
                result(ResolutionKind.WAITING_FOR_USER, token="pending-verification"),
                result(ResolutionKind.REUSED),
            )
            self.attempt = 0

        def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
            self.attempt += 1
            if self.attempt == 1:
                raise RuntimeError("cancel unavailable")
            return result(ResolutionKind.CLEANUP_REQUIRED, token="pending-real-cleanup")

    adaptive = PendingThenCleanup()
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        assert controller.cancel_interaction(task.task_id).cleanup_required is True
        still_gated = controller.retry_cancel_cleanup(task.task_id)
        assert still_gated.cleanup_required is True
        assert controller.interaction(task.task_id).kind is InteractionKind.CLEANUP  # type: ignore[union-attr]
        completed = controller.retry_cleanup(task.task_id)
        assert completed.status is TaskStatus.VALIDATING
        assert adaptive.cleaned == ["pending-real-cleanup"]


def test_expiry_and_close_cancel_failures_remain_cleanup_incomplete(tmp_path: Path) -> None:
    class BrokenCancel(FakeAdaptive):
        def cancel(self, ticket: VerificationTicket | str) -> AdaptiveResult:
            raise RuntimeError("cancel unavailable")

    now = [10.0]
    expiring = BrokenCancel(result(ResolutionKind.WAITING_FOR_USER, token="expiry-private"))
    with TaskRepository(tmp_path / "expiry.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(
            repo, expiring, interaction_ttl_seconds=1, monotonic=lambda: now[0]
        )
        controller.probe_handler(None, task)
        now[0] += 2
        assert controller.sweep() == 1
        assert repo.get_task(task.task_id).cleanup_required is True
        assert controller.interaction(task.task_id).cleanup_required is True  # type: ignore[union-attr]

    closing = BrokenCancel(result(ResolutionKind.WAITING_FOR_USER, token="close-private"))
    with TaskRepository(tmp_path / "close.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, closing)
        controller.probe_handler(None, task)
        assert controller.close() is False
        assert repo.get_task(task.task_id).cleanup_required is True
        assert controller.interaction(task.task_id) is not None
        assert controller.close() is False


def test_cancel_rechecks_owned_lease_before_touching_private_handle(tmp_path: Path) -> None:
    adaptive = FakeAdaptive(result(ResolutionKind.WAITING_FOR_USER, token="lease-private"))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        repo.connection.execute("DELETE FROM task_interaction_leases WHERE task_id=?", (task.task_id,))
        assert controller.cancel_interaction(task.task_id).status is TaskStatus.WAITING_FOR_USER
        assert adaptive.cancelled == []


def test_sweep_tolerates_service_error_and_cleans_terminal_cleanup(tmp_path: Path) -> None:
    class SweepBroken(FakeAdaptive):
        def expire_sweep(self) -> int:
            raise RuntimeError("sweep failed")

    adaptive = SweepBroken(
        result(ResolutionKind.CLEANUP_REQUIRED, token="terminal-cleanup"),
        result(ResolutionKind.REUSED),
    )
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        failed = repo.get_task(task.task_id)
        repo.transition(task.task_id, TaskStatus.CANCELLED, expected_version=failed.version)
        controller.sweep()
        assert controller.interaction(task.task_id) is None
        assert adaptive.cleaned == ["terminal-cleanup"]


def test_missing_owned_lease_blocks_continue_without_consuming_token(tmp_path: Path) -> None:
    adaptive = FakeAdaptive(result(ResolutionKind.WAITING_FOR_USER, token="lease-secret"))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        repo.connection.execute("DELETE FROM task_interaction_leases WHERE task_id=?", (task.task_id,))
        assert controller.continue_verification(task.task_id).status is TaskStatus.WAITING_FOR_USER
        assert adaptive.continued == []


def test_ticket_without_browser_expiry_uses_controller_ttl(tmp_path: Path) -> None:
    ticket = VerificationTicket(
        "no-expiry-secret", VerificationStatus.WAITING, "https://example.test", None, 0
    )
    adaptive = FakeAdaptive(AdaptiveResult(ConfigResolution(ResolutionKind.WAITING_FOR_USER), ticket))
    with TaskRepository(tmp_path / "tasks.db") as repo:
        task = probing(repo)
        controller = AdaptiveTaskController(repo, adaptive)
        controller.probe_handler(None, task)
        assert controller.interaction(task.task_id).expires_at is not None  # type: ignore[union-attr]

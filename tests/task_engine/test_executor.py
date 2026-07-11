from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from novel_crawler.task_engine import (
    BackgroundTaskExecutor,
    CheckpointNotFound,
    ExecutorClosed,
    ExecutorQueueFull,
    TaskExecutionContext,
    TaskInputError,
    TaskRepository,
    TaskStatus,
    TaskVersionConflict,
    TerminalTaskError,
)


def _wait_for_status(
    repository: TaskRepository,
    task_id: str,
    expected: TaskStatus,
    *,
    timeout: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if repository.get_task(task_id).status is expected:
            return
        time.sleep(0.01)
    assert repository.get_task(task_id).status is expected


def test_checkpoint_cas_persists_across_restart_and_has_safe_repr(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    with TaskRepository(path) as repository:
        task = repository.create_task("https://example.test/book")
        saved = repository.save_checkpoint(task.task_id, "crawl-progress", {"chapter": 12}, expected_version=None)
        assert saved.version == 0
        assert "chapter" not in repr(saved)
        with pytest.raises(TaskVersionConflict, match="checkpoint_version"):
            repository.save_checkpoint(task.task_id, "crawl-progress", {"chapter": 13}, expected_version=None)
        updated = repository.save_checkpoint(task.task_id, "crawl-progress", {"chapter": 13}, expected_version=0)
        assert updated.version == 1

    with TaskRepository(path) as repository:
        loaded = repository.load_checkpoint(task.task_id, "crawl-progress")
        assert loaded.payload == {"chapter": 13}
        assert repository.list_checkpoints(task.task_id) == [loaded]
        repository.delete_checkpoint(task.task_id, "crawl-progress", expected_version=1)
        with pytest.raises(CheckpointNotFound):
            repository.load_checkpoint(task.task_id, "crawl-progress")


@pytest.mark.parametrize(
    "payload",
    [
        {"token": "private"},
        {"safe": "Authorization: Bearer abc123"},
        {"safe": "<html><body>private</body></html>"},
    ],
)
def test_checkpoint_reuses_size_and_privacy_validation(tmp_path: Path, payload: dict[str, str]) -> None:
    with TaskRepository(tmp_path / "tasks.db", max_metadata_bytes=32) as repository:
        task = repository.create_task("https://example.test/book")
        with pytest.raises(TaskInputError, match="metadata"):
            repository.save_checkpoint(task.task_id, "progress", payload, expected_version=None)
        with pytest.raises(TaskInputError, match="metadata"):
            repository.save_checkpoint(task.task_id, "progress", {"safe": "x" * 40}, expected_version=None)


def test_checkpoint_update_rolls_back_on_stale_version(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        repository.save_checkpoint(task.task_id, "progress", {"value": 1}, expected_version=None)
        with pytest.raises(TaskVersionConflict):
            repository.save_checkpoint(task.task_id, "progress", {"value": 2}, expected_version=9)
        assert repository.load_checkpoint(task.task_id, "progress").payload == {"value": 1}


def test_checkpoint_concurrent_cas_has_exactly_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    with TaskRepository(path) as first, TaskRepository(path) as second:
        task = first.create_task("https://example.test/book")
        first.save_checkpoint(task.task_id, "progress", {"value": 0}, expected_version=None)
        barrier = threading.Barrier(3)
        outcomes: list[str] = []

        def update(repository: TaskRepository, value: int) -> None:
            barrier.wait()
            try:
                repository.save_checkpoint(task.task_id, "progress", {"value": value}, expected_version=0)
                outcomes.append("saved")
            except TaskVersionConflict:
                outcomes.append("conflict")

        threads = [
            threading.Thread(target=update, args=(repository, value)) for repository, value in ((first, 1), (second, 2))
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        assert sorted(outcomes) == ["conflict", "saved"]
        assert first.load_checkpoint(task.task_id, "progress").version == 1


def test_executor_runs_bounded_workers_and_prevents_double_submit(tmp_path: Path) -> None:
    release = threading.Event()
    calls: list[str] = []

    def probe(context, task):  # type: ignore[no-untyped-def]
        calls.append(task.task_id)
        release.wait(2)
        context.check_control()
        return TaskStatus.VALIDATING

    with TaskRepository(tmp_path / "tasks.db") as repository:
        tasks = [repository.create_task(f"https://example.test/{index}") for index in range(3)]
        with BackgroundTaskExecutor(
            repository,
            {TaskStatus.PROBING: probe},
            max_workers=1,
            max_queue_size=1,
        ) as executor:
            assert executor.submit(tasks[0].task_id)
            assert not executor.submit(tasks[0].task_id)
            _wait_for_status(repository, tasks[0].task_id, TaskStatus.PROBING)
            assert executor.submit(tasks[1].task_id)
            with pytest.raises(ExecutorQueueFull):
                executor.submit(tasks[2].task_id)
            release.set()
            _wait_for_status(repository, tasks[1].task_id, TaskStatus.VALIDATING)
        assert calls == [tasks[0].task_id, tasks[1].task_id]


def test_resume_queue_full_rolls_back_to_resumable_state(tmp_path: Path) -> None:
    release = threading.Event()

    def probe(context, task):  # type: ignore[no-untyped-def]
        release.wait(2)
        return TaskStatus.VALIDATING

    with TaskRepository(tmp_path / "tasks.db") as repository:
        running = repository.create_task("https://example.test/running")
        queued = repository.create_task("https://example.test/queued")
        paused = repository.create_task("https://example.test/paused")
        for status in (TaskStatus.PROBING, TaskStatus.VALIDATING, TaskStatus.READY):
            paused = repository.transition(paused.task_id, status, expected_version=paused.version)
        paused = repository.transition(paused.task_id, TaskStatus.PAUSED, expected_version=paused.version)
        with BackgroundTaskExecutor(
            repository, {TaskStatus.PROBING: probe}, max_workers=1, max_queue_size=1
        ) as executor:
            executor.submit(running.task_id)
            _wait_for_status(repository, running.task_id, TaskStatus.PROBING)
            executor.submit(queued.task_id)
            with pytest.raises(ExecutorQueueFull):
                executor.resume(paused.task_id)
            rolled_back = repository.get_task(paused.task_id)
            assert rolled_back.status is TaskStatus.PAUSED
            assert rolled_back.resume_status is TaskStatus.READY
            release.set()


def test_executor_pause_resume_cancel_are_cooperative_and_terminal_idempotent(
    tmp_path: Path,
) -> None:
    entered = threading.Event()

    def crawl(context, task):  # type: ignore[no-untyped-def]
        entered.set()
        while True:
            context.check_control()
            time.sleep(0.005)

    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        task = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=0)
        task = repository.transition(task.task_id, TaskStatus.VALIDATING, expected_version=1)
        task = repository.transition(task.task_id, TaskStatus.READY, expected_version=2)
        with BackgroundTaskExecutor(repository, {TaskStatus.CRAWLING: crawl}, control_poll_interval=0.01) as executor:
            executor.submit(task.task_id)
            assert entered.wait(2)
            paused = executor.pause(task.task_id)
            assert paused.status is TaskStatus.PAUSED
            assert paused.resume_status is TaskStatus.CRAWLING
            resumed = executor.resume(task.task_id)
            assert resumed.status in {TaskStatus.CRAWLING, TaskStatus.PAUSED}
            _wait_for_status(repository, task.task_id, TaskStatus.CRAWLING)
            cancelled = executor.cancel(task.task_id)
            assert cancelled.status is TaskStatus.CANCELLED
            assert executor.cancel(task.task_id) == cancelled
            assert executor.pause(task.task_id) == cancelled
            assert executor.resume(task.task_id) == cancelled


def test_pausing_a_queued_task_does_not_implicitly_resume_it(tmp_path: Path) -> None:
    release = threading.Event()

    def crawl(context, task):  # type: ignore[no-untyped-def]
        release.wait(2)
        return TaskStatus.COMPLETED

    with TaskRepository(tmp_path / "tasks.db") as repository:
        tasks = []
        for index in range(2):
            task = repository.create_task(f"https://example.test/{index}")
            for status in (TaskStatus.PROBING, TaskStatus.VALIDATING, TaskStatus.READY):
                task = repository.transition(task.task_id, status, expected_version=task.version)
            tasks.append(task)
        with BackgroundTaskExecutor(repository, {TaskStatus.CRAWLING: crawl}, max_workers=1) as executor:
            executor.submit(tasks[0].task_id)
            _wait_for_status(repository, tasks[0].task_id, TaskStatus.CRAWLING)
            executor.submit(tasks[1].task_id)
            paused = executor.pause(tasks[1].task_id)
            assert paused.status is TaskStatus.PAUSED
            time.sleep(0.05)
            assert repository.get_task(tasks[1].task_id).status is TaskStatus.PAUSED
            release.set()
            _wait_for_status(repository, tasks[0].task_id, TaskStatus.COMPLETED)
            time.sleep(0.05)
            assert repository.get_task(tasks[1].task_id).status is TaskStatus.PAUSED


def test_stale_worker_generation_stops_after_pause_resume_race(tmp_path: Path) -> None:
    entered = threading.Event()
    continue_check = threading.Event()
    stale_stopped = threading.Event()

    def crawl(context, task):  # type: ignore[no-untyped-def]
        entered.set()
        continue_check.wait(2)
        try:
            context.check_control(force=True)
        except Exception:
            stale_stopped.set()
            raise
        return None

    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        for status in (TaskStatus.PROBING, TaskStatus.VALIDATING, TaskStatus.READY):
            task = repository.transition(task.task_id, status, expected_version=task.version)
        with BackgroundTaskExecutor(repository, {TaskStatus.CRAWLING: crawl}) as executor:
            executor.submit(task.task_id)
            assert entered.wait(2)
            executor.pause(task.task_id)
            executor.resume(task.task_id)
            continue_check.set()
            assert stale_stopped.wait(2)


def test_resume_reenters_handler_after_paused_worker_observes_control(tmp_path: Path) -> None:
    calls = 0
    paused_seen = threading.Event()

    def crawl(context, task):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            while True:
                try:
                    context.check_control(force=True)
                except Exception:
                    paused_seen.set()
                    raise
                time.sleep(0.005)
        return TaskStatus.COMPLETED

    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        for status in (TaskStatus.PROBING, TaskStatus.VALIDATING, TaskStatus.READY):
            task = repository.transition(task.task_id, status, expected_version=task.version)
        with BackgroundTaskExecutor(repository, {TaskStatus.CRAWLING: crawl}) as executor:
            executor.submit(task.task_id)
            _wait_for_status(repository, task.task_id, TaskStatus.CRAWLING)
            executor.pause(task.task_id)
            assert paused_seen.wait(2)
            executor.resume(task.task_id)
            _wait_for_status(repository, task.task_id, TaskStatus.COMPLETED)
        assert calls == 2


def test_startup_recovery_marks_interrupted_and_requeues_safe_states(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    with TaskRepository(path) as repository:
        created = repository.create_task("https://example.test/created")
        interrupted = repository.create_task("https://example.test/interrupted")
        interrupted = repository.transition(interrupted.task_id, TaskStatus.PROBING, expected_version=0)
        waiting = repository.create_task("https://example.test/waiting")
        waiting = repository.transition(waiting.task_id, TaskStatus.PROBING, expected_version=0)
        waiting = repository.transition(waiting.task_id, TaskStatus.WAITING_FOR_USER, expected_version=1)

    handled = threading.Event()

    def probe(context, task):  # type: ignore[no-untyped-def]
        handled.set()
        return TaskStatus.VALIDATING

    with TaskRepository(path) as repository:
        with BackgroundTaskExecutor(repository, {TaskStatus.PROBING: probe}, recover_on_start=True):
            assert handled.wait(2)
            _wait_for_status(repository, created.task_id, TaskStatus.VALIDATING)
            failed = repository.get_task(interrupted.task_id)
            assert failed.status is TaskStatus.RECOVERABLE_FAILED
            assert failed.resume_status is TaskStatus.PROBING
            assert repository.get_task(waiting.task_id).status is TaskStatus.WAITING_FOR_USER


def test_startup_recovery_never_blocks_when_bounded_queue_is_full(tmp_path: Path) -> None:
    release = threading.Event()
    entered = threading.Event()

    def probe(context, task):  # type: ignore[no-untyped-def]
        entered.set()
        release.wait(2)
        return TaskStatus.VALIDATING

    with TaskRepository(tmp_path / "tasks.db") as repository:
        for index in range(4):
            repository.create_task(f"https://example.test/{index}")
        executor = BackgroundTaskExecutor(
            repository,
            {TaskStatus.PROBING: probe},
            max_workers=1,
            max_queue_size=1,
            recover_on_start=True,
        )
        assert entered.wait(2)
        assert executor.startup_deferred_count >= 1
        release.set()
        assert executor.shutdown(timeout=3)


def test_two_executors_only_one_claims_a_task(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    calls = 0
    calls_lock = threading.Lock()

    def probe(context, task):  # type: ignore[no-untyped-def]
        nonlocal calls
        with calls_lock:
            calls += 1
        return TaskStatus.VALIDATING

    with TaskRepository(path) as first, TaskRepository(path) as second:
        task = first.create_task("https://example.test/book")
        with BackgroundTaskExecutor(first, {TaskStatus.PROBING: probe}) as one:
            with BackgroundTaskExecutor(second, {TaskStatus.PROBING: probe}) as two:
                barrier = threading.Barrier(3)
                threads = [
                    threading.Thread(target=lambda executor=executor: (barrier.wait(), executor.submit(task.task_id)))
                    for executor in (one, two)
                ]
                for thread in threads:
                    thread.start()
                barrier.wait()
                for thread in threads:
                    thread.join()
                _wait_for_status(first, task.task_id, TaskStatus.VALIDATING)
        assert calls == 1


def test_handler_exception_is_redacted_and_recoverable(tmp_path: Path) -> None:
    def probe(context, task):  # type: ignore[no-untyped-def]
        raise RuntimeError("Authorization: Bearer do-not-store")

    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        with BackgroundTaskExecutor(repository, {TaskStatus.PROBING: probe}) as executor:
            executor.submit(task.task_id)
            _wait_for_status(repository, task.task_id, TaskStatus.RECOVERABLE_FAILED)
        failed = repository.get_task(task.task_id)
        assert failed.error_code == "task_handler_failed"
        assert "Bearer" not in repr(failed)
        assert all("Bearer" not in repr(event) for event in repository.list_events(task.task_id))


def test_terminal_handler_failure_is_classified_without_message_leak(tmp_path: Path) -> None:
    def probe(context, task):  # type: ignore[no-untyped-def]
        raise TerminalTaskError("source_not_supported")

    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        with BackgroundTaskExecutor(repository, {TaskStatus.PROBING: probe}) as executor:
            executor.submit(task.task_id)
            _wait_for_status(repository, task.task_id, TaskStatus.TERMINAL_FAILED)
        assert repository.get_task(task.task_id).error_code == "source_not_supported"


def test_context_checkpoint_and_cancel_helpers(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        context = TaskExecutionContext(repository, task.task_id, task.version)
        saved = context.checkpoint("progress", {"chapter": 1}, expected_version=None)
        assert saved.payload == {"chapter": 1}
        assert not context.is_cancelled()
        cancelled = repository.transition(task.task_id, TaskStatus.CANCELLED, expected_version=0)
        assert context.is_cancelled()
        with pytest.raises(Exception, match="control"):
            context.check_control(force=True)
        assert cancelled.is_terminal


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_workers": 0},
        {"max_queue_size": 0},
        {"control_poll_interval": 0.0},
        {"handlers": {TaskStatus.CREATED: lambda context, task: None}},
    ],
)
def test_executor_rejects_unsafe_bounds(tmp_path: Path, kwargs: dict[str, object]) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        handlers = kwargs.pop("handlers", {})
        with pytest.raises(ValueError):
            BackgroundTaskExecutor(repository, handlers, **kwargs)  # type: ignore[arg-type]


def test_closed_executor_and_terminal_or_active_submit_are_idempotent(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        terminal = repository.create_task("https://example.test/terminal")
        terminal = repository.transition(terminal.task_id, TaskStatus.CANCELLED, expected_version=0)
        active = repository.create_task("https://example.test/active")
        active = repository.transition(active.task_id, TaskStatus.PROBING, expected_version=0)
        executor = BackgroundTaskExecutor(repository, {})
        assert not executor.submit(terminal.task_id)
        assert not executor.submit(active.task_id)
        assert executor.resume(active.task_id) == active
        assert not executor.shutdown(wait=False)
        assert executor.shutdown(wait=True, timeout=2)
        with pytest.raises(ExecutorClosed):
            executor.submit(active.task_id)
        with pytest.raises(ValueError, match="timeout"):
            executor.shutdown(timeout=-1)


def test_invalid_terminal_error_code_falls_back_to_public_code() -> None:
    assert TerminalTaskError("Authorization: Bearer private").error_code == "task_terminal_failure"


def test_shutdown_timeout_is_bounded_and_eventually_releases_workers(tmp_path: Path) -> None:
    release = threading.Event()

    def probe(context, task):  # type: ignore[no-untyped-def]
        release.wait(2)
        return TaskStatus.VALIDATING

    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        executor = BackgroundTaskExecutor(repository, {TaskStatus.PROBING: probe})
        executor.submit(task.task_id)
        _wait_for_status(repository, task.task_id, TaskStatus.PROBING)
        started = time.monotonic()
        assert not executor.shutdown(wait=True, timeout=0.01)
        assert time.monotonic() - started < 0.2
        release.set()
        assert executor.shutdown(wait=True, timeout=2)


def test_executor_processes_more_than_one_hundred_tasks(tmp_path: Path) -> None:
    def probe(context, task):  # type: ignore[no-untyped-def]
        return TaskStatus.VALIDATING

    with TaskRepository(tmp_path / "tasks.db") as repository:
        tasks = [repository.create_task(f"https://example.test/{index}") for index in range(128)]
        with BackgroundTaskExecutor(
            repository, {TaskStatus.PROBING: probe}, max_workers=8, max_queue_size=128
        ) as executor:
            for task in tasks:
                executor.submit(task.task_id)
            for task in tasks:
                _wait_for_status(repository, task.task_id, TaskStatus.VALIDATING, timeout=8)

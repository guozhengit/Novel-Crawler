from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from novel_crawler.task_engine.models import TERMINAL_STATUSES, CheckpointRecord, TaskRecord, TaskStatus
from novel_crawler.task_engine.repository import (
    InvalidTaskTransition,
    TaskNotFound,
    TaskRepository,
    TaskVersionConflict,
)

TaskHandler = Callable[["TaskExecutionContext", TaskRecord], TaskStatus | None]
_INTERRUPTED = frozenset({TaskStatus.PROBING, TaskStatus.VALIDATING, TaskStatus.CRAWLING})
_SUBMITTABLE = frozenset({TaskStatus.CREATED, TaskStatus.READY})


class TaskExecutorError(RuntimeError):
    pass


class ExecutorQueueFull(TaskExecutorError):
    pass


class ExecutorClosed(TaskExecutorError):
    pass


class TaskControlRequested(TaskExecutorError):
    pass


class TerminalTaskError(TaskExecutorError):
    def __init__(self, error_code: str = "task_terminal_failure") -> None:
        if (
            not isinstance(error_code, str)
            or not error_code
            or len(error_code) > 64
            or not error_code[0].isalpha()
            or not error_code.replace("_", "").isalnum()
            or error_code.casefold() != error_code
        ):
            error_code = "task_terminal_failure"
        super().__init__("task_terminal_failure")
        self.error_code = error_code


@dataclass
class TaskExecutionContext:
    repository: TaskRepository = field(repr=False)
    task_id: str
    expected_task_version: int = field(repr=False)
    control_poll_interval: float = field(default=0.05, repr=False)
    _last_poll: float = field(default=0.0, init=False, repr=False)

    def checkpoint(
        self,
        key: str,
        payload: Mapping[str, object],
        *,
        expected_version: int | None,
    ) -> CheckpointRecord:
        self.check_control(force=True)
        return self.repository.save_checkpoint(self.task_id, key, payload, expected_version=expected_version)

    def is_cancelled(self) -> bool:
        return self.repository.get_task(self.task_id).status is TaskStatus.CANCELLED

    def check_control(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_poll < self.control_poll_interval:
            return
        self._last_poll = now
        task = self.repository.get_task(self.task_id)
        if task.version != self.expected_task_version or task.status in {TaskStatus.PAUSED, TaskStatus.CANCELLED}:
            raise TaskControlRequested("task_control_requested")


class BackgroundTaskExecutor:
    def __init__(
        self,
        repository: TaskRepository,
        handlers: Mapping[TaskStatus, TaskHandler],
        *,
        max_workers: int = 4,
        max_queue_size: int = 128,
        control_poll_interval: float = 0.05,
        recover_on_start: bool = False,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 64:
            raise ValueError("max_workers must be between 1 and 64")
        if (
            isinstance(max_queue_size, bool)
            or not isinstance(max_queue_size, int)
            or not 1 <= max_queue_size <= 100_000
        ):
            raise ValueError("max_queue_size must be between 1 and 100000")
        if not 0.001 <= control_poll_interval <= 5.0:
            raise ValueError("control_poll_interval must be between 0.001 and 5")
        if any(status not in _INTERRUPTED or not callable(handler) for status, handler in handlers.items()):
            raise ValueError("handlers must target executable task statuses")
        self._repository = repository
        self._handlers = dict(handlers)
        self._queue: queue.Queue[tuple[str, bool]] = queue.Queue(maxsize=max_queue_size)
        self._control_poll_interval = control_poll_interval
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()
        self._pending_claimed: set[str] = set()
        self._startup_deferred_count = 0
        self._closing = threading.Event()
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"novel-task-worker-{index}",
                daemon=True,
            )
            for index in range(max_workers)
        ]
        if recover_on_start:
            self.recover_startup()
        for thread in self._threads:
            thread.start()
        if recover_on_start:
            safe_tasks = self._repository.list_tasks(
                statuses={TaskStatus.CREATED, TaskStatus.READY}, limit=1000
            )
            for index, task in enumerate(safe_tasks):
                try:
                    self.submit(task.task_id)
                except ExecutorQueueFull:
                    self._startup_deferred_count = len(safe_tasks) - index
                    break

    def __enter__(self) -> BackgroundTaskExecutor:
        return self

    @property
    def startup_deferred_count(self) -> int:
        return self._startup_deferred_count

    def __exit__(self, *_args: object) -> None:
        self.shutdown(wait=True, timeout=10.0)

    def submit(self, task_id: str) -> bool:
        if self._closing.is_set():
            raise ExecutorClosed("executor_closed")
        task = self._repository.get_task(task_id)
        if task.is_terminal:
            return False
        if task.status not in _SUBMITTABLE:
            return False
        with self._lock:
            if task_id in self._scheduled:
                return False
            self._scheduled.add(task_id)
        try:
            self._queue.put_nowait((task_id, False))
        except queue.Full as exc:
            with self._lock:
                self._scheduled.discard(task_id)
            raise ExecutorQueueFull("executor_queue_full") from exc
        return True

    def pause(self, task_id: str) -> TaskRecord:
        return self._request_status(task_id, TaskStatus.PAUSED)

    def cancel(self, task_id: str) -> TaskRecord:
        return self._request_status(task_id, TaskStatus.CANCELLED)

    def resume(self, task_id: str) -> TaskRecord:
        while True:
            task = self._repository.get_task(task_id)
            if task.is_terminal:
                return task
            if task.status not in {TaskStatus.PAUSED, TaskStatus.RECOVERABLE_FAILED}:
                return task
            if task.resume_status is None:
                return task
            try:
                resumed = self._repository.transition(
                    task_id,
                    task.resume_status,
                    expected_version=task.version,
                    reason="executor_resume",
                )
            except TaskVersionConflict:
                continue
            try:
                self._schedule_claimed(task_id)
            except (ExecutorClosed, ExecutorQueueFull):
                rollback = (
                    TaskStatus.PAUSED
                    if resumed.status is TaskStatus.READY
                    else TaskStatus.RECOVERABLE_FAILED
                )
                try:
                    self._repository.transition(
                        task_id,
                        rollback,
                        expected_version=resumed.version,
                        reason="executor_resume_backpressure",
                        error_code="executor_queue_full",
                    )
                except (TaskVersionConflict, InvalidTaskTransition):
                    pass
                raise
            return resumed

    def recover_startup(self) -> list[TaskRecord]:
        recovered: list[TaskRecord] = []
        while True:
            interrupted = self._repository.list_tasks(statuses=set(_INTERRUPTED), limit=1000)
            if not interrupted:
                break
            changed = 0
            for task in interrupted:
                try:
                    recovered.append(
                        self._repository.transition(
                            task.task_id,
                            TaskStatus.RECOVERABLE_FAILED,
                            expected_version=task.version,
                            reason="executor_restart_recovery",
                            error_code="task_interrupted",
                        )
                    )
                    changed += 1
                except (TaskVersionConflict, InvalidTaskTransition):
                    continue
            if changed == 0:
                break
        return recovered

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> bool:
        self._closing.set()
        if not wait:
            return not any(thread.is_alive() for thread in self._threads)
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must not be negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in self._threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        return not any(thread.is_alive() for thread in self._threads)

    def _schedule_claimed(self, task_id: str) -> None:
        if self._closing.is_set():
            raise ExecutorClosed("executor_closed")
        with self._lock:
            if task_id in self._scheduled:
                self._pending_claimed.add(task_id)
                return
            self._scheduled.add(task_id)
        try:
            self._queue.put_nowait((task_id, True))
        except queue.Full as exc:
            with self._lock:
                self._scheduled.discard(task_id)
            raise ExecutorQueueFull("executor_queue_full") from exc

    def _request_status(self, task_id: str, status: TaskStatus) -> TaskRecord:
        while True:
            task = self._repository.get_task(task_id)
            if task.is_terminal or task.status is status:
                return task
            try:
                return self._repository.transition(
                    task_id, status, expected_version=task.version, reason=f"executor_{status.value}"
                )
            except TaskVersionConflict:
                continue
            except InvalidTaskTransition:
                latest = self._repository.get_task(task_id)
                if latest.is_terminal or latest.status is status:
                    return latest
                raise

    def _worker(self) -> None:
        while True:
            if self._closing.is_set() and self._queue.empty():
                return
            try:
                task_id, preclaimed = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                claimed = self._repository.get_task(task_id) if preclaimed else self._claim(task_id)
                if claimed is not None:
                    self._run_claimed(claimed)
            except (TaskNotFound, TaskVersionConflict, InvalidTaskTransition):
                pass
            finally:
                resubmit = False
                with self._lock:
                    self._scheduled.discard(task_id)
                    if task_id in self._pending_claimed:
                        self._pending_claimed.discard(task_id)
                        self._scheduled.add(task_id)
                        resubmit = True
                self._queue.task_done()
                if resubmit:
                    try:
                        self._queue.put_nowait((task_id, True))
                    except queue.Full:
                        with self._lock:
                            self._scheduled.discard(task_id)
                        self._record_failure(task_id, terminal=False, error_code="executor_queue_full")

    def _claim(self, task_id: str) -> TaskRecord | None:
        task = self._repository.get_task(task_id)
        if task.status is TaskStatus.CREATED:
            target = TaskStatus.PROBING
        elif task.status is TaskStatus.READY:
            target = TaskStatus.CRAWLING
        else:
            return None
        return self._repository.transition(task_id, target, expected_version=task.version, reason="executor_claim")

    def _run_claimed(self, task: TaskRecord) -> None:
        context = TaskExecutionContext(
            repository=self._repository,
            task_id=task.task_id,
            expected_task_version=task.version,
            control_poll_interval=self._control_poll_interval,
        )
        current = task
        while current.status in self._handlers:
            try:
                context.check_control(force=True)
                next_status = self._handlers[current.status](context, current)
                context.check_control(force=True)
                if next_status is None:
                    return
                latest = self._repository.get_task(current.task_id)
                if latest.version != current.version or latest.status is not current.status:
                    return
                current = self._repository.transition(
                    current.task_id,
                    next_status,
                    expected_version=current.version,
                    reason="executor_handler_completed",
                )
                context.expected_task_version = current.version
            except TaskControlRequested:
                return
            except TerminalTaskError as exc:
                self._record_failure(current.task_id, terminal=True, error_code=exc.error_code)
                return
            except BaseException:
                self._record_failure(current.task_id, terminal=False, error_code="task_handler_failed")
                return

    def _record_failure(self, task_id: str, *, terminal: bool, error_code: str) -> None:
        target = TaskStatus.TERMINAL_FAILED if terminal else TaskStatus.RECOVERABLE_FAILED
        for _attempt in range(3):
            task = self._repository.get_task(task_id)
            if task.status in TERMINAL_STATUSES or task.status in {
                TaskStatus.PAUSED,
                TaskStatus.RECOVERABLE_FAILED,
            }:
                return
            try:
                self._repository.transition(
                    task_id,
                    target,
                    expected_version=task.version,
                    reason="executor_handler_failure",
                    error_code=error_code,
                )
                return
            except (TaskVersionConflict, InvalidTaskTransition):
                continue

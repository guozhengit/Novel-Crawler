from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from novel_crawler.application.errors import ApplicationError
from novel_crawler.application.models import CrawlOptions, InteractionView, TaskEventView, TaskView
from novel_crawler.task_engine import (
    ExecutorClosed,
    ExecutorQueueFull,
    InvalidTaskTransition,
    TaskInputError,
    TaskNotFound,
    TaskRecord,
    TaskRepository,
    TaskRepositoryError,
    TaskStatus,
    TaskVersionConflict,
)

_UNSAFE_TEXT = re.compile(
    r"https?://|(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s]+|"
    r"(?<![A-Za-z0-9_])\\\\[^\\\s]+\\[^\s]+|"
    r"(?<![A-Za-z0-9_])/(?:[^/\s]+/)*[^/\s]+|"
    r"(?:password|passwd|token|secret|cookie|authorization|profile[_ -]?path)\s*[:=]",
    re.I,
)
_BOOK_KEYS = ("id", "title", "author", "site", "total", "done", "failed", "pending")
_PROGRESS_KEYS = ("total", "done", "failed", "pending")
_FORMATS = frozenset({"txt", "epub", "md", "jsonl"})
_INTERACTION_KINDS = frozenset({"verification", "confirmation", "cleanup", "cancel_pending"})


class _ServiceState(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class _Executor(Protocol):
    def submit(self, task_id: str) -> bool: ...
    def resume(self, task_id: str) -> TaskRecord | None: ...
    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> bool: ...


class _Controller(Protocol):
    def interaction(self, task_id: str) -> _SafeSerializable | None: ...
    def continue_verification(self, task_id: str) -> TaskRecord | None: ...
    def confirm_config(self, task_id: str, selector_overrides: Mapping[str, str] | None = None) -> TaskRecord | None: ...
    def cancel_interaction(self, task_id: str) -> TaskRecord | None: ...
    def retry_cleanup(self, task_id: str) -> TaskRecord | None: ...
    def close(self) -> bool: ...


class _Crawler(Protocol):
    def list_books(self) -> list[dict[str, object]]: ...
    def progress(self, book_id: int) -> dict[str, int]: ...
    def report(self, book_id: int) -> str: ...
    def export(self, book_id: int, fmt: str = "txt", output: Path | None = None) -> Path: ...
    def delete_book(self, book_id: int) -> _SafeSerializable: ...


class _SafeSerializable(Protocol):
    def to_safe_dict(self) -> dict[str, object]: ...


class ApplicationService:
    """Single safe API boundary for future CLI and Web adapters."""

    def __init__(
        self,
        repository: TaskRepository,
        executor: _Executor,
        *,
        controller: _Controller | None = None,
        crawler: _Crawler | None = None,
        close_timeout: float = 10.0,
    ) -> None:
        if close_timeout < 0 or close_timeout > 300:
            raise ValueError("close_timeout must be between 0 and 300")
        self._repository = repository
        self._executor = executor
        self._controller = controller
        self._crawler = crawler
        self._close_timeout = close_timeout
        self._lock = threading.RLock()
        self._state = _ServiceState.OPEN
        self._controller_closed = controller is None
        self._executor_closed = False
        self._repository_closed = False
        self._crawler_closed = crawler is None

    def __enter__(self) -> ApplicationService:
        self._ensure_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def create_crawl_task(
        self,
        url: str,
        options: CrawlOptions | dict[str, Any] | None = None,
    ) -> TaskView:
        parsed = CrawlOptions.parse(options)
        if parsed.chase:
            raise ApplicationError("chase_unsupported")
        if parsed.concurrency != 1:
            raise ApplicationError("concurrency_unsupported")
        with self._lock:
            self._ensure_open()
            try:
                task = self._repository.create_task(url, metadata=parsed.to_metadata())
                self._executor.submit(task.task_id)
            except (ExecutorQueueFull, ExecutorClosed) as exc:
                code = "task_queue_full" if isinstance(exc, ExecutorQueueFull) else "task_executor_closed"
                if "task" not in locals():
                    raise ApplicationError(code, retryable=True) from None
                compensated = self._compensate_submission(task, code)
                raise ApplicationError(code, retryable=True, task_id=compensated.task_id) from None
            except TaskInputError as exc:
                raise ApplicationError(_repository_input_code(exc)) from None
            except TaskRepositoryError:
                raise ApplicationError("task_create_failed", retryable=True) from None
        return self._safe_task_view(task)

    def get_task(self, task_id: str) -> TaskView:
        with self._operation():
            try:
                return self._safe_task_view(self._repository.get_task(task_id))
            except TaskNotFound:
                raise ApplicationError("task_not_found") from None
            except TaskRepositoryError:
                raise ApplicationError("task_query_failed", retryable=True) from None

    def list_tasks(
        self,
        *,
        statuses: set[TaskStatus] | frozenset[TaskStatus] | None = None,
        limit: int = 1000,
    ) -> list[TaskView]:
        with self._operation():
            try:
                return [self._safe_task_view(task) for task in self._repository.list_tasks(statuses=statuses, limit=limit)]
            except TaskInputError:
                raise ApplicationError("task_query_invalid") from None
            except TaskRepositoryError:
                raise ApplicationError("task_query_failed", retryable=True) from None

    def task_events(self, task_id: str) -> list[TaskEventView]:
        with self._operation():
            try:
                return [
                    TaskEventView(
                        event.event_id,
                        event.task_id,
                        event.from_status.value if event.from_status is not None else None,
                        event.to_status.value,
                        event.task_version,
                        event.created_at,
                        event.error_code,
                    )
                    for event in self._repository.list_events(task_id)
                ]
            except TaskNotFound:
                raise ApplicationError("task_not_found") from None
            except TaskRepositoryError:
                raise ApplicationError("task_query_failed", retryable=True) from None

    def pause_task(self, task_id: str) -> TaskView:
        return self._control_transition(task_id, TaskStatus.PAUSED, "task_paused")

    def cancel_task(self, task_id: str) -> TaskView:
        return self._control_transition(task_id, TaskStatus.CANCELLED, "task_cancelled")

    def resume_task(self, task_id: str) -> TaskView:
        with self._operation():
            try:
                current = self._repository.get_task(task_id)
                if current.cleanup_required:
                    raise ApplicationError("cleanup_required", retryable=True, task_id=task_id)
                if current.is_terminal:
                    return self._safe_task_view(current)
                if current.status in {TaskStatus.PAUSED, TaskStatus.RECOVERABLE_FAILED}:
                    self._executor.resume(task_id)
                elif current.status in {TaskStatus.CREATED, TaskStatus.READY}:
                    self._executor.submit(task_id)
                return self._safe_task_view(self._repository.get_task(task_id))
            except ApplicationError:
                raise
            except TaskNotFound:
                raise ApplicationError("task_not_found") from None
            except ExecutorQueueFull:
                raise ApplicationError("task_queue_full", retryable=True, task_id=task_id) from None
            except ExecutorClosed:
                raise ApplicationError("task_executor_closed", retryable=True, task_id=task_id) from None
            except (InvalidTaskTransition, TaskVersionConflict):
                raise ApplicationError("task_state_conflict", retryable=True, task_id=task_id) from None
            except TaskRepositoryError:
                raise ApplicationError("task_control_failed", retryable=True, task_id=task_id) from None

    def continue_interaction(self, task_id: str) -> TaskView:
        return self._interaction_action("continue_verification", task_id)

    continue_verification = continue_interaction

    def confirm_interaction(
        self,
        task_id: str,
        selector_overrides: Mapping[str, str] | None = None,
    ) -> TaskView:
        return self._interaction_action("confirm_config", task_id, selector_overrides)

    confirm_config = confirm_interaction

    def cancel_interaction(self, task_id: str) -> TaskView:
        return self._interaction_action("cancel_interaction", task_id)

    def retry_cleanup(self, task_id: str) -> TaskView:
        return self._interaction_action("retry_cleanup", task_id)

    def list_books(self) -> list[dict[str, object]]:
        with self._operation():
            crawler = self._require_crawler()
            try:
                return [_allowlist(row, _BOOK_KEYS) for row in crawler.list_books()]
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def book_progress(self, book_id: int) -> dict[str, int]:
        with self._operation():
            crawler = self._require_crawler()
            _positive_id(book_id, "book_id_invalid")
            try:
                raw = crawler.progress(book_id)
                return {key: _safe_count(raw.get(key, 0)) for key in _PROGRESS_KEYS}
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def book_report(self, book_id: int) -> str:
        with self._operation():
            crawler = self._require_crawler()
            _positive_id(book_id, "book_id_invalid")
            try:
                report = crawler.report(book_id)
                return "\n".join(_safe_line(line) for line in report[:32_768].splitlines())
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def export_book(self, book_id: int, fmt: str = "txt") -> dict[str, bool | str]:
        with self._operation():
            crawler = self._require_crawler()
            _positive_id(book_id, "book_id_invalid")
            if not isinstance(fmt, str) or fmt not in _FORMATS:
                raise ApplicationError("export_format_invalid")
            try:
                crawler.export(book_id, fmt)
                return {"completed": True, "format": fmt}
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def delete_book(self, book_id: int) -> dict[str, object]:
        with self._operation():
            crawler = self._require_crawler()
            _positive_id(book_id, "book_id_invalid")
            try:
                result = crawler.delete_book(book_id)
                safe = result.to_safe_dict()
                return {key: value for key, value in safe.items() if key in {"job_id", "state", "completed", "cleanup_required", "manual_cleanup_required", "error_code"}}
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def close(self) -> bool:
        with self._lock:
            if self._state is _ServiceState.CLOSED:
                return True
            self._state = _ServiceState.CLOSING
            if not self._executor_closed:
                try:
                    self._executor_closed = bool(
                        self._executor.shutdown(wait=True, timeout=self._close_timeout)
                    )
                except Exception:
                    self._executor_closed = False
            if not self._executor_closed:
                return False
            if not self._controller_closed and self._controller is not None:
                try:
                    self._controller_closed = bool(self._controller.close())
                except Exception:
                    self._controller_closed = False
            if not self._controller_closed:
                return False
            if not self._repository_closed:
                try:
                    self._repository.close()
                    self._repository_closed = True
                except Exception:
                    return False
            if not self._crawler_closed and self._crawler is not None:
                try:
                    close = getattr(self._crawler, "close", None)
                    if callable(close):
                        close()
                    else:
                        storage = getattr(self._crawler, "storage", None)
                        storage_close = getattr(storage, "close", None)
                        if callable(storage_close):
                            storage_close()
                    self._crawler_closed = True
                except Exception:
                    return False
            self._state = _ServiceState.CLOSED
            return True

    def _control_transition(self, task_id: str, target: TaskStatus, reason: str) -> TaskView:
        with self._operation():
            for _ in range(32):
                try:
                    current = self._repository.get_task(task_id)
                    if current.is_terminal or current.status is target:
                        return self._safe_task_view(current)
                    updated = self._repository.transition(
                        task_id,
                        target,
                        expected_version=current.version,
                        reason=reason,
                    )
                    return self._safe_task_view(updated)
                except TaskVersionConflict:
                    continue
                except TaskNotFound:
                    raise ApplicationError("task_not_found") from None
                except InvalidTaskTransition:
                    raise ApplicationError("task_state_conflict", retryable=True, task_id=task_id) from None
                except TaskRepositoryError:
                    raise ApplicationError("task_control_failed", retryable=True, task_id=task_id) from None
            raise ApplicationError("task_state_conflict", retryable=True, task_id=task_id)

    def _interaction_action(self, name: str, task_id: str, *args: object) -> TaskView:
        with self._operation():
            if self._controller is None:
                raise ApplicationError("interaction_unavailable")
            try:
                getattr(self._controller, name)(task_id, *args)
            except TaskNotFound:
                raise ApplicationError("task_not_found") from None
            except (InvalidTaskTransition, TaskVersionConflict):
                raise ApplicationError("task_state_conflict", retryable=True, task_id=task_id) from None
            except TaskRepositoryError:
                raise ApplicationError("interaction_failed", retryable=True, task_id=task_id) from None
            except Exception:
                raise ApplicationError("interaction_failed", retryable=True, task_id=task_id) from None
            return self._safe_task_view(self._repository.get_task(task_id))

    def _safe_task_view(self, task: TaskRecord) -> TaskView:
        try:
            return self._task_view(task)
        except ApplicationError:
            raise
        except Exception:
            raise ApplicationError(
                "task_view_failed", retryable=True, task_id=task.task_id
            ) from None

    def _task_view(self, task: TaskRecord) -> TaskView:
        checkpoints = self._repository.list_checkpoints(task.task_id)
        interaction = None
        if self._controller is not None:
            summary = self._controller.interaction(task.task_id)
            if summary is not None:
                raw = summary.to_safe_dict()
                safe: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}
                raw_kind = safe.get("kind")
                kind = raw_kind if isinstance(raw_kind, str) and raw_kind in _INTERACTION_KINDS else "unknown"
                interaction = InteractionView(
                    kind=kind,
                    attempt=_safe_count(safe.get("attempt", 0)),
                    expires_at=_safe_timestamp(safe.get("expires_at")),
                    verification_required=safe.get("verification_required") is True,
                    confirmation_required=safe.get("confirmation_required") is True,
                    cleanup_required=safe.get("cleanup_required") is True,
                )
        progress = _progress_from_metadata(task.metadata)
        return TaskView(
            task_id=task.task_id,
            status=task.status.value,
            version=task.version,
            created_at=task.created_at,
            updated_at=task.updated_at,
            error_code=task.error_code,
            resume_status=task.resume_status.value if task.resume_status is not None else None,
            terminal=task.is_terminal,
            cleanup_required=task.cleanup_required,
            checkpoint_count=len(checkpoints),
            checkpoint_version_total=sum(item.version for item in checkpoints),
            interaction=interaction,
            progress=MappingProxyType(progress),
        )

    def _compensate_submission(self, task: TaskRecord, code: str) -> TaskRecord:
        try:
            return self._repository.transition(
                task.task_id,
                TaskStatus.PAUSED,
                expected_version=task.version,
                reason="submission_deferred",
                error_code=code,
            )
        except TaskRepositoryError:
            return self._repository.get_task(task.task_id)

    def _require_crawler(self) -> _Crawler:
        self._ensure_open()
        if self._crawler is None:
            raise ApplicationError("crawler_unavailable")
        return self._crawler

    def _ensure_open(self) -> None:
        with self._lock:
            if self._state is _ServiceState.CLOSING:
                raise ApplicationError("service_closing", retryable=True)
            if self._state is _ServiceState.CLOSED:
                raise ApplicationError("service_closed")

    @contextmanager
    def _operation(self) -> Iterator[None]:
        with self._lock:
            self._ensure_open()
            yield


def _repository_input_code(exc: TaskInputError) -> str:
    value = str(exc).strip("'\"")
    return value if value in {"source_url_invalid", "metadata_invalid"} else "task_input_invalid"


def _positive_id(value: object, code: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 2_147_483_647:
        raise ApplicationError(code)


def _safe_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, 2_147_483_647)


def _safe_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64 or _UNSAFE_TEXT.search(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _safe_text(value: object, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or _UNSAFE_TEXT.search(value) or any(ord(char) < 32 for char in value):
        return "[redacted]"
    return value[:maximum]


def _safe_line(value: str) -> str:
    return "[redacted]" if _UNSAFE_TEXT.search(value) or any(ord(char) < 32 and char != "\t" for char in value) else value[:1000]


def _allowlist(row: Mapping[str, object], keys: tuple[str, ...]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key in keys:
        if key not in row:
            continue
        value = row[key]
        if key in {"id", "total", "done", "failed", "pending"}:
            safe[key] = _safe_count(value)
        else:
            safe[key] = _safe_text(value)
    return safe


def _progress_from_metadata(metadata: Mapping[str, object]) -> dict[str, int]:
    value = metadata.get("progress")
    if not isinstance(value, Mapping):
        return {}
    return {key: _safe_count(value.get(key, 0)) for key in _PROGRESS_KEYS}

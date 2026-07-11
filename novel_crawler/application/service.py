from __future__ import annotations

import ipaddress
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

from novel_crawler.application.errors import ApplicationError
from novel_crawler.application.models import CrawlOptions, InteractionView, TaskEventView, TaskView
from novel_crawler.core.domains import canonical_domain
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
    def validate(self, book_id: int) -> object: ...
    def fix_titles(self, book_id: int, dry_run: bool = False) -> object: ...
    def dedup(self, book_id: int, remove: bool = False) -> object: ...
    def logs(self, book_id: int | None = None, limit: int = 50) -> list[dict[str, object]]: ...
    def retry_failed(self, book_id: int, *, export: bool = True, concurrency: int = 1) -> None: ...
    def export_all(self, fmt: str = "txt") -> list[Path]: ...
    def retry_all_failed(self) -> int: ...
    def preview_chapter(self, book_id: int, chapter_index: int, length: int = 500) -> str: ...
    def stats(self) -> dict[str, object]: ...
    def validate_config(self, config_path: Path) -> dict[str, object]: ...


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
        try:
            return self._safe_task_view(task)
        except ApplicationError as exc:
            # Persistence and executor submission have already succeeded.  A
            # secondary checkpoint/interaction projection failure must not
            # hide the durable task identifier from the caller.
            if exc.code == "task_view_failed":
                return self._minimal_task_view(task)
            raise

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

    def validate_book(self, book_id: int) -> dict[str, object]:
        with self._operation():
            crawler = self._require_crawler()
            _positive_id(book_id, "book_id_invalid")
            try:
                report = crawler.validate(book_id)
                issues = []
                for issue in list(getattr(report, "issues", ()))[:100]:
                    issues.append(
                        {
                            "level": _safe_text(getattr(issue, "level", "unknown"), maximum=16),
                            "code": _safe_text(getattr(issue, "code", "unknown"), maximum=64),
                            "message": _safe_text(getattr(issue, "message", ""), maximum=500),
                        }
                    )
                return {
                    "book_id": _safe_count(getattr(report, "book_id", book_id)),
                    "total": _safe_count(getattr(report, "total", 0)),
                    "done": _safe_count(getattr(report, "done", 0)),
                    "failed": _safe_count(getattr(report, "failed", 0)),
                    "pending": _safe_count(getattr(report, "pending", 0)),
                    "ok": getattr(report, "ok", False) is True,
                    "issues": issues,
                }
            except ApplicationError:
                raise
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def fix_book_titles(self, book_id: int) -> dict[str, object]:
        return self._bounded_result(
            book_id,
            lambda crawler: crawler.fix_titles(book_id, dry_run=False),
            ("total", "fixed"),
        )

    def deduplicate_book(self, book_id: int, remove: bool = False) -> dict[str, object]:
        if not isinstance(remove, bool):
            raise ApplicationError("remove_invalid")
        return self._bounded_result(
            book_id,
            lambda crawler: crawler.dedup(book_id, remove=remove),
            ("total", "exact_dupes", "similar_dupes"),
        )

    def book_logs(self, book_id: int | None = None, limit: int = 50) -> list[dict[str, object]]:
        with self._operation():
            crawler = self._require_crawler()
            if book_id is not None:
                _positive_id(book_id, "book_id_invalid")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
                raise ApplicationError("limit_invalid")
            try:
                rows = crawler.logs(book_id, limit)
                safe_rows: list[dict[str, object]] = []
                for row in rows[:limit]:
                    safe_rows.append(
                        {
                            "created_at": _safe_timestamp(row.get("created_at")),
                            "level": _safe_text(row.get("level", "unknown"), maximum=16),
                            "book_id": _safe_count(row.get("book_id", 0)),
                            "chapter_index": _safe_count(row.get("chapter_index", 0)),
                            "message": _safe_text(row.get("message", ""), maximum=1000),
                        }
                    )
                return safe_rows
            except ApplicationError:
                raise
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def retry_failed_chapters(self, book_id: int, export: bool = True, concurrency: int = 1) -> dict[str, bool]:
        with self._operation():
            crawler = self._require_crawler()
            _positive_id(book_id, "book_id_invalid")
            if not isinstance(export, bool):
                raise ApplicationError("export_invalid")
            if isinstance(concurrency, bool) or concurrency != 1:
                raise ApplicationError("concurrency_unsupported")
            try:
                crawler.retry_failed(book_id, export=export, concurrency=concurrency)
                return {"completed": True}
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def export_all_books(self, fmt: str = "txt") -> dict[str, object]:
        with self._operation():
            crawler = self._require_crawler()
            if not isinstance(fmt, str) or fmt not in _FORMATS:
                raise ApplicationError("export_format_invalid")
            try:
                books = crawler.list_books()
                requested = min(len(books), 2_147_483_647)
                attempted_books = books[:1000]
                remaining = max(0, requested - len(attempted_books))
                succeeded = 0
                failed = 0
                for book in attempted_books:
                    book_id = _safe_count(book.get("id"))
                    if book_id < 1:
                        failed += 1
                        continue
                    try:
                        crawler.export(book_id, fmt)
                        succeeded += 1
                    except Exception:
                        failed += 1
                return {
                    "best_effort": True,
                    "requested": requested,
                    "attempted": len(attempted_books),
                    "succeeded": succeeded,
                    "failed": failed,
                    "remaining": remaining,
                    "format": fmt,
                    "error_codes": [
                        code
                        for code, present in (
                            ("export_failed", failed > 0),
                            ("export_limit_reached", remaining > 0),
                        )
                        if present
                    ],
                }
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def retry_all_failed_chapters(self) -> dict[str, object]:
        with self._operation():
            crawler = self._require_crawler()
            try:
                books = crawler.list_books()
                targets = [
                    _safe_count(book.get("id"))
                    for book in books
                    if _safe_count(book.get("failed")) > 0 and _safe_count(book.get("id")) > 0
                ]
                attempted_targets = targets[:1000]
                remaining = len(targets) - len(attempted_targets)
                succeeded = 0
                failed = 0
                for book_id in attempted_targets:
                    try:
                        crawler.retry_failed(book_id, export=False, concurrency=1)
                        succeeded += 1
                    except Exception:
                        failed += 1
                return {
                    "best_effort": True,
                    "requested": len(targets),
                    "attempted": len(attempted_targets),
                    "succeeded": succeeded,
                    "failed": failed,
                    "remaining": remaining,
                    "error_codes": [
                        code
                        for code, present in (
                            ("retry_failed", failed > 0),
                            ("retry_limit_reached", remaining > 0),
                        )
                        if present
                    ],
                }
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def preview_book_chapter(self, book_id: int, chapter_index: int, length: int = 500) -> str:
        with self._operation():
            crawler = self._require_crawler()
            _positive_id(book_id, "book_id_invalid")
            _positive_id(chapter_index, "chapter_index_invalid")
            if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= 10_000:
                raise ApplicationError("length_invalid")
            try:
                value = crawler.preview_chapter(book_id, chapter_index, length)
                return "\n".join(_safe_line(line) for line in value[:12_000].splitlines())
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def book_stats(self) -> dict[str, object]:
        with self._operation():
            crawler = self._require_crawler()
            try:
                raw = crawler.stats()
                sites_raw = raw.get("sites")
                sites: dict[str, int] = {}
                if isinstance(sites_raw, Mapping):
                    for key, value in list(sites_raw.items())[:100]:
                        safe_key = _safe_text(key, maximum=128)
                        if safe_key != "[redacted]":
                            sites[safe_key] = _safe_count(value)
                result: dict[str, object] = {
                    key: _safe_count(raw.get(key, 0))
                    for key in ("books", "chapters_total", "chapters_done", "chapters_failed", "chapters_pending")
                }
                rate = raw.get("completion_rate", 0)
                result["completion_rate"] = float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0 <= rate <= 100 else 0.0
                result["sites"] = sites
                return result
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def validate_site_config(self, config_path: Path) -> dict[str, object]:
        with self._operation():
            crawler = self._require_crawler()
            if not isinstance(config_path, Path):
                raise ApplicationError("config_path_invalid")
            try:
                raw = crawler.validate_config(config_path)
                return {
                    "valid": raw.get("valid") is True,
                    "site": _safe_text(raw.get("site", ""), maximum=128),
                    "domain": _safe_text(raw.get("domain", ""), maximum=253),
                    "errors": _safe_string_list(raw.get("errors")),
                    "warnings": _safe_string_list(raw.get("warnings")),
                }
            except Exception:
                raise ApplicationError("crawler_operation_failed", retryable=True) from None

    def create_crawl_tasks_from_file(
        self,
        file_path: Path,
        concurrency: int = 1,
        max_chapters: int | None = None,
    ) -> dict[str, object]:
        if isinstance(concurrency, bool) or concurrency != 1:
            raise ApplicationError("concurrency_unsupported")
        options = CrawlOptions.parse(
            CrawlOptions(max_chapters=max_chapters, concurrency=concurrency, export=False)
        )
        if not isinstance(file_path, Path):
            raise ApplicationError("batch_file_invalid")
        try:
            if file_path.is_symlink() or not file_path.is_file() or file_path.stat().st_size > 1_048_576:
                raise ApplicationError("batch_file_invalid")
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except ApplicationError:
            raise
        except (OSError, UnicodeError):
            raise ApplicationError("batch_file_invalid") from None
        urls = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
        if not urls or len(urls) > 1000:
            raise ApplicationError("batch_file_invalid")
        for url in urls:
            parsed = urlsplit(url)
            if (
                len(url) > 2048
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ApplicationError("source_url_invalid")
        views: list[TaskView] = []
        submitted = 0
        failed = 0
        attempted = 0
        error_code: str | None = None
        for url in urls:
            attempted += 1
            try:
                view = self.create_crawl_task(url, options)
                views.append(view)
                submitted += 1
            except ApplicationError as exc:
                failed = 1
                error_code = exc.code
                if exc.task_id is not None:
                    try:
                        views.append(self.get_task(exc.task_id))
                    except ApplicationError:
                        pass
                break
        return {
            "requested": len(urls),
            "created": len(views),
            "submitted": submitted,
            "failed": failed,
            "not_started": max(0, len(urls) - attempted),
            "error_code": error_code,
            "tasks": [view.to_safe_dict() for view in views],
        }

    def _bounded_result(
        self,
        book_id: int,
        operation: Callable[[_Crawler], object],
        count_fields: tuple[str, ...],
    ) -> dict[str, object]:
        with self._operation():
            self._require_crawler()
            _positive_id(book_id, "book_id_invalid")
            try:
                crawler = self._require_crawler()
                result = operation(crawler)
                safe: dict[str, object] = {
                    field: _safe_count(getattr(result, field, 0)) for field in count_fields
                }
                safe["details"] = _safe_string_list(getattr(result, "details", ()), maximum=20)
                return safe
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
                    safe_origin=_safe_origin(safe.get("safe_origin")),
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

    @staticmethod
    def _minimal_task_view(task: TaskRecord) -> TaskView:
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
            checkpoint_count=0,
            checkpoint_version_total=0,
            interaction=None,
            progress=MappingProxyType({}),
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


def _safe_origin(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 300 or not value.isascii():
        return None
    if "://" not in value:
        display = _canonical_ascii_host(value)
        return display.strip("[]") if display is not None else None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65_535
    ):
        return None
    display_host = _canonical_ascii_host(host)
    if display_host is None:
        return None
    default_port = 443 if parsed.scheme == "https" else 80
    authority = display_host if port in {None, default_port} else f"{display_host}:{port}"
    return f"{parsed.scheme}://{authority}/"


def _canonical_ascii_host(host: str) -> str | None:
    if not host or not host.isascii() or "%" in host or host.endswith("."):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Numeric-only names and non-canonical dotted numbers are interpreted
        # differently by legacy URL stacks; never present them as DNS names.
        if host.isdigit() or re.fullmatch(r"[0-9.]+", host):
            return None
        try:
            canonical = canonical_domain(host)
        except (TypeError, ValueError):
            return None
        return canonical if canonical == host.lower().rstrip(".") else None
    canonical_ip = address.compressed.lower()
    if canonical_ip != host.lower():
        return None
    return f"[{canonical_ip}]" if address.version == 6 else canonical_ip


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


def _safe_string_list(value: object, *, maximum: int = 100) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_safe_text(item, maximum=1000) for item in value[:maximum]]

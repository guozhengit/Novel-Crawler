from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from novel_crawler.task_engine.models import ALLOWED_TRANSITIONS, TaskEvent, TaskRecord, TaskStatus

SCHEMA_VERSION = 2
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE_KEYS = (
    "authorization",
    "browser_token",
    "content",
    "cookie",
    "credential",
    "html",
    "page_body",
    "password",
    "profile_path",
    "resume_status",
    "secret",
    "token",
)
_SENSITIVE_TEXT_MARKERS = (
    "authorization:",
    "bearer ",
    "browser_token",
    "cookie=",
    "<body",
    "<html",
    "page_body",
    "password=",
    "password:",
    "profile_path",
    "profile path",
    "secret=",
    "secret:",
    "token=",
    "token:",
)
_STATUS_SQL = ", ".join(f"'{status.value}'" for status in TaskStatus)


class TaskRepositoryError(RuntimeError):
    pass


class TaskInputError(TaskRepositoryError, ValueError):
    pass


class TaskNotFound(TaskRepositoryError, KeyError):
    pass


class TaskVersionConflict(TaskRepositoryError):
    pass


class InvalidTaskTransition(TaskRepositoryError):
    pass


class TaskRepository:
    def __init__(
        self,
        db_path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        max_metadata_bytes: int = 8192,
        max_error_length: int = 1000,
    ) -> None:
        if busy_timeout_ms < 1 or busy_timeout_ms > 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")
        if max_metadata_bytes < 2 or max_metadata_bytes > 1_000_000:
            raise ValueError("max_metadata_bytes must be between 2 and 1000000")
        if max_error_length < 1 or max_error_length > 10_000:
            raise ValueError("max_error_length must be between 1 and 10000")
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._max_metadata_bytes = max_metadata_bytes
        self._max_error_length = max_error_length
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        try:
            self._migrate()
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @property
    def schema_version(self) -> int:
        row = self._connection.execute("SELECT MAX(version) FROM task_schema_migrations").fetchone()
        return int(row[0] or 0)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> TaskRepository:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _migrate(self) -> None:
        with self._transaction():
            self._connection.execute(
                """
            CREATE TABLE IF NOT EXISTS task_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
                """
            )
            row = self._connection.execute("SELECT MAX(version) FROM task_schema_migrations").fetchone()
            if row is not None and int(row[0] or 0) > SCHEMA_VERSION:
                raise TaskInputError("task_schema_is_newer")
        statements = (
            f"""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                source_url TEXT NOT NULL CHECK(length(source_url) BETWEEN 1 AND 2048),
                status TEXT NOT NULL CHECK(status IN ({_STATUS_SQL})),
                version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
                metadata_json TEXT NOT NULL DEFAULT '{{}}'
                    CHECK(length(CAST(metadata_json AS BLOB)) <= 1000000),
                error_code TEXT,
                error_message TEXT CHECK(error_message IS NULL OR length(error_message) <= 10000),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS task_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                from_status TEXT CHECK(from_status IS NULL OR from_status IN ({_STATUS_SQL})),
                to_status TEXT NOT NULL CHECK(to_status IN ({_STATUS_SQL})),
                task_version INTEGER NOT NULL CHECK(task_version >= 0),
                reason TEXT CHECK(reason IS NULL OR length(reason) <= 256),
                metadata_json TEXT NOT NULL DEFAULT '{{}}'
                    CHECK(length(CAST(metadata_json AS BLOB)) <= 1000000),
                error_code TEXT,
                error_message TEXT CHECK(error_message IS NULL OR length(error_message) <= 10000),
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
                UNIQUE(task_id, task_version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                task_id TEXT NOT NULL,
                checkpoint_key TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
                    CHECK(length(CAST(payload_json AS BLOB)) <= 1000000),
                version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(task_id, checkpoint_key),
                FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, event_id)",
        )
        with self._transaction():
            for statement in statements:
                self._connection.execute(statement)
            self._connection.execute(
                "INSERT OR IGNORE INTO task_schema_migrations(version, applied_at) VALUES(?, ?)",
                (1, _now()),
            )
        if self.schema_version < 2:
            with self._transaction():
                columns = {
                    str(row[1]) for row in self._connection.execute("PRAGMA table_info(tasks)").fetchall()
                }
                if "resume_status" not in columns:
                    self._connection.execute(
                        f"ALTER TABLE tasks ADD COLUMN resume_status TEXT "
                        f"CHECK(resume_status IS NULL OR resume_status IN ({_STATUS_SQL}))"
                    )
                self._connection.execute(
                    "INSERT INTO task_schema_migrations(version, applied_at) VALUES(2, ?)", (_now(),)
                )
        columns = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(tasks)").fetchall()}
        if "resume_status" not in columns:
            raise TaskInputError("task_schema_corrupt")

    def create_task(self, source_url: str, *, metadata: Mapping[str, Any] | None = None) -> TaskRecord:
        source_url = _validate_source_url(source_url)
        metadata_value, metadata_json = self._validate_metadata(metadata)
        task_id = uuid.uuid4().hex
        timestamp = _now()
        with self._lock, self._transaction():
            self._connection.execute(
                """
                INSERT INTO tasks(
                    task_id, source_url, status, version, metadata_json, created_at, updated_at
                ) VALUES(?, ?, ?, 0, ?, ?, ?)
                """,
                (task_id, source_url, TaskStatus.CREATED.value, metadata_json, timestamp, timestamp),
            )
            self._connection.execute(
                """
                INSERT INTO task_events(
                    task_id, from_status, to_status, task_version, metadata_json, created_at
                ) VALUES(?, NULL, ?, 0, '{}', ?)
                """,
                (task_id, TaskStatus.CREATED.value, timestamp),
            )
        return TaskRecord(
            task_id=task_id,
            source_url=source_url,
            status=TaskStatus.CREATED,
            version=0,
            metadata=metadata_value,
            created_at=timestamp,
            updated_at=timestamp,
            resume_status=None,
        )

    def get_task(self, task_id: str) -> TaskRecord:
        with self._lock:
            row = self._connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound("task_not_found")
        return _task_from_row(row)

    def list_tasks(
        self, *, statuses: set[TaskStatus] | frozenset[TaskStatus] | None = None, limit: int = 1000
    ) -> list[TaskRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise TaskInputError("limit_invalid")
        parameters: list[object] = []
        where = ""
        if statuses is not None:
            if not isinstance(statuses, (set, frozenset)) or any(
                not isinstance(status, TaskStatus) for status in statuses
            ):
                raise TaskInputError("statuses_invalid")
            if not statuses:
                return []
            ordered = sorted(statuses, key=lambda status: status.value)
            where = f"WHERE status IN ({', '.join('?' for _ in ordered)})"
            parameters.extend(status.value for status in ordered)
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM tasks {where} ORDER BY rowid LIMIT ?", parameters
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def list_events(self, task_id: str) -> list[TaskEvent]:
        with self._lock:
            exists = self._connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if exists is None:
                raise TaskNotFound("task_not_found")
            rows = self._connection.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY event_id", (task_id,)
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def transition(
        self,
        task_id: str,
        to_status: TaskStatus,
        *,
        expected_version: int,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> TaskRecord:
        if not isinstance(to_status, TaskStatus):
            raise TaskInputError("status_invalid")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise TaskInputError("expected_version_invalid")
        reason = _validate_optional_text(reason, "reason", 256, reject_sensitive=True)
        error_code = _validate_error_code(error_code)
        error_message = _validate_optional_text(
            error_message, "error_message", self._max_error_length, reject_sensitive=True
        )
        event_metadata, event_metadata_json = self._validate_metadata(metadata)
        timestamp = _now()
        with self._lock, self._transaction():
            row = self._connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFound("task_not_found")
            current = _task_from_row(row)
            if current.version != expected_version:
                raise TaskVersionConflict("task_version_conflict")
            if to_status not in ALLOWED_TRANSITIONS[current.status]:
                raise InvalidTaskTransition(
                    f"transition_not_allowed:{current.status.value}:{to_status.value}"
                )
            if current.status in {TaskStatus.PAUSED, TaskStatus.RECOVERABLE_FAILED} and to_status not in {
                TaskStatus.TERMINAL_FAILED,
                TaskStatus.CANCELLED,
            }:
                if current.resume_status is None or to_status is not current.resume_status:
                    raise InvalidTaskTransition(
                        f"resume_not_allowed:{current.status.value}:{to_status.value}"
                    )
            if to_status in {TaskStatus.PAUSED, TaskStatus.RECOVERABLE_FAILED}:
                resume_status = current.status
            else:
                resume_status = None
            next_version = current.version + 1
            cursor = self._connection.execute(
                """
                UPDATE tasks
                SET status=?, version=?, resume_status=?, error_code=?, error_message=?, updated_at=?
                WHERE task_id=? AND version=?
                """,
                (
                    to_status.value,
                    next_version,
                    resume_status.value if resume_status is not None else None,
                    error_code,
                    error_message,
                    timestamp,
                    task_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskVersionConflict("task_version_conflict")
            self._connection.execute(
                """
                INSERT INTO task_events(
                    task_id, from_status, to_status, task_version, reason, metadata_json,
                    error_code, error_message, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    current.status.value,
                    to_status.value,
                    next_version,
                    reason,
                    event_metadata_json,
                    error_code,
                    error_message,
                    timestamp,
                ),
            )
        return TaskRecord(
            task_id=task_id,
            source_url=current.source_url,
            status=to_status,
            version=next_version,
            metadata=current.metadata,
            error_code=error_code,
            error_message=error_message,
            resume_status=resume_status,
            created_at=current.created_at,
            updated_at=timestamp,
        )

    def _validate_metadata(
        self, metadata: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], str]:
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TaskInputError("metadata_invalid")
        value = dict(metadata or {})
        try:
            _reject_sensitive_metadata(value)
            encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise TaskInputError("metadata_invalid") from exc
        if len(encoded.encode("utf-8")) > self._max_metadata_bytes:
            raise TaskInputError("metadata_too_large")
        decoded = json.loads(encoded)
        return decoded, encoded

    class _Transaction:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __enter__(self) -> None:
            self.connection.execute("BEGIN IMMEDIATE")

        def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()

    def _transaction(self) -> TaskRepository._Transaction:
        return self._Transaction(self._connection)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _validate_source_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise TaskInputError("source_url_invalid")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise TaskInputError("source_url_invalid")
    return value


def _validate_optional_text(
    value: str | None, name: str, maximum: int, *, reject_sensitive: bool = False
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise TaskInputError(f"{name}_invalid")
    if reject_sensitive and any(marker in value.casefold() for marker in _SENSITIVE_TEXT_MARKERS):
        raise TaskInputError(f"{name}_sensitive")
    return value


def _validate_error_code(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or _ERROR_CODE.fullmatch(value) is None):
        raise TaskInputError("error_code_invalid")
    return value


def _reject_sensitive_metadata(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TaskInputError("metadata_invalid")
            normalized = key.casefold().replace("-", "_")
            if any(sensitive in normalized for sensitive in _SENSITIVE_KEYS):
                raise TaskInputError("metadata_sensitive")
            _reject_sensitive_metadata(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_sensitive_metadata(child)
    elif isinstance(value, str):
        normalized = value.casefold()
        if any(marker in normalized for marker in _SENSITIVE_TEXT_MARKERS):
            raise TaskInputError("metadata_sensitive")
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise TaskInputError("metadata_invalid") from exc
        if parsed.scheme.casefold() in {"http", "https"} and (parsed.username or parsed.password):
            raise TaskInputError("metadata_sensitive")


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=str(row["task_id"]),
        source_url=str(row["source_url"]),
        status=TaskStatus(row["status"]),
        version=int(row["version"]),
        metadata=json.loads(row["metadata_json"]),
        error_code=row["error_code"],
        resume_status=TaskStatus(row["resume_status"]) if row["resume_status"] is not None else None,
        error_message=row["error_message"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _event_from_row(row: sqlite3.Row) -> TaskEvent:
    return TaskEvent(
        event_id=int(row["event_id"]),
        task_id=str(row["task_id"]),
        from_status=TaskStatus(row["from_status"]) if row["from_status"] is not None else None,
        to_status=TaskStatus(row["to_status"]),
        task_version=int(row["task_version"]),
        reason=row["reason"],
        metadata=json.loads(row["metadata_json"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=str(row["created_at"]),
    )

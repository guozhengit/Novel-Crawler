from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from novel_crawler.task_engine.models import ALLOWED_TRANSITIONS, TaskEvent, TaskRecord, TaskStatus

SCHEMA_VERSION = 2
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE_KEYS = (
    "api_key",
    "authorization",
    "browser_token",
    "content",
    "cookie",
    "credential",
    "html",
    "page_body",
    "password",
    "passwd",
    "private_key",
    "profile_path",
    "resume_status",
    "session_id",
    "secret",
    "token",
)
_AUTHORIZATION_HEADER = re.compile(r"(?im)^\s*authorization\s*:\s*(?:bearer\s+)?\S+")
_BEARER_VALUE = re.compile(r"(?i)(?:^|\s)bearer\s+([a-z0-9._~+/=-]+)(?=$|[\s,;])")
_CREDENTIAL_LABEL = (
    r"(?:browser[ _-]*token|access[ _-]*token|api[ _-]*(?:token|key)|"
    r"refresh[ _-]*token|session[ _-]*(?:token|id)|auth[ _-]*token|"
    r"private[ _-]*key|secret[ _-]*key|password|passwd|cookie|token|secret)"
)
_CREDENTIAL_EQUALS = re.compile(rf"(?i)(?<![a-z0-9]){_CREDENTIAL_LABEL}\s*=\s*\S+")
_CREDENTIAL_COLON_VALUE = re.compile(
    rf"(?i)(?<![a-z0-9_])(?P<label>{_CREDENTIAL_LABEL})\s*:\s*=?\s*(?P<value>[^\s,;]+)"
)
_CREDENTIAL_JSON = re.compile(
    rf'''(?i)["']{_CREDENTIAL_LABEL}["']\s*:\s*["'][^"']+["']'''
)
_COOKIE_HEADER = re.compile(r"(?im)^\s*cookie\s*:\s*[^\s=;]+=[^\s;]+")
_HTML_STRUCTURE = re.compile(r"(?i)<\s*(?:html|body)(?:\s|>)")
_PROFILE_PATH_ASSIGNMENT = re.compile(r"(?i)profile(?:_|\s+)path\s*[:=]\s*\S+")
_STATUS_SQL = ", ".join(f"'{status.value}'" for status in TaskStatus)
_RESUMABLE_STATUSES = frozenset({TaskStatus.PAUSED, TaskStatus.RECOVERABLE_FAILED})
_DISPLAY_TEXT_KEYS = frozenset({"book_name", "book_title", "name", "title"})
_MAX_MIGRATION_EVENTS = 100_000


V1_ALLOWED_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = MappingProxyType({
    TaskStatus.CREATED: frozenset({TaskStatus.PROBING, TaskStatus.PAUSED, TaskStatus.CANCELLED}),
    TaskStatus.PROBING: frozenset(
        {
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.VALIDATING,
            TaskStatus.RECOVERABLE_FAILED,
            TaskStatus.PAUSED,
            TaskStatus.TERMINAL_FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_FOR_USER: frozenset(
        {
            TaskStatus.VALIDATING,
            TaskStatus.RECOVERABLE_FAILED,
            TaskStatus.TERMINAL_FAILED,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.VALIDATING: frozenset(
        {
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.READY,
            TaskStatus.RECOVERABLE_FAILED,
            TaskStatus.PAUSED,
            TaskStatus.TERMINAL_FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.READY: frozenset(
        {TaskStatus.CRAWLING, TaskStatus.PAUSED, TaskStatus.TERMINAL_FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.CRAWLING: frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.PAUSED,
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.RECOVERABLE_FAILED,
            TaskStatus.TERMINAL_FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.PAUSED: frozenset(
        {
            TaskStatus.PROBING,
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.VALIDATING,
            TaskStatus.READY,
            TaskStatus.CRAWLING,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RECOVERABLE_FAILED: frozenset(
        {
            TaskStatus.PROBING,
            TaskStatus.VALIDATING,
            TaskStatus.READY,
            TaskStatus.CRAWLING,
            TaskStatus.TERMINAL_FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.TERMINAL_FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
})


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
                self._backfill_resume_statuses()
                self._connection.execute(
                    "INSERT INTO task_schema_migrations(version, applied_at) VALUES(2, ?)", (_now(),)
                )
        columns = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(tasks)").fetchall()}
        if "resume_status" not in columns:
            raise TaskInputError("task_schema_corrupt")

    def _backfill_resume_statuses(self) -> None:
        try:
            self._backfill_resume_statuses_unchecked()
        except TaskInputError:
            raise
        except (KeyError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise TaskInputError("task_schema_migration_resume_invalid") from exc

    def _backfill_resume_statuses_unchecked(self) -> None:
        rows = self._connection.execute(
            "SELECT task_id, status, version FROM tasks WHERE status IN (?, ?) ORDER BY rowid",
            (TaskStatus.PAUSED.value, TaskStatus.RECOVERABLE_FAILED.value),
        ).fetchall()
        for task in rows:
            try:
                task_id = str(task["task_id"])
                status = TaskStatus(task["status"])
                version = int(task["version"])
                if version < 1 or version > _MAX_MIGRATION_EVENTS:
                    raise ValueError
                events = self._connection.execute(
                    """
                    SELECT from_status, to_status, task_version
                    FROM task_events WHERE task_id=? ORDER BY task_version
                    """,
                    (task_id,),
                ).fetchall()
                origin = _validated_resume_origin(events, status, version)
                updated = self._connection.execute(
                    """
                    UPDATE tasks SET resume_status=?
                    WHERE task_id=? AND status=? AND version=?
                      AND (resume_status IS NULL OR resume_status=?)
                    """,
                    (origin.value, task_id, status.value, version, origin.value),
                )
                if updated.rowcount != 1:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise TaskInputError("task_schema_migration_resume_invalid") from exc

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
            if current.status in _RESUMABLE_STATUSES and to_status not in {
                TaskStatus.TERMINAL_FAILED,
                TaskStatus.CANCELLED,
            }:
                if current.resume_status is None or to_status is not current.resume_status:
                    raise InvalidTaskTransition(
                        f"resume_not_allowed:{current.status.value}:{to_status.value}"
                    )
            if to_status in _RESUMABLE_STATUSES:
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
    if reject_sensitive and _contains_sensitive_text(value):
        raise TaskInputError(f"{name}_sensitive")
    return value


def _validate_error_code(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or _ERROR_CODE.fullmatch(value) is None):
        raise TaskInputError("error_code_invalid")
    return value


def _reject_sensitive_metadata(value: Any, *, display_text: bool = False) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TaskInputError("metadata_invalid")
            normalized = re.sub(r"[-\s]+", "_", key.casefold())
            if any(sensitive in normalized for sensitive in _SENSITIVE_KEYS):
                raise TaskInputError("metadata_sensitive")
            _reject_sensitive_metadata(child, display_text=normalized in _DISPLAY_TEXT_KEYS)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_sensitive_metadata(child, display_text=display_text)
    elif isinstance(value, str):
        if _contains_sensitive_text(value, display_text=display_text):
            raise TaskInputError("metadata_sensitive")
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise TaskInputError("metadata_invalid") from exc
        if parsed.scheme.casefold() in {"http", "https"} and (parsed.username or parsed.password):
            raise TaskInputError("metadata_sensitive")


def _contains_sensitive_text(value: str, *, display_text: bool = False) -> bool:
    if (
        _AUTHORIZATION_HEADER.search(value)
        or _CREDENTIAL_EQUALS.search(value)
        or _CREDENTIAL_JSON.search(value)
        or _COOKIE_HEADER.search(value)
        or _HTML_STRUCTURE.search(value)
        or _PROFILE_PATH_ASSIGNMENT.search(value)
    ):
        return True
    colon_matches = _CREDENTIAL_COLON_VALUE.finditer(value)
    if display_text:
        if any(_looks_like_display_credential(match.group("label"), match.group("value")) for match in colon_matches):
            return True
    elif next(colon_matches, None) is not None:
        return True
    if not display_text:
        for match in _BEARER_VALUE.finditer(value):
            if _looks_like_credential(match.group(1)):
                return True
    return False


def _looks_like_credential(value: str) -> bool:
    candidate = value.strip("\"'()[]{}")
    return len(candidate) >= 6 and (
        len(candidate) >= 16
        or any(character.isdigit() for character in candidate)
        or any(character in "._~+/=-" for character in candidate)
    )


def _looks_like_display_credential(label: str, value: str) -> bool:
    del label  # The regex already restricts this to an explicit credential label.
    candidate = value.strip("\"'()[]{}").rstrip(".,;:")
    lowered = candidate.casefold()
    if lowered.startswith(("sk-", "pk-", "ghp_")):
        return True
    if any(character.isdigit() for character in candidate):
        return True
    if candidate == lowered and candidate.isalpha():
        return True
    return len(candidate) >= 16 and not candidate.istitle()


def _validated_resume_origin(
    events: list[sqlite3.Row], task_status: TaskStatus, task_version: int
) -> TaskStatus:
    if len(events) != task_version + 1:
        raise ValueError
    previous: TaskStatus | None = None
    last_from: TaskStatus | None = None
    for expected_version, event in enumerate(events):
        if int(event["task_version"]) != expected_version:
            raise ValueError
        to_status = TaskStatus(event["to_status"])
        raw_from = event["from_status"]
        from_status = TaskStatus(raw_from) if raw_from is not None else None
        if expected_version == 0:
            if from_status is not None or to_status is not TaskStatus.CREATED:
                raise ValueError
        else:
            if from_status is not previous or from_status is None:
                raise ValueError
            if to_status not in V1_ALLOWED_TRANSITIONS[from_status]:
                raise ValueError
            last_from = from_status
        previous = to_status
    if previous is not task_status or last_from is None:
        raise ValueError
    return last_from


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

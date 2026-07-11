from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from novel_crawler.core.storage import Storage
from novel_crawler.task_engine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidTaskTransition,
    TaskInputError,
    TaskNotFound,
    TaskRepository,
    TaskStatus,
    TaskVersionConflict,
)


def test_creates_task_and_initial_event_then_persists_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    with TaskRepository(path) as repository:
        task = repository.create_task("https://example.test/book/1", metadata={"site": "example"})
        assert task.status is TaskStatus.CREATED
        assert task.version == 0
        assert task.metadata == {"site": "example"}
        assert [event.to_status for event in repository.list_events(task.task_id)] == [TaskStatus.CREATED]

    with TaskRepository(path) as reopened:
        assert reopened.get_task(task.task_id) == task


def test_migration_is_idempotent_and_non_destructive_for_old_storage(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    data = tmp_path / "data"
    with Storage(path, data) as storage:
        storage.conn.execute(
            "INSERT INTO books(site, title, author, url) VALUES(?, ?, ?, ?)",
            ("legacy", "kept", None, "https://example.test/kept"),
        )
        storage.conn.commit()

    with TaskRepository(path) as first:
        assert first.schema_version == 4
    with TaskRepository(path) as second:
        assert second.schema_version == 4
        tables = {row[0] for row in second.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"books", "chapters", "tasks", "task_events", "checkpoints", "task_schema_migrations"} <= tables
        assert second.connection.execute("SELECT title FROM books").fetchone()[0] == "kept"
        assert second.connection.execute("SELECT COUNT(*) FROM task_schema_migrations").fetchone()[0] == 4


def test_database_enables_wal_foreign_keys_and_busy_timeout(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db", busy_timeout_ms=3210) as repository:
        assert repository.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert repository.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert repository.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 3210


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            [TaskStatus.PROBING, TaskStatus.VALIDATING, TaskStatus.READY, TaskStatus.CRAWLING, TaskStatus.COMPLETED],
            TaskStatus.COMPLETED,
        ),
        ([TaskStatus.PROBING, TaskStatus.WAITING_FOR_USER, TaskStatus.VALIDATING], TaskStatus.VALIDATING),
        ([TaskStatus.PROBING, TaskStatus.RECOVERABLE_FAILED, TaskStatus.PROBING], TaskStatus.PROBING),
        ([TaskStatus.CANCELLED], TaskStatus.CANCELLED),
    ],
)
def test_allows_declared_state_transitions(tmp_path: Path, path: list[TaskStatus], expected: TaskStatus) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        for status in path:
            task = repository.transition(task.task_id, status, expected_version=task.version, reason="test")
        assert task.status is expected
        assert task.version == len(path)


def test_rejects_illegal_transition_and_terminal_state_change_without_event(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        with pytest.raises(InvalidTaskTransition):
            repository.transition(task.task_id, TaskStatus.COMPLETED, expected_version=0)
        assert repository.get_task(task.task_id).version == 0
        assert len(repository.list_events(task.task_id)) == 1

        cancelled = repository.transition(task.task_id, TaskStatus.CANCELLED, expected_version=0)
        with pytest.raises(InvalidTaskTransition):
            repository.transition(cancelled.task_id, TaskStatus.PROBING, expected_version=1)


def test_concurrent_cas_allows_exactly_one_transition(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    with TaskRepository(path) as setup:
        task = setup.create_task("https://example.test/book")
    barrier = threading.Barrier(3)
    outcomes: list[object] = []

    def change(status: TaskStatus) -> None:
        with TaskRepository(path) as repository:
            barrier.wait()
            try:
                outcomes.append(repository.transition(task.task_id, status, expected_version=0))
            except Exception as exc:
                outcomes.append(exc)

    workers = [
        threading.Thread(target=change, args=(TaskStatus.PROBING,)),
        threading.Thread(target=change, args=(TaskStatus.CANCELLED,)),
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    assert sum(not isinstance(value, Exception) for value in outcomes) == 1
    assert sum(isinstance(value, TaskVersionConflict) for value in outcomes) == 1
    with TaskRepository(path) as repository:
        assert repository.get_task(task.task_id).version == 1
        assert len(repository.list_events(task.task_id)) == 2


def test_event_insert_failure_rolls_back_task_update(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        repository.connection.execute(
            """
            CREATE TRIGGER reject_event BEFORE INSERT ON task_events
            WHEN NEW.to_status = 'probing'
            BEGIN SELECT RAISE(ABORT, 'injected'); END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            repository.transition(task.task_id, TaskStatus.PROBING, expected_version=0)
        unchanged = repository.get_task(task.task_id)
        assert unchanged.status is TaskStatus.CREATED
        assert unchanged.version == 0
        assert len(repository.list_events(task.task_id)) == 1


def test_missing_task_and_stale_versions_are_distinct(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        with pytest.raises(TaskNotFound):
            repository.get_task("a" * 32)
        with pytest.raises(TaskNotFound):
            repository.list_events("a" * 32)
        with pytest.raises(TaskNotFound):
            repository.transition("a" * 32, TaskStatus.PROBING, expected_version=0)
        task = repository.create_task("https://example.test/book")
        with pytest.raises(TaskInputError, match="status"):
            repository.transition(task.task_id, "probing", expected_version=0)  # type: ignore[arg-type]
        repository.transition(task.task_id, TaskStatus.PROBING, expected_version=0)
        with pytest.raises(TaskVersionConflict):
            repository.transition(task.task_id, TaskStatus.CANCELLED, expected_version=0)


@pytest.mark.parametrize(
    "metadata",
    [
        {"token": "private"},
        {"nested": {"cookie_value": "private"}},
        {"page_body": "chapter text"},
        {"content": "chapter text"},
        {"html": "<html>private</html>"},
        {"value": object()},
    ],
)
def test_rejects_sensitive_or_non_json_metadata(tmp_path: Path, metadata: dict[str, object]) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        with pytest.raises(TaskInputError, match="metadata"):
            repository.create_task("https://example.test/book", metadata=metadata)


def test_enforces_input_limits_and_safe_repr_and_serializers(tmp_path: Path) -> None:
    secret_url = "https://example.test/book?private=value"
    private_error = "private filesystem detail"
    with TaskRepository(tmp_path / "tasks.db", max_metadata_bytes=64, max_error_length=32) as repository:
        with pytest.raises(TaskInputError, match="source_url"):
            repository.create_task("x" * 2049)
        with pytest.raises(TaskInputError, match="metadata"):
            repository.create_task("https://example.test", metadata={"safe": "x" * 100})
        task = repository.create_task(secret_url, metadata={"safe": "ok"})
        probing = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=0)
        failed = repository.transition(
            task.task_id,
            TaskStatus.RECOVERABLE_FAILED,
            expected_version=probing.version,
            error_code="network_timeout",
            error_message=private_error,
        )
        event = repository.list_events(task.task_id)[-1]
        rendered = repr(failed) + repr(event) + json.dumps(failed.to_safe_dict()) + json.dumps(event.to_safe_dict())
        assert secret_url not in rendered
        assert private_error not in rendered
        assert "safe" not in rendered
        assert failed.error_message == private_error
        assert failed.to_safe_dict()["error_code"] == "network_timeout"

        with pytest.raises(TaskInputError, match="error_message"):
            repository.transition(
                failed.task_id,
                TaskStatus.PROBING,
                expected_version=failed.version,
                error_message="x" * 33,
            )
        with pytest.raises(TaskInputError, match="error_code"):
            repository.transition(
                failed.task_id,
                TaskStatus.PROBING,
                expected_version=failed.version,
                error_code="not a safe code",
            )


def test_database_constraints_reject_invalid_status_and_orphan_events(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        with pytest.raises(sqlite3.IntegrityError):
            repository.connection.execute(
                "INSERT INTO tasks(task_id, source_url, status, version, metadata_json, created_at, updated_at) "
                "VALUES('bad', 'https://example.test', 'unknown', 0, '{}', 'now', 'now')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            repository.connection.execute(
                "INSERT INTO task_events(task_id, from_status, to_status, task_version, created_at) "
                "VALUES('missing', NULL, 'created', 0, 'now')"
            )


def test_transition_table_covers_every_status_and_active_work_can_pause_and_resume() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(TaskStatus)
    assert TERMINAL_STATUSES == {
        TaskStatus.COMPLETED,
        TaskStatus.TERMINAL_FAILED,
        TaskStatus.CANCELLED,
    }
    assert all(not ALLOWED_TRANSITIONS[status] for status in TERMINAL_STATUSES)
    for status in (
        TaskStatus.PROBING,
        TaskStatus.WAITING_FOR_USER,
        TaskStatus.VALIDATING,
        TaskStatus.READY,
        TaskStatus.CRAWLING,
    ):
        assert TaskStatus.PAUSED in ALLOWED_TRANSITIONS[status]
    for status in (
        TaskStatus.PROBING,
        TaskStatus.WAITING_FOR_USER,
        TaskStatus.VALIDATING,
        TaskStatus.READY,
        TaskStatus.CRAWLING,
    ):
        assert status in ALLOWED_TRANSITIONS[TaskStatus.PAUSED]


@pytest.mark.parametrize(
    ("origin, blocked"),
    [
        (TaskStatus.WAITING_FOR_USER, TaskStatus.CRAWLING),
        (TaskStatus.PROBING, TaskStatus.CRAWLING),
        (TaskStatus.VALIDATING, TaskStatus.READY),
    ],
)
def test_pause_can_only_resume_its_persisted_origin(tmp_path: Path, origin: TaskStatus, blocked: TaskStatus) -> None:
    path = tmp_path / "tasks.db"
    with TaskRepository(path) as repository:
        task = repository.create_task("https://example.test/book")
        if origin is TaskStatus.PROBING:
            task = repository.transition(task.task_id, origin, expected_version=task.version)
        elif origin is TaskStatus.WAITING_FOR_USER:
            task = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=task.version)
            task = repository.transition(task.task_id, origin, expected_version=task.version)
        else:
            task = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=task.version)
            task = repository.transition(task.task_id, origin, expected_version=task.version)
        paused = repository.transition(task.task_id, TaskStatus.PAUSED, expected_version=task.version)
        assert paused.resume_status is origin

    with TaskRepository(path) as reopened:
        persisted = reopened.get_task(task.task_id)
        assert persisted.resume_status is origin
        with pytest.raises(InvalidTaskTransition):
            reopened.transition(
                task.task_id,
                blocked,
                expected_version=persisted.version,
            )
        resumed = reopened.transition(task.task_id, origin, expected_version=persisted.version)
        assert resumed.resume_status is None


def test_recoverable_failure_only_retries_failed_origin_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    with TaskRepository(path) as repository:
        task = repository.create_task("https://example.test/book")
        probing = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=0)
        failed = repository.transition(task.task_id, TaskStatus.RECOVERABLE_FAILED, expected_version=probing.version)
        assert failed.resume_status is TaskStatus.PROBING

    with TaskRepository(path) as reopened:
        failed = reopened.get_task(task.task_id)
        with pytest.raises(InvalidTaskTransition):
            reopened.transition(failed.task_id, TaskStatus.CRAWLING, expected_version=failed.version)
        retried = reopened.transition(failed.task_id, TaskStatus.PROBING, expected_version=failed.version)
        assert retried.resume_status is None


def test_waiting_for_user_recoverable_failure_can_only_return_to_waiting(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        probing = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=0)
        waiting = repository.transition(task.task_id, TaskStatus.WAITING_FOR_USER, expected_version=probing.version)
        failed = repository.transition(task.task_id, TaskStatus.RECOVERABLE_FAILED, expected_version=waiting.version)
        resumed = repository.transition(task.task_id, TaskStatus.WAITING_FOR_USER, expected_version=failed.version)
        assert resumed.resume_status is None


def test_resume_origin_is_server_owned_not_client_metadata(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        probing = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=0)
        with pytest.raises(TaskInputError, match="metadata"):
            repository.transition(
                task.task_id,
                TaskStatus.RECOVERABLE_FAILED,
                expected_version=probing.version,
                metadata={"resume_status": TaskStatus.CRAWLING.value},
            )
        failed = repository.transition(
            task.task_id,
            TaskStatus.RECOVERABLE_FAILED,
            expected_version=probing.version,
        )
        assert failed.resume_status is TaskStatus.PROBING
        with pytest.raises(InvalidTaskTransition):
            repository.transition(failed.task_id, TaskStatus.CRAWLING, expected_version=failed.version)


def test_lists_tasks_for_restart_with_status_filter_and_bounds(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        created = repository.create_task("https://example.test/created")
        probing = repository.create_task("https://example.test/probing")
        probing = repository.transition(probing.task_id, TaskStatus.PROBING, expected_version=0)
        assert [task.task_id for task in repository.list_tasks()] == [created.task_id, probing.task_id]
        assert repository.list_tasks(statuses={TaskStatus.PROBING}) == [probing]
        assert repository.list_tasks(limit=1) == [created]
        with pytest.raises(TaskInputError, match="limit"):
            repository.list_tasks(limit=0)
        assert repository.list_tasks(statuses=set()) == []
        with pytest.raises(TaskInputError, match="statuses"):
            repository.list_tasks(statuses={"probing"})  # type: ignore[arg-type]


def test_refuses_database_from_unknown_future_task_schema(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE task_schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    connection.execute("INSERT INTO task_schema_migrations VALUES(999, 'future')")
    connection.commit()
    connection.close()
    with pytest.raises(TaskInputError, match="schema"):
        TaskRepository(path)


def test_upgrades_existing_v1_task_rows_with_empty_resume_origin(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE task_schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO task_schema_migrations VALUES(1, 'v1');
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            metadata_json TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO tasks VALUES(
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'https://example.test/v1', 'created', 0, '{}',
            NULL, NULL, 'v1', 'v1'
        );
        """
    )
    connection.close()

    with TaskRepository(path) as repository:
        assert repository.schema_version == 4
        task = repository.get_task("a" * 32)
        assert task.status is TaskStatus.CREATED
        assert task.resume_status is None
        assert repository.transition(task.task_id, TaskStatus.PROBING, expected_version=0).version == 1


_V1_PATHS: dict[TaskStatus, list[TaskStatus]] = {
    TaskStatus.CREATED: [TaskStatus.CREATED],
    TaskStatus.PROBING: [TaskStatus.CREATED, TaskStatus.PROBING],
    TaskStatus.WAITING_FOR_USER: [TaskStatus.CREATED, TaskStatus.PROBING, TaskStatus.WAITING_FOR_USER],
    TaskStatus.VALIDATING: [TaskStatus.CREATED, TaskStatus.PROBING, TaskStatus.VALIDATING],
    TaskStatus.READY: [TaskStatus.CREATED, TaskStatus.PROBING, TaskStatus.VALIDATING, TaskStatus.READY],
    TaskStatus.CRAWLING: [
        TaskStatus.CREATED,
        TaskStatus.PROBING,
        TaskStatus.VALIDATING,
        TaskStatus.READY,
        TaskStatus.CRAWLING,
    ],
}


def _create_v1_resume_database(
    path: Path,
    *,
    status: str,
    origin: TaskStatus | None = None,
    broken: str | None = None,
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE task_schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO task_schema_migrations VALUES(1, 'v1');
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, source_url TEXT NOT NULL, status TEXT NOT NULL,
            version INTEGER NOT NULL, metadata_json TEXT NOT NULL, error_code TEXT,
            error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE task_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            from_status TEXT, to_status TEXT NOT NULL, task_version INTEGER NOT NULL,
            reason TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', error_code TEXT,
            error_message TEXT, created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id), UNIQUE(task_id, task_version)
        );
        """
    )
    task_id = "b" * 32
    destination = TaskStatus(status)
    origin = origin or (TaskStatus.WAITING_FOR_USER if destination is TaskStatus.PAUSED else TaskStatus.PROBING)
    statuses = [*_V1_PATHS[origin], destination]
    events = [
        (None if version == 0 else statuses[version - 1].value, current.value)
        for version, current in enumerate(statuses)
    ]
    connection.execute(
        "INSERT INTO tasks VALUES(?, ?, ?, ?, '{}', NULL, NULL, 'v1', 'v1')",
        (task_id, "https://example.test/v1-resume", status, len(events) - 1),
    )
    for version, (from_status, to_status) in enumerate(events):
        if broken == "missing" and version == len(events) - 1:
            continue
        if broken == "illegal" and version == len(events) - 1:
            from_status = "completed"
        connection.execute(
            """
            INSERT INTO task_events(
                task_id, from_status, to_status, task_version, metadata_json, created_at
            ) VALUES(?, ?, ?, ?, '{}', 'v1')
            """,
            (task_id, from_status, to_status, version),
        )
    connection.commit()
    connection.close()


@pytest.mark.parametrize(
    "origin",
    [
        TaskStatus.CREATED,
        TaskStatus.PROBING,
        TaskStatus.WAITING_FOR_USER,
        TaskStatus.VALIDATING,
        TaskStatus.READY,
        TaskStatus.CRAWLING,
    ],
)
def test_v1_migration_accepts_every_historical_paused_origin(tmp_path: Path, origin: TaskStatus) -> None:
    path = tmp_path / f"paused-{origin.value}.db"
    _create_v1_resume_database(path, status=TaskStatus.PAUSED.value, origin=origin)
    with TaskRepository(path) as repository:
        paused = repository.get_task("b" * 32)
        assert paused.resume_status is origin
        resumed = repository.transition(paused.task_id, origin, expected_version=paused.version)
        assert resumed.status is origin
        if origin is TaskStatus.CREATED:
            with pytest.raises(InvalidTaskTransition):
                repository.transition(resumed.task_id, TaskStatus.CRAWLING, expected_version=resumed.version)
            assert (
                repository.transition(resumed.task_id, TaskStatus.PROBING, expected_version=resumed.version).status
                is TaskStatus.PROBING
            )


@pytest.mark.parametrize(
    "origin",
    [
        TaskStatus.PROBING,
        TaskStatus.WAITING_FOR_USER,
        TaskStatus.VALIDATING,
        TaskStatus.CRAWLING,
    ],
)
def test_v1_migration_accepts_every_historical_recoverable_origin(tmp_path: Path, origin: TaskStatus) -> None:
    path = tmp_path / f"recoverable-{origin.value}.db"
    _create_v1_resume_database(path, status=TaskStatus.RECOVERABLE_FAILED.value, origin=origin)
    with TaskRepository(path) as repository:
        failed = repository.get_task("b" * 32)
        assert failed.resume_status is origin
        assert repository.transition(failed.task_id, origin, expected_version=failed.version).status is origin


@pytest.mark.parametrize(
    ("status, expected_origin"),
    [(TaskStatus.PAUSED, TaskStatus.WAITING_FOR_USER), (TaskStatus.RECOVERABLE_FAILED, TaskStatus.PROBING)],
)
def test_v1_migration_atomically_backfills_resume_origin_from_event_chain(
    tmp_path: Path, status: TaskStatus, expected_origin: TaskStatus
) -> None:
    path = tmp_path / f"v1-{status.value}.db"
    _create_v1_resume_database(path, status=status.value)
    with TaskRepository(path) as repository:
        task = repository.get_task("b" * 32)
        assert repository.schema_version == 4
        assert task.resume_status is expected_origin

    with TaskRepository(path) as repository:
        task = repository.get_task("b" * 32)
        assert task.resume_status is expected_origin
        with pytest.raises(InvalidTaskTransition):
            repository.transition(task.task_id, TaskStatus.CRAWLING, expected_version=task.version)
        resumed = repository.transition(task.task_id, expected_origin, expected_version=task.version)
        assert resumed.resume_status is None


@pytest.mark.parametrize("broken", ["missing", "illegal"])
def test_v1_resume_migration_bad_events_roll_back_schema_and_version(tmp_path: Path, broken: str) -> None:
    path = tmp_path / f"broken-{broken}.db"
    _create_v1_resume_database(path, status=TaskStatus.PAUSED.value, broken=broken)
    with pytest.raises(TaskInputError, match="migration_resume"):
        TaskRepository(path)

    connection = sqlite3.connect(path)
    assert connection.execute("SELECT MAX(version) FROM task_schema_migrations").fetchone()[0] == 1
    columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    assert "resume_status" not in columns
    connection.close()


def test_v1_resume_migration_multiple_tasks_one_bad_rolls_everything_back(tmp_path: Path) -> None:
    path = tmp_path / "multiple.db"
    _create_v1_resume_database(path, status=TaskStatus.PAUSED.value, origin=TaskStatus.CRAWLING)
    connection = sqlite3.connect(path)
    bad_id = "c" * 32
    connection.execute(
        "INSERT INTO tasks VALUES(?, ?, 'paused', 1, '{}', NULL, NULL, 'v1', 'v1')",
        (bad_id, "https://example.test/bad"),
    )
    connection.execute(
        """
        INSERT INTO task_events(task_id, from_status, to_status, task_version, metadata_json, created_at)
        VALUES(?, NULL, 'created', 0, '{}', 'v1')
        """,
        (bad_id,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(TaskInputError, match="migration_resume"):
        TaskRepository(path)
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT MAX(version) FROM task_schema_migrations").fetchone()[0] == 1
    assert "resume_status" not in {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    connection.close()


def test_rejects_cyclic_and_non_mapping_metadata_with_public_error(tmp_path: Path) -> None:
    cyclic: dict[str, object] = {}
    cyclic["child"] = cyclic
    with TaskRepository(tmp_path / "tasks.db") as repository:
        for metadata in (cyclic, ["not", "a", "mapping"]):
            with pytest.raises(TaskInputError, match="metadata"):
                repository.create_task("https://example.test/book", metadata=metadata)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "private_value",
    [
        "Authorization: Bearer abc123",
        "Bearer abc123",
        "cookie=session-private",
        "token=private",
        "password=hunter2",
        "secret=private",
        "<html><body>chapter正文</body></html>",
        "profile_path=C:/Users/private/browser",
        "https://username:password@example.test/private",
    ],
)
def test_rejects_sensitive_string_values_in_task_and_event_nested_metadata(tmp_path: Path, private_value: str) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        with pytest.raises(TaskInputError, match="metadata"):
            repository.create_task("https://example.test/book", metadata={"safe": [{"note": private_value}]})
        task = repository.create_task("https://example.test/book")
        with pytest.raises(TaskInputError, match="metadata"):
            repository.transition(
                task.task_id,
                TaskStatus.PROBING,
                expected_version=0,
                metadata={"safe": [[private_value]]},
            )


@pytest.mark.parametrize(
    "normal_value",
    [
        "The Secret: A Novel",
        "Bearer of the Curse",
        "Cookie: A Novel",
        "Password: A Novel",
        "The API Key: A Thriller",
        "Private Key: A Mystery",
        "Session Token: A Novel",
        "The Password: Reset",
        "Secret: Identity",
        "API Key: Genesis",
        "The Secret: Mother-in-Law",
        "Token: Counterrevolution",
        "Cookie: Chocolate-Chip",
    ],
)
def test_allows_normal_book_text_that_contains_credential_words(tmp_path: Path, normal_value: str) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book", metadata={"book_title": normal_value})
        probing = repository.transition(
            task.task_id,
            TaskStatus.PROBING,
            expected_version=0,
            metadata={"book_title": [normal_value]},
        )
        assert probing.status is TaskStatus.PROBING


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("book_title", "access_token: abc123"),
        ("title", "API Key: sk-live-example"),
        ("name", "browser token: aB3dE5"),
        ("book_title", "api key: private"),
        ("title", "ACCESS-TOKEN: opaque"),
        ("name", "api key: aBcdEfghIjkl1234"),
    ],
)
def test_display_fields_still_reject_high_confidence_colon_credentials(tmp_path: Path, field: str, value: str) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        with pytest.raises(TaskInputError, match="metadata"):
            repository.create_task("https://example.test/book", metadata={field: value})


@pytest.mark.parametrize(
    "credential",
    [
        "BROWSER_TOKEN=abc123",
        "access-token: abc123",
        "Api Token = private",
        "Refresh_Token: abc123",
        "session token=session-private",
        "AUTH TOKEN: abc123",
        "api-key=private",
        "private key: abc123",
        "SECRET_KEY=private",
        "Password=hunter2",
        "PASSWD: hunter2",
        "Cookie=session-private",
        "session_id=abc123",
        "diagnostic browser token: abc123",
        'payload {"api_key": "abc123"}',
        "access_token: abc123,",
        "diagnostic access_token: abc123",
        "refresh-token: abc123 expires=soon",
        "access_token := abc123",
        "access_token: abc123 # log",
        "access_token: abc123 (active)",
        "access_token: abc123 expires: soon",
        "password: hunter",
        "passwd: secret",
        "api_key: private",
        "access token: opaque",
        "cookie: session",
        "auth-token: trusted",
    ],
)
def test_rejects_high_confidence_credential_assignment_matrix(tmp_path: Path, credential: str) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        with pytest.raises(TaskInputError, match="metadata"):
            repository.create_task("https://example.test/book", metadata={"note": credential})


@pytest.mark.parametrize(
    "key",
    ["browser_token", "ACCESS-TOKEN", "api key", "refresh_token", "session_id", "private-key", "passwd"],
)
def test_rejects_structured_credential_keys(tmp_path: Path, key: str) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        with pytest.raises(TaskInputError, match="metadata"):
            repository.create_task("https://example.test/book", metadata={key: "value"})


@pytest.mark.parametrize(
    "assignment",
    [
        "OPENAI_API_KEY=sk-live-example",
        "MY_ACCESS_TOKEN=abc123",
        "CRAWLER_BROWSER_TOKEN=private",
        "vendor_Refresh_Token=opaque",
        "PREFIX_SESSION_ID=session-private",
    ],
)
def test_rejects_prefixed_environment_credential_assignments(tmp_path: Path, assignment: str) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        with pytest.raises(TaskInputError, match="metadata"):
            repository.create_task("https://example.test/book", metadata={"note": assignment})
        task = repository.create_task("https://example.test/book")
        with pytest.raises(TaskInputError, match="metadata"):
            repository.transition(
                task.task_id,
                TaskStatus.PROBING,
                expected_version=0,
                metadata={"note": assignment},
            )


def test_rejects_sensitive_error_details_and_boolean_version(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        for detail in ("cookie=secret", "Authorization: Bearer private", "<html>page body</html>"):
            with pytest.raises(TaskInputError, match="error_message"):
                repository.transition(
                    task.task_id,
                    TaskStatus.PROBING,
                    expected_version=0,
                    error_message=detail,
                )
            with pytest.raises(TaskInputError, match="reason"):
                repository.transition(
                    task.task_id,
                    TaskStatus.PROBING,
                    expected_version=0,
                    reason=detail,
                )
        with pytest.raises(TaskInputError, match="expected_version"):
            repository.transition(task.task_id, TaskStatus.PROBING, expected_version=False)


def test_schema_has_absolute_text_size_limits_even_for_direct_sql(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test/book")
        with pytest.raises(sqlite3.IntegrityError):
            repository.connection.execute(
                "UPDATE tasks SET metadata_json=? WHERE task_id=?", ("x" * 1_000_001, task.task_id)
            )
        with pytest.raises(sqlite3.IntegrityError):
            repository.connection.execute(
                "UPDATE tasks SET error_message=? WHERE task_id=?", ("x" * 10_001, task.task_id)
            )


def test_cleanup_gate_is_persistent_fail_closed_and_safe(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    with TaskRepository(path) as repository:
        task = repository.create_task("https://example.test")
        probing = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=task.version)
        gated = repository.require_cleanup(
            task.task_id,
            expected_version=probing.version,
            error_code="interaction_cleanup_required",
        )
        assert gated.status is TaskStatus.RECOVERABLE_FAILED
        assert gated.resume_status is TaskStatus.PROBING
        assert gated.cleanup_required is True
        assert gated.to_safe_dict()["cleanup_required"] is True
        assert "cleanup" in repr(gated)
        with pytest.raises(sqlite3.IntegrityError):
            repository.connection.execute(
                "UPDATE tasks SET resume_gate='bypass' WHERE task_id=?", (task.task_id,)
            )
        with pytest.raises(InvalidTaskTransition, match="cleanup_gate"):
            repository.transition(
                task.task_id, TaskStatus.PROBING, expected_version=gated.version
            )
    with TaskRepository(path) as restarted:
        gated = restarted.get_task(task.task_id)
        assert restarted.schema_version == 4
        assert gated.cleanup_required is True
        with pytest.raises(InvalidTaskTransition, match="cleanup_gate"):
            restarted.transition(
                task.task_id, TaskStatus.PROBING, expected_version=gated.version
            )


def test_only_cleanup_completion_or_terminal_cancel_clears_gate(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test")
        probing = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=task.version)
        gated = repository.require_cleanup(
            task.task_id,
            expected_version=probing.version,
            error_code="interaction_cleanup_required",
        )
        completed = repository.complete_cleanup_gate(
            task.task_id, expected_version=gated.version
        )
        assert completed.status is TaskStatus.PROBING
        assert completed.cleanup_required is False

        again = repository.require_cleanup(
            task.task_id,
            expected_version=completed.version,
            error_code="interaction_cleanup_required",
        )
        cancelled = repository.transition(
            task.task_id, TaskStatus.CANCELLED, expected_version=again.version
        )
        assert cancelled.cleanup_required is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"busy_timeout_ms": 0}, "busy_timeout_ms"),
        ({"max_metadata_bytes": 1}, "max_metadata_bytes"),
        ({"max_error_length": 0}, "max_error_length"),
    ],
)
def test_repository_rejects_unsafe_resource_bounds(
    tmp_path: Path, kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        TaskRepository(tmp_path / "tasks.db", **kwargs)


def test_cleanup_gate_repository_rejects_stale_missing_and_invalid_calls(tmp_path: Path) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test")
        with pytest.raises(TaskInputError, match="cleanup_error_code_required"):
            repository.require_cleanup(  # type: ignore[arg-type]
                task.task_id, expected_version=task.version, error_code=None
            )
        with pytest.raises(InvalidTaskTransition, match="cleanup_gate_not_allowed"):
            repository.require_cleanup(
                task.task_id,
                expected_version=task.version,
                error_code="interaction_cleanup_required",
            )
        with pytest.raises(TaskNotFound):
            repository.require_cleanup(
                "missing",
                expected_version=0,
                error_code="interaction_cleanup_required",
            )
        probing = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=task.version)
        with pytest.raises(TaskVersionConflict):
            repository.require_cleanup(
                task.task_id,
                expected_version=task.version,
                error_code="interaction_cleanup_required",
            )
        gated = repository.require_cleanup(
            task.task_id,
            expected_version=probing.version,
            error_code="interaction_cleanup_required",
        )
        assert (
            repository.require_cleanup(
                task.task_id,
                expected_version=gated.version,
                error_code="interaction_cleanup_required",
            )
            == gated
        )
        with pytest.raises(TaskNotFound):
            repository.complete_cleanup_gate("missing", expected_version=0)
        with pytest.raises(TaskVersionConflict):
            repository.complete_cleanup_gate(task.task_id, expected_version=probing.version)
        completed = repository.complete_cleanup_gate(task.task_id, expected_version=gated.version)
        with pytest.raises(InvalidTaskTransition, match="cleanup_gate_not_active"):
            repository.complete_cleanup_gate(task.task_id, expected_version=completed.version)


def test_interaction_lease_rejects_foreign_active_and_invalid_operations(tmp_path: Path) -> None:
    owner = "a" * 32
    now = "2030-01-01T00:00:00+00:00"
    expiry = "2030-01-01T00:10:00+00:00"
    with TaskRepository(tmp_path / "tasks.db") as repository:
        with pytest.raises(TaskInputError, match="expected_version_invalid"):
            repository.recover_lost_interaction("missing", expected_version=-1)
        with pytest.raises(TaskNotFound):
            repository.recover_lost_interaction("missing", expected_version=0)

        task = repository.create_task("https://example.test")
        probing = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=task.version)
        with pytest.raises(InvalidTaskTransition, match="interaction_recovery_not_allowed"):
            repository.recover_lost_interaction(task.task_id, expected_version=probing.version)
        waiting = repository.transition_to_waiting_with_lease(
            task.task_id,
            expected_version=probing.version,
            owner_id=owner,
            owner_epoch=1,
            expires_at=expiry,
        )
        with pytest.raises(TaskVersionConflict):
            repository.recover_lost_interaction(
                task.task_id, expected_version=probing.version, now=now
            )
        with pytest.raises(InvalidTaskTransition, match="interaction_lease_active"):
            repository.recover_lost_interaction(
                task.task_id, expected_version=waiting.version, now=now
            )

        with pytest.raises(TaskNotFound):
            repository.renew_interaction_lease(
                "missing",
                expected_version=0,
                owner_id=owner,
                owner_epoch=1,
                expires_at=expiry,
            )
        with pytest.raises(TaskVersionConflict):
            repository.renew_interaction_lease(
                task.task_id,
                expected_version=probing.version,
                owner_id=owner,
                owner_epoch=1,
                expires_at=expiry,
            )
        created = repository.create_task("https://other.test")
        assert (
            repository.renew_interaction_lease(
                created.task_id,
                expected_version=created.version,
                owner_id=owner,
                owner_epoch=1,
                expires_at=expiry,
            )
            is False
        )
        with pytest.raises(TaskInputError, match="interaction_lease_time_invalid"):
            repository.list_orphaned_waiting(now="")


@pytest.mark.parametrize(
    ("owner", "epoch", "expiry", "message"),
    [
        ("short", 1, "2030-01-01T00:10:00+00:00", "owner_invalid"),
        ("a" * 32, -1, "2030-01-01T00:10:00+00:00", "epoch_invalid"),
        ("a" * 32, 1, "bad", "time_invalid"),
        ("a" * 32, 1, "x" * 20, "time_invalid"),
        ("a" * 32, 1, "2030-01-01T00:10:00", "time_invalid"),
    ],
)
def test_interaction_lease_input_validation_is_fail_closed(
    tmp_path: Path, owner: str, epoch: int, expiry: str, message: str
) -> None:
    with TaskRepository(tmp_path / "tasks.db") as repository:
        task = repository.create_task("https://example.test")
        probing = repository.transition(task.task_id, TaskStatus.PROBING, expected_version=task.version)
        with pytest.raises(TaskInputError, match=message):
            repository.transition_to_waiting_with_lease(
                task.task_id,
                expected_version=probing.version,
                owner_id=owner,
                owner_epoch=epoch,
                expires_at=expiry,
            )

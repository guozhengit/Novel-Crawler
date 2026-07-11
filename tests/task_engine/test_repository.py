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
        assert first.schema_version == 1
    with TaskRepository(path) as second:
        assert second.schema_version == 1
        tables = {
            row[0]
            for row in second.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"books", "chapters", "tasks", "task_events", "checkpoints", "task_schema_migrations"} <= tables
        assert second.connection.execute("SELECT title FROM books").fetchone()[0] == "kept"
        assert second.connection.execute("SELECT COUNT(*) FROM task_schema_migrations").fetchone()[0] == 1


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
        task = repository.create_task("https://example.test/book")
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
        TaskStatus.CREATED,
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


def test_refuses_database_from_unknown_future_task_schema(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE task_schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO task_schema_migrations VALUES(999, 'future')")
    connection.commit()
    connection.close()
    with pytest.raises(TaskInputError, match="schema"):
        TaskRepository(path)


def test_rejects_cyclic_and_non_mapping_metadata_with_public_error(tmp_path: Path) -> None:
    cyclic: dict[str, object] = {}
    cyclic["child"] = cyclic
    with TaskRepository(tmp_path / "tasks.db") as repository:
        for metadata in (cyclic, ["not", "a", "mapping"]):
            with pytest.raises(TaskInputError, match="metadata"):
                repository.create_task("https://example.test/book", metadata=metadata)  # type: ignore[arg-type]


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

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from novel_crawler.browser.sessions import BrowserSessionError, BrowserSessionStore
from novel_crawler.core.storage import Storage
from novel_crawler.task_engine import TaskRepository

pytestmark = pytest.mark.release


def test_storage_finalizer_closes_unreleased_sqlite_connection(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "content.sqlite", tmp_path / "data")
    connection = storage.conn

    storage.__del__()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_task_repository_finalizer_closes_unreleased_sqlite_connection(tmp_path: Path) -> None:
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    connection = repository.connection

    repository.__del__()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_failed_browser_session_release_finalizer_unlocks_domain(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions", lock_timeout=0.01, max_profile_bytes=3)
    lease = store.acquire("bounded.example")
    (lease.profile_path / "oversized").write_bytes(b"1234")

    with pytest.raises(BrowserSessionError, match="release_failed"):
        lease.close()

    (lease.profile_path / "oversized").unlink()
    lease.__del__()
    with store.acquire("bounded.example"):
        pass

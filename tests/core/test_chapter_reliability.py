from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from novel_crawler.core.crawler import CrawlerService
from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.storage import ChapterContentConflict, Storage, canonical_chapter_url
from novel_crawler.runtime.env import RuntimeContext


def _book(storage: Storage) -> int:
    return storage.upsert_book(Book(title="Reliable", url="https://example.test/book", site="test"))


def test_canonical_chapter_url_is_deterministic_and_strips_fragment() -> None:
    assert canonical_chapter_url("HTTPS://BÜCHER.Example:443/a/../c?q=two%20words&a=1#part") == (
        "https://xn--bcher-kva.example/c?q=two%20words&a=1"
    )


def test_canonical_chapter_url_preserves_signed_query_order_and_duplicates() -> None:
    first = canonical_chapter_url("https://example.test/1?a=1&a=2&X-Signature=%7e%2f#fragment")
    second = canonical_chapter_url("https://example.test/1?a=2&a=1&X-Signature=%7E%2F")
    assert first == "https://example.test/1?a=1&a=2&X-Signature=~%2F"
    assert second == "https://example.test/1?a=2&a=1&X-Signature=~%2F"
    assert first != second
    assert canonical_chapter_url("https://example.test/1?flag&empty=&q=a+b&&raw=%41%2f") == (
        "https://example.test/1?flag&empty=&q=a+b&&raw=A%2F"
    )


@pytest.mark.parametrize("url", ["https://example.test/1?bad=%ZZ", "https://user:secret@example.test/1"])
def test_canonical_chapter_url_rejects_ambiguous_or_credentialed_input(url: str) -> None:
    with pytest.raises(ValueError, match="chapter_url_invalid"):
        canonical_chapter_url(url)


def test_upsert_deduplicates_by_index_and_canonical_url_without_downgrading_done(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite", tmp_path / "data") as storage:
        book_id = _book(storage)
        first = Chapter(1, "one", "https://EXAMPLE.test:443/ch/1#top")
        storage.upsert_chapters(book_id, [first])
        storage.mark_done(book_id, first, "one\n\nbody")

        storage.upsert_chapters(
            book_id,
            [Chapter(9, "renumbered", "https://example.test/ch/1"), Chapter(1, "changed", "https://example.test/other")],
        )

        chapters = storage.all_chapters(book_id)
        assert [(item.index, item.title, item.status, item.url) for item in chapters] == [
            (1, "one", "done", "https://EXAMPLE.test:443/ch/1#top")
        ]


def test_mark_done_is_idempotent_and_conflicting_content_is_rejected(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite", tmp_path / "data") as storage:
        book_id = _book(storage)
        chapter = Chapter(1, "one", "https://example.test/1")
        storage.upsert_chapters(book_id, [chapter])
        path = storage.mark_done(book_id, chapter, "body")
        assert storage.mark_done(book_id, chapter, "body") == path
        with pytest.raises(ChapterContentConflict, match="chapter_content_conflict"):
            storage.mark_done(book_id, chapter, "different")
        storage.mark_failed(book_id, 1, "late failure")
        assert storage.all_chapters(book_id)[0].status == "done"
        assert path.read_text(encoding="utf-8") == "body"


def test_mark_done_database_failure_never_records_missing_or_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with Storage(tmp_path / "db.sqlite", tmp_path / "data") as storage:
        book_id = _book(storage)
        chapter = Chapter(1, "one", "https://example.test/1")
        storage.upsert_chapters(book_id, [chapter])
        monkeypatch.setattr(storage, "_commit_done", lambda *_args: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
        with pytest.raises(sqlite3.OperationalError):
            storage.mark_done(book_id, chapter, "body")
        saved = storage.all_chapters(book_id)[0]
        assert saved.status != "done"
        assert saved.content_path is None
        files = list((tmp_path / "data").rglob("*"))
        assert not any(path.is_file() and path.suffix == ".tmp" for path in files)


def test_mark_done_replace_failure_rolls_back_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Storage(tmp_path / "db.sqlite", tmp_path / "data") as storage:
        book_id = _book(storage)
        chapter = Chapter(1, "one", "https://example.test/1")
        storage.upsert_chapters(book_id, [chapter])

        def fail_replace(_source: object, _destination: object) -> None:
            raise OSError("injected replace failure")

        monkeypatch.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected replace failure"):
            storage.mark_done(book_id, chapter, "body")
        saved = storage.all_chapters(book_id)[0]
        assert saved.status == "pending"
        assert saved.content_path is None
        assert not any(path.is_file() for path in (tmp_path / "data").rglob("*"))


def test_schema_migration_adds_integrity_columns_and_audit(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE books(id INTEGER PRIMARY KEY, site TEXT, title TEXT, author TEXT, url TEXT UNIQUE, created_at TEXT);
        INSERT INTO books VALUES(1, 'x', 'book', NULL, 'https://example.test/book', CURRENT_TIMESTAMP);
        CREATE TABLE chapters(id INTEGER PRIMARY KEY, book_id INTEGER, chapter_index INTEGER, title TEXT, url TEXT,
          status TEXT, content_path TEXT, error TEXT, updated_at TEXT);
        INSERT INTO chapters VALUES(1,1,1,'pending','https://example.test/one#x','pending',NULL,NULL,CURRENT_TIMESTAMP);
        INSERT INTO chapters VALUES(2,1,2,'done','https://EXAMPLE.test:443/one','done','kept.txt',NULL,CURRENT_TIMESTAMP);
        """
    )
    connection.commit()
    connection.close()

    with Storage(db, tmp_path / "data") as storage:
        columns = {row[1] for row in storage.conn.execute("PRAGMA table_info(chapters)")}
        assert {"canonical_url", "content_hash", "attempt_count"} <= columns
        rows = storage.conn.execute("SELECT * FROM chapters").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "done"
        assert rows[0]["chapter_index"] == 2
        assert rows[0]["content_path"] == "kept.txt"
        audit = storage.conn.execute("SELECT reason, discarded_content_hash FROM chapter_migration_audit").fetchone()
        assert audit["reason"] == "canonical_url_conflict"
        assert audit["discarded_content_hash"] is None
        indexes = {row[1] for row in storage.conn.execute("PRAGMA index_list(chapters)") if row[2]}
        assert "uq_chapters_book_canonical_url" in indexes


def test_schema_migration_sanitizes_legacy_raw_error_details(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE books(id INTEGER PRIMARY KEY, site TEXT, title TEXT, author TEXT, url TEXT UNIQUE, created_at TEXT);
        INSERT INTO books VALUES(1, 'x', 'book', NULL, 'https://example.test/book', CURRENT_TIMESTAMP);
        CREATE TABLE chapters(id INTEGER PRIMARY KEY, book_id INTEGER, chapter_index INTEGER, title TEXT, url TEXT,
          status TEXT, content_path TEXT, error TEXT, updated_at TEXT);
        INSERT INTO chapters VALUES(1,1,1,'failed','https://example.test/1','failed',NULL,
          'Authorization: Bearer private-token <html>body</html>',CURRENT_TIMESTAMP);
        """
    )
    connection.commit()
    connection.close()
    with Storage(db, tmp_path / "data") as storage:
        assert storage.conn.execute("SELECT error FROM chapters").fetchone()[0] == "chapter_download_failed"


@pytest.mark.parametrize(
    ("loser_status", "loser_has_path", "expects_path"),
    [("failed", True, False), ("pending", True, False), ("done", True, True), ("done", False, False)],
)
def test_migration_never_downgrades_done_or_attaches_untrusted_failed_content(
    tmp_path: Path, loser_status: str, loser_has_path: bool, expects_path: bool
) -> None:
    db = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE books(id INTEGER PRIMARY KEY, site TEXT, title TEXT, author TEXT, url TEXT UNIQUE, created_at TEXT);
        INSERT INTO books VALUES(1, 'x', 'book', NULL, 'https://example.test/book', CURRENT_TIMESTAMP);
        CREATE TABLE chapters(id INTEGER PRIMARY KEY, book_id INTEGER, chapter_index INTEGER, title TEXT, url TEXT,
          status TEXT, content_path TEXT, error TEXT, updated_at TEXT);
        INSERT INTO chapters VALUES(1,1,1,'winner','https://example.test/shared','done',NULL,NULL,CURRENT_TIMESTAMP);
        """
    )
    trusted = tmp_path / "trusted.txt"
    if loser_has_path:
        trusted.write_text("preserved body", encoding="utf-8")
    path = str(trusted) if loser_has_path else None
    connection.execute(
        "INSERT INTO chapters VALUES(2,1,2,'loser','https://EXAMPLE.test:443/shared',?,?,NULL,CURRENT_TIMESTAMP)",
        (loser_status, path),
    )
    connection.commit()
    connection.close()
    with Storage(db, tmp_path / "data") as storage:
        row = storage.conn.execute("SELECT status, content_path FROM chapters").fetchone()
        assert row["status"] == "done"
        assert row["content_path"] == (str(trusted) if expects_path else None)


def test_content_hash_matches_committed_bytes(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite", tmp_path / "data") as storage:
        book_id = _book(storage)
        chapter = Chapter(1, "one", "https://example.test/1")
        storage.upsert_chapters(book_id, [chapter])
        storage.mark_done(book_id, chapter, "body")
        row = storage.conn.execute("SELECT content_hash FROM chapters").fetchone()
        assert row["content_hash"] == hashlib.sha256(b"body").hexdigest()


def test_legacy_mark_failed_records_attempt_without_double_counting_claim(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite", tmp_path / "data") as storage:
        book_id = _book(storage)
        storage.upsert_chapters(book_id, [Chapter(1, "one", "https://example.test/1")])
        storage.mark_failed(book_id, 1, "legacy")
        assert storage.conn.execute("SELECT attempt_count FROM chapters").fetchone()[0] == 1
        assert storage.claim_chapter(book_id, 1, "owner", now=0, lease_seconds=10)
        storage.mark_failed(book_id, 1, "claimed")
        assert storage.conn.execute("SELECT attempt_count FROM chapters").fetchone()[0] == 2


def test_mark_failed_never_persists_credentials_html_body_or_user_paths(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite", tmp_path / "data") as storage:
        book_id = _book(storage)
        storage.upsert_chapters(book_id, [Chapter(1, "one", "https://example.test/1")])
        private = "Authorization: Bearer top-secret Cookie: sid=private password=hunter2 <html>BODY C:\\Users\\alice\\secret"
        storage.mark_failed(book_id, 1, private)
        error = storage.conn.execute("SELECT error FROM chapters").fetchone()[0]
        assert error == "chapter_download_failed"
        assert not any(value.casefold() in error.casefold() for value in ("secret", "cookie", "password", "html", "users"))
        storage.mark_failed(book_id, 1, "connection_timeout")
        assert storage.conn.execute("SELECT error FROM chapters").fetchone()[0] == "connection_timeout"
        storage.mark_failed(book_id, 1, "topsecret")
        assert storage.conn.execute("SELECT error FROM chapters").fetchone()[0] == "chapter_download_failed"


def test_two_storage_instances_cannot_race_file_and_database_commit(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    data = tmp_path / "data"
    first = Storage(db, data)
    second = Storage(db, data)
    try:
        book_id = _book(first)
        chapter = Chapter(1, "one", "https://example.test/1")
        first.upsert_chapters(book_id, [chapter])
        results: list[Path] = []
        errors: list[BaseException] = []

        def commit(storage: Storage, content: str) -> None:
            try:
                results.append(storage.mark_done(book_id, chapter, content))
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=commit, args=(first, "first")),
            threading.Thread(target=commit, args=(second, "second")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        row = first.conn.execute("SELECT content_hash, content_path FROM chapters").fetchone()
        path = Path(row["content_path"])
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["content_hash"]
        assert len(results) == 1
        assert len(errors) == 1 and isinstance(errors[0], ChapterContentConflict)
    finally:
        first.close()
        second.close()


def test_two_storage_instances_upsert_same_identity_idempotently(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    first = Storage(db, tmp_path / "data")
    second = Storage(db, tmp_path / "data")
    try:
        book_id = _book(first)
        errors: list[BaseException] = []

        def upsert(storage: Storage, chapter: Chapter) -> None:
            try:
                storage.upsert_chapters(book_id, [chapter])
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=upsert, args=(first, Chapter(1, "one", "https://example.test/1#x"))),
            threading.Thread(target=upsert, args=(second, Chapter(1, "one", "https://EXAMPLE.test:443/1"))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert len(first.all_chapters(book_id)) == 1
    finally:
        first.close()
        second.close()


def test_books_with_same_title_never_share_chapter_files(tmp_path: Path) -> None:
    with Storage(tmp_path / "db.sqlite", tmp_path / "data") as storage:
        first_id = storage.upsert_book(Book(title="same", url="https://one.example/book", site="one"))
        second_id = storage.upsert_book(Book(title="same", url="https://two.example/book", site="two"))
        first = Chapter(1, "one", "https://one.example/1")
        second = Chapter(1, "two", "https://two.example/1")
        storage.upsert_chapters(first_id, [first])
        storage.upsert_chapters(second_id, [second])
        first_path = storage.mark_done(first_id, first, "first body")
        second_path = storage.mark_done(second_id, second, "second body")
        assert first_path != second_path
        assert first_path.parent.name == str(first_id)
        assert second_path.parent.name == str(second_id)
        assert first_path.read_text(encoding="utf-8") == "first body"
        assert second_path.read_text(encoding="utf-8") == "second body"


def test_crawler_delete_removes_unique_and_safe_legacy_content_without_touching_same_title_book(tmp_path: Path) -> None:
    data = tmp_path / "data"
    storage = Storage(data / "crawler.db", data)
    first_id = storage.upsert_book(Book(title="same", url="https://one.example/book", site="same-site"))
    second_id = storage.upsert_book(Book(title="same", url="https://two.example/book", site="same-site"))
    first = Chapter(1, "one", "https://one.example/1")
    second = Chapter(1, "two", "https://two.example/1")
    storage.upsert_chapters(first_id, [first])
    storage.upsert_chapters(second_id, [second])
    first_path = storage.mark_done(first_id, first, "first")
    second_path = storage.mark_done(second_id, second, "second")
    legacy = data / "contents" / "same"
    legacy.mkdir(parents=True)
    legacy_file = legacy / "old.txt"
    legacy_file.write_text("legacy", encoding="utf-8")
    storage.conn.execute(
        "INSERT INTO chapters(book_id, chapter_index, title, url, canonical_url, status, content_path) VALUES(?,?,?,?,?,'done',?)",
        (second_id, 2, "legacy", "https://two.example/2", "https://two.example/2", str(legacy_file)),
    )
    storage.conn.commit()
    ctx = RuntimeContext("test", "3.12", tmp_path, data, data / "cache", data / "output", data / "crawler.db", [], [], {}, {})
    service = CrawlerService.__new__(CrawlerService)
    service.ctx = ctx
    service.storage = storage
    try:
        service.delete_book(first_id)
        assert not first_path.exists()
        assert second_path.read_text(encoding="utf-8") == "second"
        assert legacy.exists(), "shared legacy directory must remain while another same-title book exists"
        service.delete_book(second_id)
        assert not second_path.exists()
        assert not legacy.exists()
    finally:
        storage.close()


def test_delete_uses_book_id_directory_after_title_changes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    storage = Storage(data / "crawler.db", data)
    original = Book(title="old title", url="https://example.test/book", site="site")
    book_id = storage.upsert_book(original)
    chapter = Chapter(1, "one", "https://example.test/1")
    storage.upsert_chapters(book_id, [chapter])
    path = storage.mark_done(book_id, chapter, "body")
    storage.upsert_book(Book(title="new title", url=original.url, site=original.site))
    ctx = RuntimeContext("test", "3.12", tmp_path, data, data / "cache", data / "output", data / "crawler.db", [], [], {}, {})
    service = CrawlerService.__new__(CrawlerService)
    service.ctx = ctx
    service.storage = storage
    try:
        service.delete_book(book_id)
        assert not path.exists()
        assert not (data / "contents" / str(book_id)).exists()
    finally:
        storage.close()


def test_delete_legacy_paths_and_cache_use_safe_filename_collision_key(tmp_path: Path) -> None:
    data = tmp_path / "data"
    storage = Storage(data / "crawler.db", data)
    first_id = storage.upsert_book(Book(title="a/b", url="https://one.example/book", site="same"))
    second_id = storage.upsert_book(Book(title="a:b", url="https://two.example/book", site="same"))
    legacy = data / "contents" / "a_b"
    legacy.mkdir(parents=True)
    first_file, second_file = legacy / "first.txt", legacy / "second.txt"
    first_file.write_text("first", encoding="utf-8")
    second_file.write_text("second", encoding="utf-8")
    for book_id, index, url, path in (
        (first_id, 1, "https://one.example/1", first_file),
        (second_id, 1, "https://two.example/1", second_file),
    ):
        storage.conn.execute(
            "INSERT INTO chapters(book_id,chapter_index,title,url,canonical_url,status,content_path) VALUES(?,?,'x',?,?,'done',?)",
            (book_id, index, url, url, str(path)),
        )
    storage.conn.commit()
    cache = data / "cache" / "same" / "a_b"
    cache.mkdir(parents=True)
    (cache / "shared.html").write_text("cache", encoding="utf-8")
    ctx = RuntimeContext("test", "3.12", tmp_path, data, data / "cache", data / "output", data / "crawler.db", [], [], {}, {})
    service = CrawlerService.__new__(CrawlerService)
    service.ctx = ctx
    service.storage = storage
    try:
        service.delete_book(first_id)
        assert not first_file.exists()
        assert second_file.read_text(encoding="utf-8") == "second"
        assert cache.exists()
        service.delete_book(second_id)
        assert not second_file.exists()
        assert not legacy.exists()
        assert not cache.exists()
    finally:
        storage.close()


def test_delete_content_path_outside_root_fails_closed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    storage = Storage(data / "crawler.db", data)
    book_id = storage.upsert_book(Book(title="book", url="https://example.test/book", site="site"))
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    storage.conn.execute(
        "INSERT INTO chapters(book_id,chapter_index,title,url,canonical_url,status,content_path) VALUES(?,1,'x','https://example.test/1','https://example.test/1','done',?)",
        (book_id, str(outside)),
    )
    storage.conn.commit()
    with pytest.raises(ValueError, match="content_path_outside_root"):
        storage.delete_book_content(book_id, "book")
    assert outside.read_text(encoding="utf-8") == "private"
    assert storage.get_book(book_id).book_id == book_id
    storage.close()


def test_delete_content_symlink_fails_closed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    root = data / "contents"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    private = outside / "private.txt"
    private.write_text("private", encoding="utf-8")
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    storage = Storage(data / "crawler.db", data)
    book_id = storage.upsert_book(Book(title="book", url="https://example.test/book", site="site"))
    storage.conn.execute(
        "INSERT INTO chapters(book_id,chapter_index,title,url,canonical_url,status,content_path) VALUES(?,1,'x','https://example.test/1','https://example.test/1','done',?)",
        (book_id, str(link / "private.txt")),
    )
    storage.conn.commit()
    with pytest.raises(ValueError, match="content_path_(outside_root|reparse_point)"):
        storage.delete_book_content(book_id, "book")
    assert private.read_text(encoding="utf-8") == "private"
    storage.close()


def test_secure_content_delete_rejects_paths_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="delete_path_outside_root"):
        Storage.remove_tree_under(root, outside)
    assert marker.read_text(encoding="utf-8") == "keep"

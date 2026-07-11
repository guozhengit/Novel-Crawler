import hashlib
import logging
import os
import re
import sqlite3
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from novel_crawler.core.domains import canonical_domain
from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.url_paths import canonical_path
from novel_crawler.core.utils import ensure_dir, safe_filename

logger = logging.getLogger(__name__)
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


class ChapterContentConflict(RuntimeError):
    pass


def canonical_chapter_url(url: str) -> str:
    """Canonicalize an HTTP(S) chapter identity without changing query semantics."""
    if not isinstance(url, str) or any(ord(character) < 32 for character in url) or _BAD_PERCENT.search(url):
        raise ValueError("chapter_url_invalid")
    parts = urlsplit(url)
    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("chapter_url_invalid")
    host = canonical_domain(parts.hostname)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("chapter_url_invalid") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("chapter_url_invalid")
    default_port = 443 if scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False, encoding="utf-8", errors="strict")
    except UnicodeError:
        raise ValueError("chapter_url_invalid") from None
    if any(any(ord(character) < 32 for character in key + value) for key, value in pairs):
        raise ValueError("chapter_url_invalid")
    query = urlencode(sorted(pairs), doseq=True, quote_via=quote, safe="-._~")
    return urlunsplit((scheme, authority, canonical_path(parts.path or "/"), query, ""))


class Storage:
    def __init__(self, db_path: Path, data_dir: Path):
        self.db_path = db_path
        self.data_dir = data_dir
        ensure_dir(db_path.parent)
        ensure_dir(data_dir)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site TEXT NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT,
                    url TEXT NOT NULL UNIQUE,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS chapters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    chapter_index INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    content_path TEXT,
                    error TEXT,
                    canonical_url TEXT,
                    content_hash TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    claim_owner TEXT,
                    claim_until REAL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(book_id) REFERENCES books(id)
                );
                CREATE TABLE IF NOT EXISTS download_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER,
                    chapter_index INTEGER,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_download_logs_book ON download_logs(book_id, id);
                CREATE TABLE IF NOT EXISTS chapter_migration_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    kept_chapter_id INTEGER NOT NULL,
                    discarded_chapter_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    discarded_content_path TEXT,
                    discarded_content_hash TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            self._migrate_chapters()
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chapters_status ON chapters(book_id, status)")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_chapters_book_index ON chapters(book_id, chapter_index)"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_chapters_book_canonical_url ON chapters(book_id, canonical_url)"
            )
            self.conn.commit()

    def _migrate_chapters(self) -> None:
        columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(chapters)")}
        additions = {
            "canonical_url": "TEXT",
            "content_hash": "TEXT",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "claim_owner": "TEXT",
            "claim_until": "REAL",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE chapters ADD COLUMN {name} {declaration}")
        rows = self.conn.execute("SELECT * FROM chapters ORDER BY id").fetchall()
        for row in rows:
            try:
                canonical = canonical_chapter_url(str(row["url"]))
            except ValueError:
                canonical = f"invalid://chapter/{int(row['id'])}"
            content_hash = row["content_hash"]
            if not content_hash and row["content_path"]:
                path = Path(str(row["content_path"]))
                if path.is_file():
                    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            self.conn.execute(
                "UPDATE chapters SET canonical_url=?, content_hash=?, attempt_count=COALESCE(attempt_count, 0) WHERE id=?",
                (canonical, content_hash, row["id"]),
            )
        self._merge_duplicate_chapters("chapter_index", "chapter_index_conflict")
        self._merge_duplicate_chapters("canonical_url", "canonical_url_conflict")

    def _merge_duplicate_chapters(self, identity: str, reason: str) -> None:
        groups = self.conn.execute(
            f"SELECT book_id, {identity} AS identity FROM chapters GROUP BY book_id, {identity} HAVING COUNT(*) > 1"
        ).fetchall()
        for group in groups:
            rows = self.conn.execute(
                f"SELECT * FROM chapters WHERE book_id=? AND {identity}=? ORDER BY CASE status WHEN 'done' THEN 0 ELSE 1 END, id",
                (group["book_id"], group["identity"]),
            ).fetchall()
            winner = rows[0]
            for loser in rows[1:]:
                if not winner["content_path"] and loser["content_path"]:
                    self.conn.execute(
                        "UPDATE chapters SET status=?, content_path=?, content_hash=? WHERE id=?",
                        (loser["status"], loser["content_path"], loser["content_hash"], winner["id"]),
                    )
                    winner = self.conn.execute("SELECT * FROM chapters WHERE id=?", (winner["id"],)).fetchone()
                self.conn.execute(
                    """INSERT INTO chapter_migration_audit(
                       book_id, kept_chapter_id, discarded_chapter_id, reason,
                       discarded_content_path, discarded_content_hash) VALUES(?,?,?,?,?,?)""",
                    (group["book_id"], winner["id"], loser["id"], reason, loser["content_path"], loser["content_hash"]),
                )
                self.conn.execute("DELETE FROM chapters WHERE id=?", (loser["id"],))

    def upsert_book(self, book: Book) -> int:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO books(site,title,author,url) VALUES(?,?,?,?)",
                (book.site, book.title, book.author, book.url),
            )
            self.conn.execute(
                "UPDATE books SET site=?, title=?, author=? WHERE url=?",
                (book.site, book.title, book.author, book.url),
            )
            row = self.conn.execute("SELECT id FROM books WHERE url=?", (book.url,)).fetchone()
            self.conn.commit()
        book.book_id = int(row["id"])
        return book.book_id

    def upsert_chapters(self, book_id: int, chapters: Iterable[Chapter]) -> None:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                for chapter in chapters:
                    canonical = canonical_chapter_url(chapter.url)
                    by_index = self.conn.execute(
                        "SELECT * FROM chapters WHERE book_id=? AND chapter_index=?", (book_id, chapter.index)
                    ).fetchone()
                    by_url = self.conn.execute(
                        "SELECT * FROM chapters WHERE book_id=? AND canonical_url=?", (book_id, canonical)
                    ).fetchone()
                    if by_index is None and by_url is None:
                        self.conn.execute(
                            """INSERT INTO chapters(book_id,chapter_index,title,url,canonical_url,status)
                               VALUES(?,?,?,?,?,?)""",
                            (book_id, chapter.index, chapter.title, chapter.url, canonical, chapter.status),
                        )
                    elif by_index is not None and by_url is not None and by_index["id"] == by_url["id"]:
                        if by_index["status"] != "done":
                            self.conn.execute(
                                "UPDATE chapters SET title=?, url=?, canonical_url=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (chapter.title, chapter.url, canonical, by_index["id"]),
                            )
                    elif by_index is not None and by_url is None and by_index["status"] != "done":
                        self.conn.execute(
                            "UPDATE chapters SET title=?, url=?, canonical_url=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (chapter.title, chapter.url, canonical, by_index["id"]),
                        )
                    # A URL already bound to another index is deliberately left bound.
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def pending_chapters(self, book_id: int, start: int | None = None, end: int | None = None) -> list[Chapter]:
        with self._lock:
            where = ["book_id=?", "status!='done'"]
            params: list[object] = [book_id]
            if start is not None:
                where.append("chapter_index>=?")
                params.append(start)
            if end is not None:
                where.append("chapter_index<=?")
                params.append(end)
            rows = self.conn.execute(
                f"SELECT * FROM chapters WHERE {' AND '.join(where)} ORDER BY chapter_index",
                params,
            ).fetchall()
        return [self._row_to_chapter(row) for row in rows]

    def all_chapters(self, book_id: int) -> list[Chapter]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM chapters WHERE book_id=? ORDER BY chapter_index",
                (book_id,),
            ).fetchall()
        return [self._row_to_chapter(row) for row in rows]

    def mark_done(self, book_id: int, chapter: Chapter, content: str) -> Path:
        with self._lock:
            try:
                # The SQLite write reservation is also our cross-process file commit lock.
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT * FROM chapters WHERE book_id=? AND chapter_index=?", (book_id, chapter.index)
                ).fetchone()
                if row is None:
                    raise KeyError(f"chapter not found: {book_id}/{chapter.index}")
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if row["status"] == "done":
                    if row["content_hash"] == digest and row["content_path"] and Path(row["content_path"]).is_file():
                        self.conn.commit()
                        return Path(row["content_path"])
                    raise ChapterContentConflict("chapter_content_conflict")
                book = self.get_book(book_id)
                content_dir = ensure_dir(self.data_dir / "contents" / f"{book_id}-{safe_filename(book.title)}")
                path = content_dir / f"{chapter.index:05d}.txt"
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{chapter.index:05d}-", suffix=".tmp", dir=content_dir
                )
                temporary = Path(temporary_name)
                replaced = False
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                replaced = True
                self._fsync_directory(content_dir)
                self._commit_done(book_id, chapter, path, digest)
                return path
            except BaseException:
                self.conn.rollback()
                if "temporary" in locals():
                    temporary.unlink(missing_ok=True)
                if locals().get("replaced", False):
                    path.unlink(missing_ok=True)
                    self._fsync_directory(content_dir)
                raise

    def _commit_done(self, book_id: int, chapter: Chapter, path: Path, digest: str) -> None:
        try:
            updated = self.conn.execute(
                """UPDATE chapters SET title=?, status='done', content_path=?, content_hash=?, error=NULL,
                   claim_owner=NULL, claim_until=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE book_id=? AND chapter_index=? AND status!='done'""",
                (chapter.title, str(path), digest, book_id, chapter.index),
            )
            if updated.rowcount != 1:
                raise ChapterContentConflict("chapter_content_conflict")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def mark_failed(self, book_id: int, chapter_index: int, error: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE chapters SET status='failed', error=?,
                attempt_count=attempt_count + CASE WHEN claim_owner IS NULL THEN 1 ELSE 0 END,
                claim_owner=NULL, claim_until=NULL,
                updated_at=CURRENT_TIMESTAMP
                WHERE book_id=? AND chapter_index=? AND status!='done'
                """,
                (error[:1000], book_id, chapter_index),
            )
            self.conn.commit()

    def claim_chapter(self, book_id: int, chapter_index: int, owner: str, *, now: float, lease_seconds: float) -> bool:
        if not owner or lease_seconds <= 0:
            raise ValueError("chapter_claim_invalid")
        with self._lock:
            updated = self.conn.execute(
                """UPDATE chapters SET claim_owner=?, claim_until=?, attempt_count=attempt_count+1,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE book_id=? AND chapter_index=? AND status!='done'
                   AND (claim_owner IS NULL OR claim_owner=? OR claim_until<=?)""",
                (owner, now + lease_seconds, book_id, chapter_index, owner, now),
            )
            self.conn.commit()
            return updated.rowcount == 1

    def reset_failed(self, book_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE chapters SET status='pending', error=NULL WHERE book_id=? AND status='failed'",
                (book_id,),
            )
            self.conn.commit()

    def get_book(self, book_id: int) -> Book:
        with self._lock:
            row = self.conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if row is None:
            raise KeyError(f"book not found: {book_id}")
        return Book(title=row["title"], author=row["author"], url=row["url"], site=row["site"], book_id=row["id"])

    def find_book_by_url(self, url: str) -> Book | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM books WHERE url=?", (url,)).fetchone()
        if row is None:
            return None
        return Book(title=row["title"], author=row["author"], url=row["url"], site=row["site"], book_id=row["id"])

    def list_books(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT b.id, b.title, b.author, b.site, b.url, b.created_at,
                       COUNT(c.id) AS total,
                       SUM(CASE WHEN c.status='done' THEN 1 ELSE 0 END) AS done,
                       SUM(CASE WHEN c.status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN c.status='pending' THEN 1 ELSE 0 END) AS pending
                FROM books b
                LEFT JOIN chapters c ON c.book_id = b.id
                GROUP BY b.id
                ORDER BY b.id
                """,
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_book(self, book_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
            self.conn.execute("DELETE FROM download_logs WHERE book_id=?", (book_id,))
            self.conn.execute("DELETE FROM books WHERE id=?", (book_id,))
            self.conn.commit()

    def progress(self, book_id: int) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM chapters WHERE book_id=? GROUP BY status",
                (book_id,),
            ).fetchall()
        data = {row["status"]: int(row["c"]) for row in rows}
        data["total"] = sum(data.values())
        return data

    def add_log(self, book_id: int | None, chapter_index: int | None, level: str, message: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO download_logs(book_id, chapter_index, level, message) VALUES(?,?,?,?)",
                (book_id, chapter_index, level, message[:2000]),
            )
            self.conn.commit()

    def recent_logs(self, book_id: int | None = None, limit: int = 50) -> list[dict[str, object]]:
        with self._lock:
            if book_id is None:
                rows = self.conn.execute(
                    "SELECT * FROM download_logs ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM download_logs WHERE book_id=? ORDER BY id DESC LIMIT ?",
                    (book_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def _row_to_chapter(self, row: sqlite3.Row) -> Chapter:
        return Chapter(
            index=int(row["chapter_index"]),
            title=row["title"],
            url=row["url"],
            status=row["status"],
            content_path=Path(row["content_path"]) if row["content_path"] else None,
            error=row["error"],
        )

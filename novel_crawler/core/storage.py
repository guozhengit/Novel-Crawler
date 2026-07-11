import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import quote, urlsplit, urlunsplit

from novel_crawler.core.domains import canonical_domain
from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.url_paths import canonical_path
from novel_crawler.core.utils import ensure_dir, safe_filename

logger = logging.getLogger(__name__)
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SAFE_ERROR_CODES = frozenset(
    {
        "chapter_download_failed",
        "chapter_processor_failed",
        "connection_timeout",
        "duplicate_content",
        "empty_content",
        "parse_failed",
        "source_fetch_failed",
    }
)
_MAX_DELETION_PATHS = 10_000
_MAX_DELETION_MANIFEST_BYTES = 1_048_576


class ChapterContentConflict(RuntimeError):
    pass


class ChapterClaimConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimLease:
    book_id: int
    chapter_index: int
    owner: str
    generation: int
    expires_at: float
    token: str = field(repr=False)


@dataclass(frozen=True)
class BookDeletionResult:
    job_id: int | None
    state: Literal["completed", "pending", "blocked"]
    error_code: str | None = None

    @property
    def completed(self) -> bool:
        return self.state == "completed"

    @property
    def cleanup_required(self) -> bool:
        return self.state != "completed"

    @property
    def manual_cleanup_required(self) -> bool:
        return self.state == "blocked" and self.error_code == "deletion_manifest_migration_blocked"

    def to_safe_dict(self) -> dict[str, int | str | bool | None]:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "completed": self.completed,
            "cleanup_required": self.cleanup_required,
            "manual_cleanup_required": self.manual_cleanup_required,
            "error_code": self.error_code,
        }


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
    query = "&".join(_canonical_query_field(field) for field in parts.query.split("&")) if parts.query else ""
    return urlunsplit((scheme, authority, canonical_path(parts.path or "/"), query, ""))


def _canonical_query_field(field: str) -> str:
    key, separator, value = field.partition("=")
    canonical = _canonical_query_component(key)
    return canonical + ("=" + _canonical_query_component(value) if separator else "")


def _canonical_query_component(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            byte = int(value[index + 1 : index + 3], 16)
            decoded = chr(byte)
            output.append(decoded if decoded.isascii() and (decoded.isalnum() or decoded in "-._~") else f"%{byte:02X}")
            index += 3
        elif character == "+":
            output.append("+")
            index += 1
        else:
            output.append(quote(character, safe="-._~"))
            index += 1
    return "".join(output)


def _safe_error_code(error: object) -> str:
    return error if isinstance(error, str) and error in _SAFE_ERROR_CODES else "chapter_download_failed"


class Storage:
    def __init__(self, db_path: Path, data_dir: Path, *, clock: Callable[[], float] | None = None):
        self.db_path = db_path
        self.data_dir = data_dir
        ensure_dir(db_path.parent)
        ensure_dir(data_dir)
        self._lock = threading.RLock()
        self._clock = clock or time.time
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()
        self._retry_deletion_jobs()

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
                    claim_generation INTEGER NOT NULL DEFAULT 0,
                    claim_token TEXT,
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
                    manual_cleanup_required INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS deletion_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    manifest_json TEXT NOT NULL CHECK(length(manifest_json) <= 1048576),
                    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'blocked')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            audit_columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(chapter_migration_audit)")}
            if "manual_cleanup_required" not in audit_columns:
                self.conn.execute(
                    "ALTER TABLE chapter_migration_audit ADD COLUMN manual_cleanup_required INTEGER NOT NULL DEFAULT 0"
                )
            self._migrate_audit_paths()
            self._migrate_chapters()
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chapters_status ON chapters(book_id, status)")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_chapters_book_index ON chapters(book_id, chapter_index)"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_chapters_book_canonical_url ON chapters(book_id, canonical_url)"
            )
            deletion_columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(deletion_jobs)")}
            if "attempt_count" not in deletion_columns:
                self.conn.execute("ALTER TABLE deletion_jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
            self._migrate_deletion_job_manifests()
            self.conn.commit()

    def _migrate_audit_paths(self) -> None:
        rows = self.conn.execute(
            "SELECT id,discarded_content_path,manual_cleanup_required FROM chapter_migration_audit"
        ).fetchall()
        for row in rows:
            raw = row["discarded_content_path"]
            if not raw:
                continue
            try:
                path = Path(str(raw))
                if path.is_absolute():
                    relative = self._audit_relative_path(path)
                elif self._valid_manifest_relative_path(str(raw)):
                    relative = str(raw)
                    self._validate_content_path(self.data_dir / "contents", self._join_manifest_path(self.data_dir / "contents", relative))
                else:
                    raise ValueError("audit_path_invalid")
            except ValueError:
                self.conn.execute(
                    """UPDATE chapter_migration_audit SET discarded_content_path=NULL,
                       manual_cleanup_required=1 WHERE id=?""",
                    (row["id"],),
                )
            else:
                self.conn.execute(
                    "UPDATE chapter_migration_audit SET discarded_content_path=? WHERE id=?",
                    (relative, row["id"]),
                )

    def _audit_relative_path(self, path: Path) -> str:
        root = self.data_dir / "contents"
        safe_path = self._validate_content_path(root, path)
        return self._relative_manifest_path(root, safe_path)

    def _migrate_deletion_job_manifests(self) -> None:
        content_root = self.data_dir / "contents"
        cache_root = self.data_dir / "cache"
        rows = self.conn.execute("SELECT id, manifest_json FROM deletion_jobs").fetchall()
        for row in rows:
            try:
                value = json.loads(str(row["manifest_json"]))
                if not isinstance(value, dict) or set(value) != {
                    "cache_trees",
                    "content_files",
                    "content_trees",
                    "version",
                }:
                    raise ValueError("deletion_manifest_invalid")
                migrated: dict[str, object] = {"version": value["version"]}
                for key, root in (
                    ("cache_trees", cache_root),
                    ("content_files", content_root),
                    ("content_trees", content_root),
                ):
                    items = value[key]
                    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                        raise ValueError("deletion_manifest_invalid")
                    migrated[key] = [
                        self._relative_manifest_path(root, Path(item))
                        if Path(item).is_absolute()
                        else item
                        for item in items
                    ]
                encoded = json.dumps(migrated, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                self._decode_deletion_manifest(encoded)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, RecursionError):
                encoded = '{"cache_trees":[],"content_files":[],"content_trees":[],"version":1}'
                self.conn.execute(
                    """UPDATE deletion_jobs SET manifest_json=?, state='blocked',
                       error_code='deletion_manifest_migration_blocked', updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (encoded, row["id"]),
                )
            else:
                self.conn.execute("UPDATE deletion_jobs SET manifest_json=? WHERE id=?", (encoded, row["id"]))

    def _migrate_chapters(self) -> None:
        columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(chapters)")}
        additions = {
            "canonical_url": "TEXT",
            "content_hash": "TEXT",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "claim_owner": "TEXT",
            "claim_until": "REAL",
            "claim_generation": "INTEGER NOT NULL DEFAULT 0",
            "claim_token": "TEXT",
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
                """UPDATE chapters SET canonical_url=?, content_hash=?, attempt_count=COALESCE(attempt_count, 0),
                   error=CASE WHEN error IS NULL THEN NULL ELSE ? END WHERE id=?""",
                (canonical, content_hash, _safe_error_code(row["error"]), row["id"]),
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
                if (
                    not winner["content_path"]
                    and loser["status"] == "done"
                    and loser["content_path"]
                    and Path(str(loser["content_path"])).is_file()
                ):
                    self.conn.execute(
                        "UPDATE chapters SET content_path=?, content_hash=? WHERE id=?",
                        (loser["content_path"], loser["content_hash"], winner["id"]),
                    )
                    winner = self.conn.execute("SELECT * FROM chapters WHERE id=?", (winner["id"],)).fetchone()
                audit_path: str | None = None
                audit_manual = 0
                if loser["content_path"]:
                    try:
                        audit_path = self._audit_relative_path(Path(str(loser["content_path"])))
                    except ValueError:
                        audit_manual = 1
                self.conn.execute(
                    """INSERT INTO chapter_migration_audit(
                       book_id, kept_chapter_id, discarded_chapter_id, reason,
                       discarded_content_path, discarded_content_hash, manual_cleanup_required)
                       VALUES(?,?,?,?,?,?,?)""",
                    (group["book_id"], winner["id"], loser["id"], reason, audit_path, loser["content_hash"], audit_manual),
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

    def mark_done(
        self,
        book_id: int,
        chapter: Chapter,
        content: str,
        *,
        claim: ClaimLease | None = None,
        now: float | None = None,
    ) -> Path:
        with self._lock:
            try:
                # The SQLite write reservation is also our cross-process file commit lock.
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT * FROM chapters WHERE book_id=? AND chapter_index=?", (book_id, chapter.index)
                ).fetchone()
                if row is None:
                    raise KeyError(f"chapter not found: {book_id}/{chapter.index}")
                current = self._clock() if now is None else now
                self._validate_claim(row, claim, current, book_id, chapter.index)
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if row["status"] == "done":
                    if row["content_hash"] == digest and row["content_path"] and Path(row["content_path"]).is_file():
                        self.conn.commit()
                        return Path(row["content_path"])
                    raise ChapterContentConflict("chapter_content_conflict")
                book = self.get_book(book_id)
                content_dir = ensure_dir(self.chapter_content_dir(book_id, book.title))
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
                self._commit_done(book_id, chapter, path, digest, claim=claim, now=current)
                return path
            except BaseException:
                self.conn.rollback()
                if "temporary" in locals():
                    temporary.unlink(missing_ok=True)
                if locals().get("replaced", False):
                    path.unlink(missing_ok=True)
                    self._fsync_directory(content_dir)
                raise

    def _commit_done(
        self,
        book_id: int,
        chapter: Chapter,
        path: Path,
        digest: str,
        *,
        claim: ClaimLease | None,
        now: float,
    ) -> None:
        try:
            fence = "AND (claim_owner IS NULL OR claim_until<=?)"
            params: tuple[object, ...] = (chapter.title, str(path), digest, book_id, chapter.index, now)
            if claim is not None:
                fence = "AND claim_owner=? AND claim_generation=? AND claim_token=? AND claim_until>?"
                params = (
                    chapter.title,
                    str(path),
                    digest,
                    book_id,
                    chapter.index,
                    claim.owner,
                    claim.generation,
                    claim.token,
                    now,
                )
            updated = self.conn.execute(
                f"""UPDATE chapters SET title=?, status='done', content_path=?, content_hash=?, error=NULL,
                   claim_owner=NULL, claim_until=NULL, claim_token=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE book_id=? AND chapter_index=? AND status!='done' {fence}""",
                params,
            )
            if updated.rowcount != 1:
                raise ChapterClaimConflict("chapter_claim_conflict")
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

    def mark_failed(
        self,
        book_id: int,
        chapter_index: int,
        error: str,
        *,
        claim: ClaimLease | None = None,
        now: float | None = None,
    ) -> None:
        error_code = _safe_error_code(error)
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT * FROM chapters WHERE book_id=? AND chapter_index=?", (book_id, chapter_index)
                ).fetchone()
                if row is None:
                    self.conn.commit()
                    return
                current = self._clock() if now is None else now
                self._validate_claim(row, claim, current, book_id, chapter_index)
                fence = "AND (claim_owner IS NULL OR claim_until<=?)"
                params: tuple[object, ...] = (error_code, book_id, chapter_index, current)
                if claim is not None:
                    fence = "AND claim_owner=? AND claim_generation=? AND claim_token=? AND claim_until>?"
                    params = (error_code, book_id, chapter_index, claim.owner, claim.generation, claim.token, current)
                self.conn.execute(
                    f"""
                UPDATE chapters SET status='failed', error=?,
                attempt_count=attempt_count + CASE WHEN claim_owner IS NULL THEN 1 ELSE 0 END,
                claim_owner=NULL, claim_until=NULL, claim_token=NULL,
                updated_at=CURRENT_TIMESTAMP
                WHERE book_id=? AND chapter_index=? AND status!='done' {fence}
                    """,
                    params,
                )
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def claim_chapter(
        self,
        book_id: int,
        chapter_index: int,
        owner: str,
        *,
        now: float | None = None,
        lease_seconds: float,
    ) -> ClaimLease | None:
        if not owner or lease_seconds <= 0:
            raise ValueError("chapter_claim_invalid")
        current = self._clock() if now is None else now
        token = secrets.token_urlsafe(32)
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                updated = self.conn.execute(
                    """UPDATE chapters SET claim_owner=?, claim_until=?, claim_token=?,
                   claim_generation=claim_generation+1, attempt_count=attempt_count+1,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE book_id=? AND chapter_index=? AND status!='done'
                   AND (claim_owner IS NULL OR claim_owner=? OR claim_until<=?)""",
                    (owner, current + lease_seconds, token, book_id, chapter_index, owner, current),
                )
                if updated.rowcount != 1:
                    self.conn.commit()
                    return None
                row = self.conn.execute(
                    "SELECT claim_generation,claim_until FROM chapters WHERE book_id=? AND chapter_index=?",
                    (book_id, chapter_index),
                ).fetchone()
                lease = ClaimLease(
                    book_id, chapter_index, owner, int(row["claim_generation"]), float(row["claim_until"]), token
                )
                self.conn.commit()
                return lease
            except BaseException:
                self.conn.rollback()
                raise

    @staticmethod
    def _validate_claim(
        row: sqlite3.Row,
        claim: ClaimLease | None,
        now: float,
        book_id: int,
        chapter_index: int,
    ) -> None:
        active = row["claim_owner"] is not None and row["claim_until"] is not None and float(row["claim_until"]) > now
        if claim is None:
            if active:
                raise ChapterClaimConflict("chapter_claim_conflict")
            return
        valid = (
            active
            and claim.book_id == book_id
            and claim.chapter_index == chapter_index
            and row["claim_owner"] == claim.owner
            and int(row["claim_generation"]) == claim.generation
            and isinstance(row["claim_token"], str)
            and secrets.compare_digest(str(row["claim_token"]), claim.token)
            and claim.expires_at > now
        )
        if not valid:
            raise ChapterClaimConflict("chapter_claim_conflict")

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

    def delete_book(self, book_id: int) -> BookDeletionResult:
        job_id: int | None = None
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                book = self.conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
                if book is not None:
                    manifest = self._build_deletion_manifest_locked(book_id, book)
                    encoded = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                    if len(encoded.encode("utf-8")) > _MAX_DELETION_MANIFEST_BYTES:
                        raise ValueError("deletion_manifest_too_large")
                    self._decode_deletion_manifest(encoded)
                    cursor = self.conn.execute(
                        "INSERT INTO deletion_jobs(manifest_json, state) VALUES(?, 'pending')", (encoded,)
                    )
                    job_id = int(cursor.lastrowid)
                self.conn.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
                self.conn.execute("DELETE FROM download_logs WHERE book_id=?", (book_id,))
                self.conn.execute("DELETE FROM chapter_migration_audit WHERE book_id=?", (book_id,))
                self.conn.execute("DELETE FROM books WHERE id=?", (book_id,))
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
        if job_id is None:
            return BookDeletionResult(None, "completed")
        return self._process_deletion_job(job_id)

    def _build_deletion_manifest_locked(self, book_id: int, book: sqlite3.Row) -> dict[str, object]:
        content_root = self.data_dir / "contents"
        cache_root = self.data_dir / "cache"
        rows = self.conn.execute(
            "SELECT content_path FROM chapters WHERE book_id=? AND content_path IS NOT NULL", (book_id,)
        ).fetchall()
        audit_rows = self.conn.execute(
            """SELECT discarded_content_path,manual_cleanup_required FROM chapter_migration_audit
               WHERE book_id=?""",
            (book_id,),
        ).fetchall()
        if any(int(row["manual_cleanup_required"]) for row in audit_rows):
            raise ValueError("audit_manual_cleanup_required")
        other_rows = self.conn.execute(
            "SELECT content_path FROM chapters WHERE book_id!=? AND content_path IS NOT NULL", (book_id,)
        ).fetchall()
        other_audit_rows = self.conn.execute(
            """SELECT discarded_content_path FROM chapter_migration_audit
               WHERE book_id!=? AND discarded_content_path IS NOT NULL""",
            (book_id,),
        ).fetchall()
        other_books = self.conn.execute("SELECT title, site FROM books WHERE id!=?", (book_id,)).fetchall()
        paths = [self._validate_content_path(content_root, Path(str(row["content_path"]))) for row in rows]
        paths.extend(
            self._validate_content_path(
                content_root, self._join_manifest_path(content_root, str(row["discarded_content_path"]))
            )
            for row in audit_rows
            if row["discarded_content_path"]
        )
        shared_keys = {
            key
            for raw in [
                *(str(row["content_path"]) for row in other_rows),
                *(
                    str(self._join_manifest_path(content_root, str(row["discarded_content_path"])))
                    for row in other_audit_rows
                ),
            ]
            if (key := self._content_path_key(content_root, Path(raw))) is not None
        }
        files = sorted(
            {
                self._relative_manifest_path(content_root, path)
                for path in paths
                if self._content_path_key(content_root, path) not in shared_keys
            }
        )
        tree_candidates = {self.chapter_content_dir(book_id, str(book["title"]))}
        content_root_resolved = content_root.resolve(strict=False)
        other_title_keys = {safe_filename(str(row["title"])) for row in other_books}
        for path in paths:
            relative = path.resolve(strict=False).relative_to(content_root_resolved)
            if len(relative.parts) < 2:
                continue
            parent = content_root / relative.parts[0]
            if parent == self.chapter_content_dir(book_id, str(book["title"])) or parent.name not in other_title_keys:
                tree_candidates.add(parent)
        content_trees: list[str] = []
        for tree in sorted(tree_candidates, key=str):
            self.validate_tree_under(content_root, tree)
            if not any(self._key_is_under_tree(key, tree.resolve(strict=False)) for key in shared_keys):
                content_trees.append(self._relative_manifest_path(content_root, tree))

        cache_tree = cache_root / str(book["site"]) / safe_filename(str(book["title"]))
        cache_trees: list[str] = []
        cache_shared = any(
            str(row["site"]) == str(book["site"])
            and safe_filename(str(row["title"])) == safe_filename(str(book["title"]))
            for row in other_books
        )
        if not cache_shared:
            self.validate_tree_under(cache_root, cache_tree)
            cache_trees.append(self._relative_manifest_path(cache_root, cache_tree))
        count = len(files) + len(content_trees) + len(cache_trees)
        if count > _MAX_DELETION_PATHS:
            raise ValueError("deletion_manifest_too_many_paths")
        return {"cache_trees": cache_trees, "content_files": files, "content_trees": content_trees, "version": 1}

    def _retry_deletion_jobs(self) -> None:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM deletion_jobs WHERE state='pending' ORDER BY id LIMIT 1000"
            ).fetchall()
        for row in rows:
            self._process_deletion_job(int(row["id"]))

    def retry_deletion_job(self, job_id: int, *, force: bool = False) -> BookDeletionResult:
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
            raise ValueError("deletion_job_id_invalid")
        with self._lock:
            row = self.conn.execute(
                "SELECT state, error_code FROM deletion_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return BookDeletionResult(job_id, "completed")
            error_code = str(row["error_code"]) if row["error_code"] else None
            if row["state"] == "blocked" and (not force or error_code == "deletion_manifest_migration_blocked"):
                return BookDeletionResult(job_id, "blocked", error_code)
            if row["state"] == "blocked":
                self.conn.execute(
                    "UPDATE deletion_jobs SET state='pending', error_code=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (job_id,),
                )
                self.conn.commit()
        return self._process_deletion_job(job_id)

    def _process_deletion_job(self, job_id: int) -> BookDeletionResult:
        try:
            with self._lock:
                self.conn.execute(
                    """UPDATE deletion_jobs SET attempt_count=attempt_count+1, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND state='pending'""",
                    (job_id,),
                )
                self.conn.commit()
                row = self.conn.execute(
                    "SELECT manifest_json FROM deletion_jobs WHERE id=? AND state='pending'", (job_id,)
                ).fetchone()
            if row is None:
                return BookDeletionResult(job_id, "completed")
            manifest = self._decode_deletion_manifest(str(row["manifest_json"]))
            content_root = self.data_dir / "contents"
            cache_root = self.data_dir / "cache"
            files = [
                self._validate_content_path(content_root, self._join_manifest_path(content_root, value))
                for value in manifest["content_files"]
            ]
            content_trees = [self._join_manifest_path(content_root, value) for value in manifest["content_trees"]]
            cache_trees = [self._join_manifest_path(cache_root, value) for value in manifest["cache_trees"]]
            for tree in content_trees:
                self.validate_tree_under(content_root, tree)
            for tree in cache_trees:
                self.validate_tree_under(cache_root, tree)
            for path in files:
                path = self._validate_content_path(content_root, path)
                if path.exists():
                    path.unlink()
            for tree in content_trees:
                self.remove_tree_under(content_root, tree)
            for tree in cache_trees:
                self.remove_tree_under(cache_root, tree)
            with self._lock:
                self.conn.execute("DELETE FROM deletion_jobs WHERE id=? AND state='pending'", (job_id,))
                self.conn.commit()
            return BookDeletionResult(job_id, "completed")
        except OSError:
            with self._lock:
                self.conn.execute(
                    """UPDATE deletion_jobs SET state='pending', error_code='deletion_cleanup_retryable',
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (job_id,),
                )
                self.conn.commit()
            return BookDeletionResult(job_id, "pending", "deletion_cleanup_retryable")
        except ValueError:
            return self._block_deletion_job(job_id, "deletion_safety_blocked")
        except Exception:
            return self._block_deletion_job(job_id, "deletion_cleanup_failed")

    def _block_deletion_job(self, job_id: int, error_code: str) -> BookDeletionResult:
        with self._lock:
            self.conn.execute(
                """UPDATE deletion_jobs SET state='blocked', error_code=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (error_code, job_id),
            )
            self.conn.commit()
        return BookDeletionResult(job_id, "blocked", error_code)

    @staticmethod
    def _decode_deletion_manifest(encoded: str) -> dict[str, list[str] | int]:
        if len(encoded.encode("utf-8")) > _MAX_DELETION_MANIFEST_BYTES:
            raise ValueError("deletion_manifest_too_large")
        try:
            value = json.loads(encoded)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("deletion_manifest_invalid") from exc
        if not isinstance(value, dict) or set(value) != {"cache_trees", "content_files", "content_trees", "version"}:
            raise ValueError("deletion_manifest_invalid")
        if value["version"] != 1:
            raise ValueError("deletion_manifest_invalid")
        total = 0
        for key in ("cache_trees", "content_files", "content_trees"):
            items = value[key]
            if not isinstance(items, list) or any(
                not isinstance(item, str) or len(item) > 4096 or not Storage._valid_manifest_relative_path(item)
                for item in items
            ):
                raise ValueError("deletion_manifest_invalid")
            total += len(items)
        if total > _MAX_DELETION_PATHS:
            raise ValueError("deletion_manifest_too_many_paths")
        return value

    @staticmethod
    def _valid_manifest_relative_path(value: str) -> bool:
        if not value or "\x00" in value or "\\" in value or ":" in value or "//" in value or value.endswith("/"):
            return False
        path = PurePosixPath(value)
        return not path.is_absolute() and all(part not in {"", ".", ".."} for part in value.split("/"))

    @staticmethod
    def _join_manifest_path(root: Path, value: str) -> Path:
        if not Storage._valid_manifest_relative_path(value):
            raise ValueError("deletion_manifest_invalid")
        return root.joinpath(*PurePosixPath(value).parts)

    def _relative_manifest_path(self, root: Path, path: Path) -> str:
        root_resolved = root.resolve(strict=False)
        resolved = path.resolve(strict=False)
        try:
            relative = resolved.relative_to(root_resolved).as_posix()
        except ValueError:
            raise ValueError("deletion_manifest_path_outside_root") from None
        if not self._valid_manifest_relative_path(relative):
            raise ValueError("deletion_manifest_invalid")
        return relative

    def has_other_book(self, book_id: int, title: str, *, site: str | None = None) -> bool:
        with self._lock:
            if site is None:
                rows = self.conn.execute("SELECT title FROM books WHERE id!=?", (book_id,)).fetchall()
            else:
                rows = self.conn.execute("SELECT title FROM books WHERE id!=? AND site=?", (book_id, site)).fetchall()
        key = safe_filename(title)
        return any(safe_filename(str(row["title"])) == key for row in rows)

    def chapter_content_dir(self, book_id: int, title: str) -> Path:
        if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
            raise ValueError("book_id_invalid")
        return self.data_dir / "contents" / str(book_id)

    def delete_book_content(self, book_id: int, title: str) -> None:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self._delete_book_content_locked(book_id, title)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def _delete_book_content_locked(self, book_id: int, title: str) -> None:
        root = self.data_dir / "contents"
        rows = self.conn.execute(
            "SELECT content_path FROM chapters WHERE book_id=? AND content_path IS NOT NULL", (book_id,)
        ).fetchall()
        other_rows = self.conn.execute(
            "SELECT content_path FROM chapters WHERE book_id!=? AND content_path IS NOT NULL", (book_id,)
        ).fetchall()
        paths = [self._validate_content_path(root, Path(str(row["content_path"]))) for row in rows]
        shared_keys = {
            key
            for row in other_rows
            if (key := self._content_path_key(root, Path(str(row["content_path"])))) is not None
        }
        tree_candidates = [self.chapter_content_dir(book_id, title)]
        if not self.has_other_book(book_id, title):
            tree_candidates.append(root / safe_filename(title))
        trees: list[Path] = []
        for tree in tree_candidates:
            self.validate_tree_under(root, tree)
            tree_resolved = tree.resolve(strict=False)
            if not any(self._key_is_under_tree(key, tree_resolved) for key in shared_keys):
                trees.append(tree)
        for path in paths:
            if self._content_path_key(root, path) in shared_keys or any(
                self._path_is_under_tree(path, tree) for tree in trees
            ):
                continue
            path = self._validate_content_path(root, path)
            if path.exists():
                path.unlink()
            self._prune_empty_parents(path.parent, root)
        for tree in trees:
            self.remove_tree_under(root, tree)

    def _content_path_key(self, root: Path, path: Path) -> str | None:
        candidate = path if path.is_absolute() else self.data_dir / path
        resolved = candidate.resolve(strict=False)
        root_resolved = root.resolve(strict=False)
        if resolved == root_resolved or root_resolved not in resolved.parents:
            return None
        return os.path.normcase(str(resolved))

    @staticmethod
    def _key_is_under_tree(key: str, tree: Path) -> bool:
        tree_key = os.path.normcase(str(tree))
        try:
            return os.path.commonpath((key, tree_key)) == tree_key
        except ValueError:
            return False

    @staticmethod
    def _path_is_under_tree(path: Path, tree: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(tree.resolve(strict=False))
        except ValueError:
            return False
        return True

    def _validate_content_path(self, root: Path, path: Path) -> Path:
        candidate = path if path.is_absolute() else self.data_dir / path
        root_absolute = Path(os.path.abspath(root))
        candidate_absolute = Path(os.path.abspath(candidate))
        try:
            lexical_relative = candidate_absolute.relative_to(root_absolute)
        except ValueError:
            raise ValueError("content_path_outside_root") from None
        root_resolved = root.resolve(strict=False)
        candidate_resolved = candidate_absolute.resolve(strict=False)
        if candidate_resolved == root_resolved or root_resolved not in candidate_resolved.parents:
            raise ValueError("content_path_outside_root")
        current = root_absolute
        for part in lexical_relative.parts:
            current /= part
            if self._is_reparse_point(current):
                raise ValueError("content_path_reparse_point")
        if candidate_absolute.exists() and not candidate_absolute.is_file():
            raise ValueError("content_path_not_file")
        return candidate_absolute

    @classmethod
    def _prune_empty_parents(cls, directory: Path, root: Path) -> None:
        root_resolved = root.resolve(strict=False)
        current = directory
        while current.resolve(strict=False) != root_resolved:
            resolved = current.resolve(strict=False)
            if root_resolved not in resolved.parents or cls._is_reparse_point(current):
                raise ValueError("content_path_outside_root")
            try:
                current.rmdir()
            except (FileNotFoundError, OSError):
                return
            current = current.parent

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return False
        attributes = int(getattr(info, "st_file_attributes", 0))
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()) or bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    @staticmethod
    def remove_tree_under(root: Path, target: Path) -> None:
        Storage.validate_tree_under(root, target)
        if not target.exists():
            return
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    @staticmethod
    def validate_tree_under(root: Path, target: Path) -> None:
        root_absolute = Path(os.path.abspath(root))
        target_absolute = Path(os.path.abspath(target))
        try:
            relative = target_absolute.relative_to(root_absolute)
        except ValueError:
            raise ValueError("delete_path_outside_root") from None
        root_resolved = root.resolve(strict=False)
        target_resolved = target_absolute.resolve(strict=False)
        if target_resolved == root_resolved or root_resolved not in target_resolved.parents:
            raise ValueError("delete_path_outside_root")
        current = root_absolute
        if Storage._is_reparse_point(current):
            raise ValueError("delete_path_reparse_point")
        for part in relative.parts:
            current /= part
            if Storage._is_reparse_point(current):
                raise ValueError("delete_path_reparse_point")
        if not target_absolute.exists():
            return
        if target_absolute.is_dir():
            for descendant in target_absolute.rglob("*"):
                if Storage._is_reparse_point(descendant):
                    raise ValueError("delete_path_reparse_point")

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

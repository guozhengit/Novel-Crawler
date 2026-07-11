import logging
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.utils import ensure_dir, safe_filename

logger = logging.getLogger(__name__)


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
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(book_id, chapter_index),
                    FOREIGN KEY(book_id) REFERENCES books(id)
                );
                CREATE INDEX IF NOT EXISTS idx_chapters_status ON chapters(book_id, status);
                CREATE TABLE IF NOT EXISTS download_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER,
                    chapter_index INTEGER,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_download_logs_book ON download_logs(book_id, id);
                """
            )
            self.conn.commit()

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
            for chapter in chapters:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO chapters(book_id,chapter_index,title,url,status)
                    VALUES(?,?,?,?,?)
                    """,
                    (book_id, chapter.index, chapter.title, chapter.url, chapter.status),
                )
                self.conn.execute(
                    """
                    UPDATE chapters SET title=?, url=?, updated_at=CURRENT_TIMESTAMP
                    WHERE book_id=? AND chapter_index=? AND status!='done'
                    """,
                    (chapter.title, chapter.url, book_id, chapter.index),
                )
            self.conn.commit()

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
        book = self.get_book(book_id)
        content_dir = ensure_dir(self.data_dir / "contents" / safe_filename(book.title))
        path = content_dir / f"{chapter.index:05d}.txt"
        path.write_text(content, encoding="utf-8")
        with self._lock:
            self.conn.execute(
                """
                UPDATE chapters SET title=?, status='done', content_path=?, error=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE book_id=? AND chapter_index=?
                """,
                (chapter.title, str(path), book_id, chapter.index),
            )
            self.conn.commit()
        return path

    def mark_failed(self, book_id: int, chapter_index: int, error: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE chapters SET status='failed', error=?, updated_at=CURRENT_TIMESTAMP
                WHERE book_id=? AND chapter_index=?
                """,
                (error[:1000], book_id, chapter_index),
            )
            self.conn.commit()

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

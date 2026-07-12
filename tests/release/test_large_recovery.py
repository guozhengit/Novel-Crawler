from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from novel_crawler.core.models import Book, Chapter, ChapterStatus
from novel_crawler.core.storage import Storage
from novel_crawler.exporters.txt import TxtExporter
from novel_crawler.task_engine import TaskExecutionContext, TaskRepository, TaskStatus
from novel_crawler.task_engine.chapter_batch import ChapterBatchRunner

pytestmark = pytest.mark.release


def test_1200_chapter_restart_is_idempotent_and_file_database_consistent(tmp_path: Path) -> None:
    now = [0.0]
    data = tmp_path / "data"
    storage = Storage(data / "content.sqlite", data, clock=lambda: now[0])
    book_id = storage.upsert_book(
        Book(title="Synthetic 1200", author="fixture", url="https://fixture.test/book", site="fixture")
    )
    chapters = [
        Chapter(index, f"Chapter {index}", f"https://fixture.test/chapter/{index}")
        for index in range(1, 1201)
    ]
    storage.upsert_chapters(book_id, chapters)
    repository = TaskRepository(data / "tasks.sqlite")
    task = repository.create_task("https://fixture.test/book", metadata={"book_id": book_id})
    calls: dict[int, int] = {}
    crash_index = 437
    crash_once = True

    def process(chapter: Chapter) -> str:
        nonlocal crash_once
        calls[chapter.index] = calls.get(chapter.index, 0) + 1
        if chapter.index == crash_index and crash_once:
            crash_once = False
            raise KeyboardInterrupt("synthetic process crash")
        return f"Chapter {chapter.index}\n\nfixture body {chapter.index}"

    runner = ChapterBatchRunner(storage, process, batch_size=37, claim_lease_seconds=1)
    context = TaskExecutionContext(repository, task.task_id, task.version)
    with pytest.raises(KeyboardInterrupt, match="synthetic process crash"):
        runner(context, task)
    assert storage.progress(book_id) == {"total": 1200, "done": 436, "pending": 764}
    before = repository.load_checkpoint(task.task_id, "chapter-progress")
    assert before.payload == {"failed": 0, "next_index": 408, "succeeded": 407}

    # Model a process that died after claiming the in-flight chapter.  The
    # restarted process must wait for/observe lease expiry instead of stealing it.
    stale = storage.claim_chapter(book_id, crash_index, "dead-worker", now=now[0], lease_seconds=1)
    assert stale is not None
    repository.close()
    storage.close()

    now[0] = 2.0
    reopened_storage = Storage(data / "content.sqlite", data, clock=lambda: now[0])
    reopened_repository = TaskRepository(data / "tasks.sqlite")
    resumed = reopened_repository.get_task(task.task_id)
    resumed_context = TaskExecutionContext(reopened_repository, resumed.task_id, resumed.version)
    resumed_runner = ChapterBatchRunner(
        reopened_storage,
        process,
        batch_size=37,
        claim_lease_seconds=1,
    )

    assert resumed_runner(resumed_context, resumed) is TaskStatus.COMPLETED
    assert reopened_storage.progress(book_id) == {"total": 1200, "done": 1200}
    assert calls == {index: (2 if index == crash_index else 1) for index in range(1, 1201)}
    checkpoint = reopened_repository.load_checkpoint(task.task_id, "chapter-progress")
    assert checkpoint.payload == {"failed": 0, "next_index": 1201, "succeeded": 1200}

    rows = reopened_storage.conn.execute(
        "SELECT chapter_index, canonical_url, status, content_path FROM chapters WHERE book_id=? ORDER BY chapter_index",
        (book_id,),
    ).fetchall()
    assert len(rows) == 1200
    assert len({row["chapter_index"] for row in rows}) == 1200
    assert len({row["canonical_url"] for row in rows}) == 1200
    assert {row["status"] for row in rows} == {ChapterStatus.DONE}
    for row in rows:
        content_path = Path(row["content_path"])
        assert content_path.is_file()
        assert content_path.read_text(encoding="utf-8") == (
            f"Chapter {row['chapter_index']}\n\nfixture body {row['chapter_index']}"
        )

    exporter = TxtExporter(data / "output")
    output = exporter.export(reopened_storage, book_id)
    first_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    assert exporter.export(reopened_storage, book_id) == output
    assert hashlib.sha256(output.read_bytes()).hexdigest() == first_hash
    text = output.read_text(encoding="utf-8")
    assert text.count("fixture body ") == 1200

    reopened_repository.close()
    reopened_storage.close()

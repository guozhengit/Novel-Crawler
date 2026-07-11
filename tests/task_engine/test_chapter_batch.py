from __future__ import annotations

import threading
from pathlib import Path

import pytest

from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.storage import Storage
from novel_crawler.task_engine import (
    RecoverableTaskError,
    TaskControlRequested,
    TaskExecutionContext,
    TaskRepository,
    TaskStatus,
)
from novel_crawler.task_engine.chapter_batch import ChapterBatchRunner


def _setup(tmp_path: Path, count: int = 100):
    storage = Storage(tmp_path / "content.sqlite", tmp_path / "data")
    book_id = storage.upsert_book(Book(title="batch", url="https://example.test/book", site="test"))
    storage.upsert_chapters(
        book_id,
        [Chapter(index, f"chapter {index}", f"https://example.test/{index}") for index in range(1, count + 1)],
    )
    repository = TaskRepository(tmp_path / "tasks.sqlite")
    task = repository.create_task("https://example.test/book", metadata={"book_id": book_id})
    context = TaskExecutionContext(repository, task.task_id, task.version, control_poll_interval=0.001)
    return storage, repository, task, context


def test_batch_runner_recovers_100_chapters_after_worker_crash_without_redownload(tmp_path: Path) -> None:
    storage, repository, task, context = _setup(tmp_path)
    calls: dict[int, int] = {}
    crash_once = True

    def download(chapter: Chapter) -> str:
        nonlocal crash_once
        calls[chapter.index] = calls.get(chapter.index, 0) + 1
        if chapter.index == 47 and crash_once:
            crash_once = False
            raise KeyboardInterrupt("worker crashed")
        return f"{chapter.title}\n\nbody"

    runner = ChapterBatchRunner(storage, download, batch_size=10)
    with pytest.raises(KeyboardInterrupt, match="worker crashed"):
        runner(context, task)
    assert storage.progress(task.metadata["book_id"])["done"] == 46

    restarted = TaskRepository(tmp_path / "tasks.sqlite")
    try:
        resumed_task = restarted.get_task(task.task_id)
        resumed_context = TaskExecutionContext(restarted, task.task_id, resumed_task.version, control_poll_interval=0.001)
        assert runner(resumed_context, resumed_task) is TaskStatus.COMPLETED
        assert storage.progress(task.metadata["book_id"])["done"] == 100
        # The in-flight request can only be retried after a hard process crash;
        # every durably completed chapter is downloaded exactly once.
        assert calls == {index: (2 if index == 47 else 1) for index in range(1, 101)}
        checkpoint = restarted.load_checkpoint(task.task_id, "chapter-progress")
        assert checkpoint.payload == {"failed": 0, "next_index": 101, "succeeded": 100}
    finally:
        restarted.close()
        repository.close()
        storage.close()


def test_checkpoint_failure_after_file_commit_is_recoverable_and_skips_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage, repository, task, context = _setup(tmp_path, count=2)
    calls: list[int] = []
    runner = ChapterBatchRunner(storage, lambda chapter: calls.append(chapter.index) or "body", batch_size=1)
    original = context.checkpoint
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("checkpoint unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(context, "checkpoint", fail_once)
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        runner(context, task)
    assert storage.progress(task.metadata["book_id"])["done"] == 1
    assert runner(TaskExecutionContext(repository, task.task_id, task.version), task) is TaskStatus.COMPLETED
    assert calls == [1, 2]
    repository.close()
    storage.close()


@pytest.mark.parametrize(
    "signal",
    [TaskControlRequested("late_interaction"), RecoverableTaskError("verification_required")],
)
def test_runtime_control_signals_are_never_marked_as_chapter_failures(tmp_path: Path, signal: Exception) -> None:
    storage, repository, task, context = _setup(tmp_path, count=1)
    runner = ChapterBatchRunner(storage, lambda _chapter: (_ for _ in ()).throw(signal))
    with pytest.raises(type(signal)):
        runner(context, task)
    assert storage.progress(task.metadata["book_id"]) == {"total": 1, "pending": 1}
    repository.close()
    storage.close()


def test_concurrent_runners_claim_each_chapter_once(tmp_path: Path) -> None:
    storage, first_repository, first_task, first_context = _setup(tmp_path, count=20)
    second_repository = TaskRepository(tmp_path / "tasks-2.sqlite")
    second_task = second_repository.create_task("https://example.test/book", metadata={"book_id": first_task.metadata["book_id"]})
    second_context = TaskExecutionContext(second_repository, second_task.task_id, second_task.version)
    calls: dict[int, int] = {}
    lock = threading.Lock()

    def download(chapter: Chapter) -> str:
        with lock:
            calls[chapter.index] = calls.get(chapter.index, 0) + 1
        return "body"

    runner = ChapterBatchRunner(storage, download, batch_size=2)
    threads = [
        threading.Thread(target=runner, args=(first_context, first_task)),
        threading.Thread(target=runner, args=(second_context, second_task)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == {index: 1 for index in range(1, 21)}
    assert storage.progress(first_task.metadata["book_id"])["done"] == 20
    first_repository.close()
    second_repository.close()
    storage.close()


def test_processor_failure_is_counted_and_done_is_never_reprocessed(tmp_path: Path) -> None:
    storage, repository, task, context = _setup(tmp_path, count=3)

    def download(chapter: Chapter) -> str:
        if chapter.index == 2:
            raise ValueError("bad chapter")
        return "body"

    assert ChapterBatchRunner(storage, download, batch_size=3)(context, task) is TaskStatus.RECOVERABLE_FAILED
    checkpoint = repository.load_checkpoint(task.task_id, "chapter-progress")
    assert checkpoint.payload == {"failed": 1, "next_index": 2, "succeeded": 2}
    assert storage.progress(task.metadata["book_id"])["done"] == 2
    assert storage.progress(task.metadata["book_id"])["failed"] == 1
    repository.close()
    storage.close()


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"batch_size": 0}, ValueError),
        ({"batch_size": True}, ValueError),
        ({"claim_lease_seconds": 0.5}, ValueError),
    ],
)
def test_runner_rejects_invalid_limits(tmp_path: Path, kwargs: dict[str, object], error: type[Exception]) -> None:
    storage, repository, _task, _context = _setup(tmp_path, count=0)
    with pytest.raises(error):
        ChapterBatchRunner(storage, lambda _chapter: "body", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="processor must be callable"):
        ChapterBatchRunner(storage, None)  # type: ignore[arg-type]
    repository.close()
    storage.close()


def test_runner_validates_task_and_checkpoint_payload(tmp_path: Path) -> None:
    storage, repository, task, context = _setup(tmp_path, count=0)
    bad_task = repository.create_task("https://example.test/bad", metadata={})
    with pytest.raises(ValueError, match="task_book_id_invalid"):
        ChapterBatchRunner(storage, lambda _chapter: "body")(
            TaskExecutionContext(repository, bad_task.task_id, bad_task.version), bad_task
        )
    repository.save_checkpoint(
        task.task_id,
        "chapter-progress",
        {"failed": 0, "next_index": 0, "succeeded": 0},
        expected_version=None,
    )
    with pytest.raises(ValueError, match="checkpoint_next_index_invalid"):
        ChapterBatchRunner(storage, lambda _chapter: "body")(context, task)
    repository.close()
    storage.close()


def test_empty_book_and_non_string_processor_result_are_handled(tmp_path: Path) -> None:
    storage, repository, task, context = _setup(tmp_path, count=0)
    assert ChapterBatchRunner(storage, lambda _chapter: "body")(context, task) is TaskStatus.COMPLETED
    repository.close()
    storage.close()

    storage, repository, task, context = _setup(tmp_path / "other", count=1)
    assert ChapterBatchRunner(storage, lambda _chapter: None)(context, task) is TaskStatus.RECOVERABLE_FAILED  # type: ignore[arg-type]
    repository.close()
    storage.close()

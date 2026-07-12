from __future__ import annotations

from collections.abc import Callable

from novel_crawler.core.models import Chapter, ChapterStatus
from novel_crawler.core.storage import ChapterClaimConflict, Storage
from novel_crawler.task_engine.executor import TaskControlRequested, TaskExecutionContext, TerminalTaskError
from novel_crawler.task_engine.models import TaskRecord, TaskStatus
from novel_crawler.task_engine.repository import CheckpointNotFound

ChapterProcessor = Callable[[Chapter], str]


class ChapterBatchRunner:
    """Reusable resumable chapter handler for ``BackgroundTaskExecutor``."""

    def __init__(
        self,
        storage: Storage,
        processor: ChapterProcessor,
        *,
        batch_size: int = 20,
        checkpoint_key: str = "chapter-progress",
        claim_lease_seconds: float = 300.0,
        chapter_start: int = 1,
        chapter_end: int | None = None,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        if not callable(processor):
            raise TypeError("processor must be callable")
        if not 1 <= claim_lease_seconds <= 3600:
            raise ValueError("claim_lease_seconds must be between 1 and 3600")
        if (
            isinstance(chapter_start, bool)
            or not isinstance(chapter_start, int)
            or not 1 <= chapter_start <= 10_000_000
            or chapter_end is not None
            and (
                isinstance(chapter_end, bool)
                or not isinstance(chapter_end, int)
                or not chapter_start <= chapter_end <= 10_000_000
            )
        ):
            raise ValueError("chapter_range_invalid")
        self._storage = storage
        self._processor = processor
        self._batch_size = batch_size
        self._checkpoint_key = checkpoint_key
        self._claim_lease_seconds = claim_lease_seconds
        self._chapter_start = chapter_start
        self._chapter_end = chapter_end

    def __call__(self, context: TaskExecutionContext, task: TaskRecord) -> TaskStatus:
        book_id = task.metadata.get("book_id")
        if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
            raise ValueError("task_book_id_invalid")
        try:
            prior = context.repository.load_checkpoint(task.task_id, self._checkpoint_key)
        except CheckpointNotFound:
            checkpoint_version: int | None = None
            next_index = self._chapter_start
            succeeded = 0
            failed = 0
        else:
            checkpoint_version = prior.version
            next_index = self._positive_int(prior.payload.get("next_index"), "checkpoint_next_index_invalid")
            if next_index < self._chapter_start or (
                self._chapter_end is not None and next_index > self._chapter_end + 1
            ):
                raise ValueError("checkpoint_next_index_invalid")
            succeeded = self._nonnegative_int(prior.payload.get("succeeded"), "checkpoint_succeeded_invalid")
            failed = self._nonnegative_int(prior.payload.get("failed"), "checkpoint_failed_invalid")

        pending_batch = 0
        final_next = next_index
        retry_indices: list[int] = []
        chapters = (
            []
            if self._chapter_end is not None and next_index > self._chapter_end
            else self._storage.all_chapters(book_id, start=next_index, end=self._chapter_end)
        )
        for chapter in chapters:
            context.check_control(force=True)
            final_next = max(final_next, chapter.index + 1)
            if chapter.status == ChapterStatus.DONE:
                pending_batch += 1
            elif lease := self._storage.claim_chapter(
                book_id,
                chapter.index,
                task.task_id,
                lease_seconds=self._claim_lease_seconds,
            ):
                try:
                    content = self._processor(chapter)
                    if not isinstance(content, str):
                        raise TypeError("chapter_processor_content_invalid")
                    self._storage.mark_done(book_id, chapter, content, claim=lease)
                except (TaskControlRequested, TerminalTaskError):
                    self._storage.release_chapter_claim(book_id, chapter.index, claim=lease)
                    raise
                except Exception:
                    try:
                        self._storage.mark_failed(
                            book_id, chapter.index, "chapter_processor_failed", claim=lease
                        )
                    except ChapterClaimConflict:
                        pass
                    retry_indices.append(chapter.index)
                except BaseException:
                    self._storage.release_chapter_claim(book_id, chapter.index, claim=lease)
                    raise
                pending_batch += 1
            else:
                retry_indices.append(chapter.index)
            if pending_batch >= self._batch_size:
                succeeded, failed = self._counts(book_id)
                checkpoint_version = self._save(
                    context, min(retry_indices, default=final_next), succeeded, failed, checkpoint_version
                )
                pending_batch = 0
        if pending_batch or checkpoint_version is None:
            succeeded, failed = self._counts(book_id)
            self._save(
                context,
                min(retry_indices, default=final_next),
                succeeded,
                failed,
                checkpoint_version,
            )
        return TaskStatus.RECOVERABLE_FAILED if failed or retry_indices else TaskStatus.COMPLETED

    def _counts(self, book_id: int) -> tuple[int, int]:
        progress = self._storage.progress(book_id, start=self._chapter_start, end=self._chapter_end)
        return progress.get(ChapterStatus.DONE, 0), progress.get(ChapterStatus.FAILED, 0)

    def _save(
        self,
        context: TaskExecutionContext,
        next_index: int,
        succeeded: int,
        failed: int,
        expected_version: int | None,
    ) -> int:
        saved = context.checkpoint(
            self._checkpoint_key,
            {"failed": failed, "next_index": next_index, "succeeded": succeeded},
            expected_version=expected_version,
        )
        return saved.version

    @staticmethod
    def _positive_int(value: object, error: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(error)
        return value

    @staticmethod
    def _nonnegative_int(value: object, error: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(error)
        return value

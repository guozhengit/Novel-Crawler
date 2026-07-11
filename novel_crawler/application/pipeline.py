"""Production task handlers that bridge validated configs to durable crawling."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Protocol

from novel_crawler.acquisition.http import AcquisitionError
from novel_crawler.acquisition.models import PageSnapshot
from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.application.models import CrawlOptions
from novel_crawler.application.site_adapter import SiteConfigAdapter
from novel_crawler.browser import BrowserCleanupRequired, VerificationRequired
from novel_crawler.core.models import Chapter
from novel_crawler.core.storage import Storage
from novel_crawler.task_engine.chapter_batch import ChapterBatchRunner
from novel_crawler.task_engine.executor import RecoverableTaskError, TaskControlRequested, TaskExecutionContext
from novel_crawler.task_engine.models import TaskRecord, TaskStatus
from novel_crawler.task_engine.repository import CheckpointNotFound, TaskRepository


class _Registry(Protocol):
    def load_active(self, url: str) -> SiteConfig | None: ...


class _Acquirer(Protocol):
    def fetch(
        self,
        url: str,
        *,
        task_key: str | None = None,
        timeout: float | None = None,
    ) -> PageSnapshot: ...


class CrawlTaskPipeline:
    """VALIDATING and CRAWLING handlers with only bounded durable state."""

    def __init__(
        self,
        repository: TaskRepository,
        storage: Storage,
        registry: _Registry,
        acquirer: _Acquirer,
        *,
        exporter: Callable[[int, str], object] | None = None,
        legacy_adapter: Callable[[str], SiteConfigAdapter] | None = None,
        interaction_handler: Callable[
            [TaskRecord, str, BrowserCleanupRequired | VerificationRequired], object
        ]
        | None = None,
        access_preparer: Callable[[str, str], object] | None = None,
        batch_size: int = 20,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._registry = registry
        self._acquirer = acquirer
        self._exporter = exporter
        self._legacy_adapter = legacy_adapter
        self._interaction_handler = interaction_handler
        self._access_preparer = access_preparer
        self._batch_size = batch_size
        self._monotonic = monotonic
        self._sleep = sleep
        self._rate_lock = threading.Lock()
        self._last_request_at: float | None = None

    @property
    def handlers(self) -> Mapping[TaskStatus, Callable[[TaskExecutionContext, TaskRecord], TaskStatus]]:
        return {TaskStatus.VALIDATING: self.validating, TaskStatus.CRAWLING: self.crawling}

    def validating(self, context: TaskExecutionContext, task: TaskRecord) -> TaskStatus:
        options = self._options(task)
        config = self._registry.load_active(task.source_url)
        if config is None:
            if self._legacy_adapter is None:
                return TaskStatus.PROBING
            adapter = self._legacy_adapter(task.source_url)
        else:
            adapter = SiteConfigAdapter(config)
        if not adapter.match(task.source_url):
            raise ValueError("active_config_mismatch")
        context.check_control(force=True)
        html = self._fetch_html(task.source_url, task, adapter)
        book = adapter.get_book_info(html, task.source_url)
        chapters = adapter.get_chapter_list(html, task.source_url, start=options.start, count=options.count)
        if options.max_chapters is not None:
            chapters = chapters[: options.max_chapters]
        if not chapters:
            raise ValueError("chapter_list_empty")
        book_id = self._storage.upsert_book(book)
        self._storage.upsert_chapters(book_id, chapters)
        payload: dict[str, object] = {
            "book_id": book_id,
            "export": options.export,
            "export_format": options.export_format,
        }
        try:
            previous = self._repository.load_checkpoint(task.task_id, "crawl-plan")
        except CheckpointNotFound:
            expected = None
        else:
            expected = previous.version
        context.checkpoint("crawl-plan", payload, expected_version=expected)
        return TaskStatus.READY

    def crawling(self, context: TaskExecutionContext, task: TaskRecord) -> TaskStatus:
        plan = self._repository.load_checkpoint(task.task_id, "crawl-plan").payload
        book_id = plan.get("book_id")
        if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id < 1:
            raise ValueError("crawl_plan_invalid")
        config = self._registry.load_active(task.source_url)
        if config is None:
            raise ValueError("active_config_missing")
        adapter = SiteConfigAdapter(config)

        def process(chapter: Chapter) -> str:
            context.check_control(force=True)
            html = self._fetch_html(chapter.url, task, adapter)
            title, body = adapter.parse_chapter(html, chapter.url)
            if title:
                chapter.title = title
            return f"{chapter.title}\n\n{body}"

        runner = ChapterBatchRunner(self._storage, process, batch_size=self._batch_size)
        ephemeral = replace(task, metadata={**task.metadata, "book_id": book_id})
        outcome = runner(context, ephemeral)
        if outcome is not TaskStatus.COMPLETED:
            return outcome
        should_export = plan.get("export") is True
        fmt = plan.get("export_format")
        if should_export:
            if not isinstance(fmt, str) or fmt not in {"txt", "epub", "md", "jsonl"}:
                raise ValueError("crawl_plan_invalid")
            if self._exporter is None:
                raise ValueError("exporter_unavailable")
            self._exporter(book_id, fmt)
        return TaskStatus.COMPLETED

    @staticmethod
    def _options(task: TaskRecord) -> CrawlOptions:
        raw = task.metadata.get("crawl")
        if not isinstance(raw, dict):
            raise ValueError("crawl_options_missing")
        options = CrawlOptions.parse(raw)
        if options.chase:
            raise ValueError("chase_unsupported")
        if options.concurrency != 1:
            raise ValueError("concurrency_unsupported")
        return options

    def _fetch_html(self, url: str, task: TaskRecord, adapter: SiteConfigAdapter) -> str:
        options = adapter.fetch_options
        for attempt in range(options.retries):
            self._wait_rate_limit(task, options.delay_min)
            if self._access_preparer is not None:
                self._access_preparer(url, task.task_id)
            try:
                return self._acquirer.fetch(
                    url, task_key=task.task_id, timeout=options.timeout
                ).html
            except BrowserCleanupRequired as signal:
                if self._interaction_handler is not None:
                    self._interaction_handler(task, url, signal)
                    raise TaskControlRequested("late_browser_interaction") from None
                raise RecoverableTaskError("browser_cleanup_required") from None
            except VerificationRequired as signal:
                if self._interaction_handler is not None:
                    self._interaction_handler(task, url, signal)
                    raise TaskControlRequested("late_browser_interaction") from None
                raise RecoverableTaskError("verification_required") from None
            except AcquisitionError as exc:
                if not exc.recoverable or attempt + 1 >= options.retries:
                    raise
        raise RecoverableTaskError("source_fetch_failed")  # pragma: no cover

    def _wait_rate_limit(self, task: TaskRecord, interval: float) -> None:
        with self._rate_lock:
            if self._last_request_at is not None:
                remaining = interval - (self._monotonic() - self._last_request_at)
                while remaining > 0:
                    task_context = TaskExecutionContext(
                        self._repository, task.task_id, task.version
                    )
                    task_context.check_control(force=True)
                    delay = min(remaining, 0.05)
                    self._sleep(delay)
                    remaining = interval - (self._monotonic() - self._last_request_at)
            self._last_request_at = self._monotonic()


__all__ = ["CrawlTaskPipeline"]

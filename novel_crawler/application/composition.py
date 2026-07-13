"""Production composition root for the unified application service."""

from __future__ import annotations

from typing import Any, Protocol, cast

from novel_crawler.acquisition.http import HttpPageAcquirer
from novel_crawler.acquisition.models import AcquiredPage
from novel_crawler.adaptation import (
    ConfigManager,
    ConfigRegistry,
    ConfigRevalidator,
    ProbeService,
    StaticAdaptiveService,
)
from novel_crawler.application.pipeline import CrawlTaskPipeline
from novel_crawler.application.service import ApplicationService
from novel_crawler.core.crawler import CrawlerService
from novel_crawler.core.fetcher import Fetcher
from novel_crawler.runtime.env import RuntimeContext
from novel_crawler.sites.bqg import BqgAdapter
from novel_crawler.sites.router import AdapterRouter
from novel_crawler.sites.twbook import TwbookAdapter
from novel_crawler.task_engine import AdaptiveTaskController, BackgroundTaskExecutor, TaskRepository, TaskStatus


class _HttpAcquirer(Protocol):
    def fetch_page(self, url: str, **kwargs: object) -> AcquiredPage: ...


def build_application(
    ctx: RuntimeContext,
    *,
    driver: object | None = None,
    http_acquirer: _HttpAcquirer | None = None,
    recover_on_start: bool = True,
    max_workers: int = 4,
    max_queue_size: int = 128,
) -> ApplicationService:
    """Wire all private runtime dependencies and start the task executor."""
    repository: TaskRepository | None = None
    crawler: CrawlerService | None = None
    controller: AdaptiveTaskController | None = None
    executor: BackgroundTaskExecutor | None = None
    try:
        registry = ConfigRegistry(ctx.data_dir / "config-registry")
        del driver  # source-compatible argument; production browser acquisition is disabled
        http = http_acquirer or HttpPageAcquirer()
        probe = ProbeService(acquirer=cast(Any, http))
        revalidator = ConfigRevalidator(acquirer=cast(Any, http), registry=registry)
        manager = ConfigManager(registry, revalidator, probe)
        router = AdapterRouter(
            (BqgAdapter(), TwbookAdapter(ctx.project_dir)),
            Fetcher(acquirer=cast(Any, http)),
        )
        adaptive = StaticAdaptiveService(manager, router)
        # Task CAS/events are intentionally isolated from ctx.db_path's book
        # content schema; both databases remain private under the same data_dir.
        repository = TaskRepository(ctx.data_dir / "tasks.db")
        controller = AdaptiveTaskController(repository, cast(Any, adaptive))
        crawler = CrawlerService(ctx)

        pipeline = CrawlTaskPipeline(
            repository,
            crawler.storage,
            registry,
            cast(Any, http),
            exporter=lambda book_id, fmt: crawler.export(book_id, fmt),
            adapter_router=router,
        )
        handlers = {
            TaskStatus.PROBING: controller.probe_handler,
            **pipeline.handlers,
        }
        executor = BackgroundTaskExecutor(
            repository,
            handlers,
            max_workers=max_workers,
            max_queue_size=max_queue_size,
            recover_on_start=False,
        )
        controller.bind_scheduler(executor.schedule_active)
        service = ApplicationService(
            repository, executor, controller=controller, crawler=cast(Any, crawler)
        )
        if recover_on_start:
            last_error: Exception | None = None
            for _attempt in range(3):
                try:
                    executor.recover_and_schedule()
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
            if last_error is not None:
                raise RuntimeError("startup_recovery_failed") from None
        return service
    except BaseException:
        executor_stopped = executor is None
        if executor is not None:
            try:
                executor_stopped = executor.shutdown(wait=True, timeout=5)
            except Exception:
                executor_stopped = False
        if executor_stopped and controller is not None:
            try:
                controller.close()
            except Exception:
                pass
        if executor_stopped and repository is not None:
            try:
                repository.close()
            except Exception:
                pass
        if executor_stopped and crawler is not None:
            try:
                _close_crawler(crawler)
            except Exception:
                pass
        raise


def _close_crawler(crawler: CrawlerService) -> None:
    close = getattr(crawler, "close", None)
    if callable(close):
        close()
    else:
        crawler.storage.close()


__all__ = ["build_application"]

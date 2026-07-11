from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.application.errors import ApplicationError
from novel_crawler.application.pipeline import CrawlTaskPipeline
from novel_crawler.application.site_adapter import SiteConfigAdapter
from novel_crawler.browser import BrowserAcquirer, BrowserSessionStore, VerificationRequired
from novel_crawler.core.storage import Storage
from novel_crawler.task_engine import BackgroundTaskExecutor, TaskExecutionContext, TaskRepository, TaskStatus
from novel_crawler.task_engine.executor import RecoverableTaskError


class Registry:
    def __init__(self, config: SiteConfig | None) -> None:
        self.config = config

    def load_active(self, url: str) -> SiteConfig | None:
        return self.config


class Page:
    def __init__(self, html: str) -> None:
        self.html = html


class Acquirer:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.task_keys: list[str | None] = []

    def fetch(self, url: str, *, task_key: str | None = None) -> Page:
        self.task_keys.append(task_key)
        return Page(self.pages[url])


def site_config() -> SiteConfig:
    return SiteConfig.new(
        site="fixture",
        domain="example.test",
        url_patterns=["/books/**"],
        selectors={
            "clean": [".noise"],
            "book": {"title": "h1.book", "chapter_list": "#list"},
            "chapter": {"chapter_title": "h1.chapter", "content": "article"},
        },
        request_policy={"timeout_seconds": 5, "max_retries": 0, "rate_limit_seconds": 0},
    )


def active_task(repo: TaskRepository, url: str, metadata: dict):
    created = repo.create_task(url, metadata=metadata)
    probing = repo.transition(created.task_id, TaskStatus.PROBING, expected_version=created.version)
    return repo.transition(created.task_id, TaskStatus.VALIDATING, expected_version=probing.version)


def context(repo: TaskRepository, task) -> TaskExecutionContext:
    return TaskExecutionContext(repo, task.task_id, task.version)


def test_validating_persists_bounded_plan_and_crawling_completes_and_exports(tmp_path: Path) -> None:
    url = "https://example.test/books/index.html"
    pages = {
        url: "<h1 class=book>Book</h1><div id=list><a href='1.html'>One</a><a href='2.html'>Two</a></div>",
        "https://example.test/books/1.html": "<h1 class=chapter>One</h1><article><p>A</p></article>",
        "https://example.test/books/2.html": "<h1 class=chapter>Two</h1><article><p>B</p></article>",
    }
    repo = TaskRepository(tmp_path / "tasks.db")
    storage = Storage(tmp_path / "crawl.db", tmp_path / "data")
    exported: list[tuple[int, str]] = []
    acquirer = Acquirer(pages)
    pipeline = CrawlTaskPipeline(
        repo,
        storage,
        Registry(site_config()),
        acquirer,
        exporter=lambda book_id, fmt: exported.append((book_id, fmt)),
    )
    task = active_task(
        repo,
        url,
        {
            "crawl": {
                "start": None,
                "count": None,
                "max_chapters": None,
                "concurrency": 1,
                "export": True,
                "export_format": "txt",
                "chase": False,
            }
        },
    )
    assert pipeline.validating(context(repo, task), task) is TaskStatus.READY
    plan = repo.load_checkpoint(task.task_id, "crawl-plan")
    assert set(plan.payload) == {"book_id", "export", "export_format"}
    ready = repo.transition(task.task_id, TaskStatus.READY, expected_version=task.version)
    crawling = repo.transition(task.task_id, TaskStatus.CRAWLING, expected_version=ready.version)
    assert pipeline.crawling(context(repo, crawling), crawling) is TaskStatus.COMPLETED
    assert storage.progress(plan.payload["book_id"]) == {"total": 2, "done": 2}
    assert exported == [(plan.payload["book_id"], "txt")]
    assert acquirer.task_keys == [task.task_id, task.task_id, task.task_id]
    repo.close()
    storage.close()


def test_pipeline_consumes_real_browser_acquirer_page_snapshot(tmp_path: Path) -> None:
    url = "https://example.test/books/index.html"
    html = "<h1 class=book>Book</h1><div id=list><a href='1.html'>One</a></div>"

    class Http:
        def fetch_page(self, requested: str, **_kwargs):
            snapshot = PageSnapshot(
                requested, requested, 200, {}, "utf-8", html, html.encode(), "GET", (), datetime.now(UTC)
            )
            return AcquiredPage(snapshot, requested)

    class Classification:
        kind = PageKind.BOOK_INDEX

    class Classifier:
        def classify(self, _snapshot):
            return Classification()

    class Driver:
        def launch(self, **_kwargs):
            raise AssertionError("static HTTP page must not launch browser")

    acquirer = BrowserAcquirer(
        http=Http(),  # type: ignore[arg-type]
        classifier=Classifier(),  # type: ignore[arg-type]
        driver=Driver(),  # type: ignore[arg-type]
        sessions=BrowserSessionStore(tmp_path / "sessions"),
    )
    repo = TaskRepository(tmp_path / "real-browser-tasks.db")
    storage = Storage(tmp_path / "real-browser-crawl.db", tmp_path / "real-browser-data")
    pipeline = CrawlTaskPipeline(repo, storage, Registry(site_config()), acquirer)
    task = active_task(repo, url, {"crawl": {"concurrency": 1, "export": False}})
    assert pipeline.validating(context(repo, task), task) is TaskStatus.READY
    repo.close()
    storage.close()


def test_pipeline_revalidates_existing_plan_and_enforces_defensive_contracts(tmp_path: Path) -> None:
    url = "https://example.test/books/index"
    html = "<h1 class=book>Book</h1><div id=list><a href='1'>One</a><a href='2'>Two</a></div>"
    registry = Registry(site_config())
    repo = TaskRepository(tmp_path / "defensive-tasks.db")
    storage = Storage(tmp_path / "defensive-crawl.db", tmp_path / "defensive-data")
    pipeline = CrawlTaskPipeline(repo, storage, registry, Acquirer({url: html}))
    assert set(pipeline.handlers) == {TaskStatus.VALIDATING, TaskStatus.CRAWLING}
    task = active_task(repo, url, {"crawl": {"concurrency": 1, "max_chapters": 1, "export": False}})
    assert pipeline.validating(context(repo, task), task) is TaskStatus.READY
    assert pipeline.validating(context(repo, task), task) is TaskStatus.READY
    assert repo.load_checkpoint(task.task_id, "crawl-plan").version == 1
    assert len(storage.all_chapters(repo.load_checkpoint(task.task_id, "crawl-plan").payload["book_id"])) == 1

    registry.config = None
    ready = repo.transition(task.task_id, TaskStatus.READY, expected_version=task.version)
    crawling = repo.transition(task.task_id, TaskStatus.CRAWLING, expected_version=ready.version)
    with pytest.raises(ValueError, match="active_config_missing"):
        pipeline.crawling(context(repo, crawling), crawling)

    missing = repo.create_task(url)
    for metadata, code in [
        ({}, "crawl_options_missing"),
        ({"crawl": {"chase": True}}, "chase_unsupported"),
        ({"crawl": {"concurrency": 2}}, "concurrency_unsupported"),
    ]:
        candidate = replace(missing, metadata=metadata)
        with pytest.raises((ValueError, ApplicationError), match=code):
            pipeline._options(candidate)
    repo.close()
    storage.close()


def test_pipeline_explicit_legacy_fallback_and_mismatch(tmp_path: Path) -> None:
    url = "https://example.test/books/index"
    html = "<h1 class=book>Book</h1><div id=list><a href='1'>One</a></div>"
    repo = TaskRepository(tmp_path / "legacy-tasks.db")
    storage = Storage(tmp_path / "legacy-crawl.db", tmp_path / "legacy-data")
    adapter = SiteConfigAdapter(site_config())
    pipeline = CrawlTaskPipeline(
        repo, storage, Registry(None), Acquirer({url: html}), legacy_adapter=lambda _url: adapter
    )
    task = active_task(repo, url, {"crawl": {"concurrency": 1, "export": False}})
    assert pipeline.validating(context(repo, task), task) is TaskStatus.READY

    raw = site_config().to_sensitive_dict()
    raw["url_patterns"] = ["/other/**"]
    mismatch_adapter = SiteConfigAdapter(SiteConfig.from_dict(raw))
    mismatch = CrawlTaskPipeline(
        repo,
        storage,
        Registry(None),
        Acquirer({}),
        legacy_adapter=lambda _url: mismatch_adapter,
    )
    with pytest.raises(ValueError, match="active_config_mismatch"):
        mismatch.validating(context(repo, task), task)
    repo.close()
    storage.close()


def test_protected_page_after_probe_fails_closed_with_stable_recoverable_code(tmp_path: Path) -> None:
    class Protected:
        def fetch(self, _url: str, *, task_key: str | None = None):
            raise VerificationRequired(
                "verification_required", original_url="https://private.test/path?token=hidden"
            )

    repo = TaskRepository(tmp_path / "protected-tasks.db")
    storage = Storage(tmp_path / "protected-crawl.db", tmp_path / "protected-data")
    pipeline = CrawlTaskPipeline(repo, storage, Registry(site_config()), Protected())
    task = active_task(repo, "https://example.test/books/index", {"crawl": {"concurrency": 1}})
    with pytest.raises(RecoverableTaskError) as caught:
        pipeline.validating(context(repo, task), task)
    assert caught.value.error_code == "verification_required"
    assert "private" not in repr(caught.value)
    repo.close()
    storage.close()


def test_validating_fails_closed_to_probe_without_active_config(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.db")
    storage = Storage(tmp_path / "crawl.db", tmp_path / "data")
    pipeline = CrawlTaskPipeline(repo, storage, Registry(None), Acquirer({}))
    task = active_task(repo, "https://example.test/books/1", {"crawl": {"concurrency": 1}})
    assert pipeline.validating(context(repo, task), task) is TaskStatus.PROBING
    assert repo.list_checkpoints(task.task_id) == []
    repo.close()
    storage.close()


@pytest.mark.parametrize(
    ("html", "code"),
    [
        ("<div id=list><a href='1'>One</a></div>", "book_title_missing"),
        ("<h1 class=book>Book</h1><div id=list></div>", "chapter_list_empty"),
    ],
)
def test_validation_errors_are_stable(tmp_path: Path, html: str, code: str) -> None:
    repo = TaskRepository(tmp_path / f"{code}.db")
    storage = Storage(tmp_path / f"{code}-crawl.db", tmp_path / code)
    url = "https://example.test/books/1"
    pipeline = CrawlTaskPipeline(repo, storage, Registry(site_config()), Acquirer({url: html}))
    task = active_task(repo, url, {"crawl": {"concurrency": 1}})
    with pytest.raises(ValueError, match=code):
        pipeline.validating(context(repo, task), task)
    repo.close()
    storage.close()


def wait_status(repo: TaskRepository, task_id: str, status: TaskStatus) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if repo.get_task(task_id).status is status:
            return
        time.sleep(0.01)
    assert repo.get_task(task_id).status is status


def test_static_fixture_runs_end_to_end_and_recovers_failed_chapter_after_restart(tmp_path: Path) -> None:
    url = "https://example.test/books/index.html"
    pages = {url: "<h1 class=book>Book</h1><div id=list><a href='1.html'>One</a></div>"}
    acquirer = Acquirer(pages)
    repo = TaskRepository(tmp_path / "tasks.db")
    storage = Storage(tmp_path / "crawl.db", tmp_path / "data")
    exports: list[tuple[int, str]] = []
    pipeline = CrawlTaskPipeline(
        repo, storage, Registry(site_config()), acquirer, exporter=lambda book_id, fmt: exports.append((book_id, fmt))
    )
    handlers = {TaskStatus.PROBING: lambda _context, _task: TaskStatus.VALIDATING, **pipeline.handlers}
    first = BackgroundTaskExecutor(repo, handlers, max_workers=1)
    task = repo.create_task(url, metadata={"crawl": {"concurrency": 1}})
    first.submit(task.task_id)
    wait_status(repo, task.task_id, TaskStatus.RECOVERABLE_FAILED)
    assert repo.load_checkpoint(task.task_id, "chapter-progress").payload["failed"] == 1
    assert first.shutdown(timeout=2)

    pages["https://example.test/books/1.html"] = "<h1 class=chapter>One</h1><article>Recovered</article>"
    second = BackgroundTaskExecutor(repo, handlers, max_workers=1, recover_on_start=True)
    second.resume(task.task_id)
    wait_status(repo, task.task_id, TaskStatus.COMPLETED)
    assert len(exports) == 1
    assert storage.progress(repo.load_checkpoint(task.task_id, "crawl-plan").payload["book_id"])["done"] == 1
    assert second.shutdown(timeout=2)
    repo.close()
    storage.close()

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.acquisition.http import AcquisitionError
from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.application.errors import ApplicationError
from novel_crawler.application.pipeline import CrawlTaskPipeline
from novel_crawler.application.site_adapter import SiteConfigAdapter
from novel_crawler.browser import BrowserAcquirer, BrowserSessionStore
from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.storage import Storage
from novel_crawler.task_engine import (
    BackgroundTaskExecutor,
    TaskExecutionContext,
    TaskRepository,
    TaskStatus,
)
from novel_crawler.task_engine.executor import TerminalTaskError


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

    def fetch(
        self, url: str, *, task_key: str | None = None, timeout: float | None = None
    ) -> Page:
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
    assert set(plan.payload) == {
        "book_id", "chapter_start", "chapter_end", "chapter_count", "export", "export_format",
    }
    assert (plan.payload["chapter_start"], plan.payload["chapter_end"], plan.payload["chapter_count"]) == (1, 2, 2)
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


def test_javascript_only_page_after_probe_fails_closed_without_browser_fallback(tmp_path: Path) -> None:
    class Protected:
        def fetch(
            self,
            _url: str,
            *,
            task_key: str | None = None,
            timeout: float | None = None,
        ):
            raise AcquisitionError("javascript_required", "https://private.test", False)

    repo = TaskRepository(tmp_path / "protected-tasks.db")
    storage = Storage(tmp_path / "protected-crawl.db", tmp_path / "protected-data")
    pipeline = CrawlTaskPipeline(repo, storage, Registry(site_config()), Protected())
    task = active_task(repo, "https://example.test/books/index", {"crawl": {"concurrency": 1}})
    with pytest.raises(TerminalTaskError) as caught:
        pipeline.validating(context(repo, task), task)
    assert caught.value.error_code == "javascript_required"
    assert "private" not in repr(caught.value)
    repo.close()
    storage.close()


def test_request_policy_controls_timeout_retry_count_and_rate_limit(tmp_path: Path) -> None:
    url = "https://example.test/books/index"
    html = "<h1 class=book>Book</h1><div id=list><a href='1'>One</a></div>"
    raw = site_config().to_sensitive_dict()
    raw["request_policy"] = {
        "timeout_seconds": 7,
        "max_retries": 2,
        "rate_limit_seconds": 1,
    }
    configured = SiteConfig.from_dict(raw)
    clock = [0.0]
    sleeps: list[float] = []

    class Flaky:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, float | None]] = []

        def fetch(self, _url: str, *, task_key=None, timeout=None):
            self.calls.append((task_key, timeout))
            if len(self.calls) < 3:
                raise AcquisitionError("transport_error", "https://example.test", True)
            return Page(html)

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    repo = TaskRepository(tmp_path / "policy-tasks.db")
    storage = Storage(tmp_path / "policy-crawl.db", tmp_path / "policy-data")
    acquirer = Flaky()
    pipeline = CrawlTaskPipeline(
        repo,
        storage,
        Registry(configured),
        acquirer,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    task = active_task(repo, url, {"crawl": {"concurrency": 1, "export": False}})
    assert pipeline.validating(context(repo, task), task) is TaskStatus.READY
    assert acquirer.calls == [(task.task_id, 7.0)] * 3
    assert sum(sleeps) == pytest.approx(2.0)
    assert sleeps and max(sleeps) <= 0.05
    repo.close()
    storage.close()


def test_static_pipeline_never_invokes_browser_interaction_hooks(tmp_path: Path) -> None:
    url = "https://example.test/books/index"
    prepared: list[tuple[str, str]] = []
    adopted: list[tuple[str, str, str]] = []

    class Protected:
        def fetch(self, requested, *, task_key=None, timeout=None):
            raise AcquisitionError("challenge_unsupported", "https://example.test", False)

    def adopt(task, requested, signal):
        adopted.append((task.task_id, requested, signal.code))

    repo = TaskRepository(tmp_path / "broker-tasks.db")
    storage = Storage(tmp_path / "broker-crawl.db", tmp_path / "broker-data")
    pipeline = CrawlTaskPipeline(
        repo,
        storage,
        Registry(site_config()),
        Protected(),
        interaction_handler=adopt,
        access_preparer=lambda requested, task_id: prepared.append((requested, task_id)),
    )
    task = active_task(repo, url, {"crawl": {"concurrency": 1}})
    with pytest.raises(TerminalTaskError):
        pipeline.validating(context(repo, task), task)
    assert prepared == []
    assert adopted == []
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


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"start": 3}, [3, 4, 5]),
        ({"count": 2}, [1, 2]),
        ({"max_chapters": 2}, [1, 2]),
    ],
)
def test_new_task_only_crawls_its_planned_range_for_existing_book(
    tmp_path: Path,
    options: dict[str, int],
    expected: list[int],
) -> None:
    url = "https://example.test/books/index.html"
    index_html = "<h1 class=book>Book</h1><div id=list>" + "".join(
        f"<a href='{index}.html'>Chapter {index}</a>" for index in range(1, 6)
    ) + "</div>"
    pages = {url: index_html}
    pages.update({
        f"https://example.test/books/{index}.html": (
            f"<h1 class=chapter>Chapter {index}</h1><article>Body {index}</article>"
        )
        for index in expected
    })
    repo = TaskRepository(tmp_path / "tasks.db")
    storage = Storage(tmp_path / "crawl.db", tmp_path / "data")
    book_id = storage.upsert_book(Book(title="Book", url=url, site="fixture"))
    storage.upsert_chapters(
        book_id,
        [Chapter(index, f"Old {index}", f"https://example.test/books/{index}.html") for index in range(1, 6)],
    )
    acquirer = Acquirer(pages)
    pipeline = CrawlTaskPipeline(repo, storage, Registry(site_config()), acquirer)
    task = active_task(repo, url, {"crawl": {**options, "concurrency": 1, "export": False}})
    assert pipeline.validating(context(repo, task), task) is TaskStatus.READY
    plan = repo.load_checkpoint(task.task_id, "crawl-plan").payload
    assert (plan["book_id"], plan["chapter_start"], plan["chapter_end"], plan["chapter_count"]) == (
        book_id,
        expected[0],
        expected[-1],
        len(expected),
    )
    ready = repo.transition(task.task_id, TaskStatus.READY, expected_version=task.version)
    crawling = repo.transition(task.task_id, TaskStatus.CRAWLING, expected_version=ready.version)
    assert pipeline.crawling(context(repo, crawling), crawling) is TaskStatus.COMPLETED
    assert acquirer.task_keys == [task.task_id] * (len(expected) + 1)
    assert storage.progress(book_id, start=expected[0], end=expected[-1]) == {
        "total": len(expected),
        "done": len(expected),
    }
    assert repo.load_checkpoint(task.task_id, "chapter-progress").payload["next_index"] == expected[-1] + 1
    repo.close()
    storage.close()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: {key: value for key, value in plan.items() if key != "chapter_count"},
        lambda plan: {**plan, "chapter_count": True},
        lambda plan: {**plan, "chapter_end": plan["chapter_end"] + 1},
        lambda plan: {**plan, "chapter_start": 10_000_001},
        lambda plan: {**plan, "private": 1},
    ],
)
def test_crawling_rejects_tampered_or_unbounded_plan(tmp_path: Path, mutate) -> None:
    url = "https://example.test/books/index.html"
    html = "<h1 class=book>Book</h1><div id=list><a href='1.html'>One</a></div>"
    repo = TaskRepository(tmp_path / "tasks.db")
    storage = Storage(tmp_path / "crawl.db", tmp_path / "data")
    pipeline = CrawlTaskPipeline(repo, storage, Registry(site_config()), Acquirer({url: html}))
    task = active_task(repo, url, {"crawl": {"concurrency": 1, "export": False}})
    assert pipeline.validating(context(repo, task), task) is TaskStatus.READY
    saved = repo.load_checkpoint(task.task_id, "crawl-plan")
    repo.save_checkpoint(task.task_id, "crawl-plan", mutate(dict(saved.payload)), expected_version=saved.version)
    ready = repo.transition(task.task_id, TaskStatus.READY, expected_version=task.version)
    crawling = repo.transition(task.task_id, TaskStatus.CRAWLING, expected_version=ready.version)
    with pytest.raises(ValueError, match="crawl_plan_invalid"):
        pipeline.crawling(context(repo, crawling), crawling)
    repo.close()
    storage.close()


def test_crawling_rejects_plan_when_range_inventory_is_incomplete(tmp_path: Path) -> None:
    url = "https://example.test/books/index.html"
    html = "<h1 class=book>Book</h1><div id=list><a href='1.html'>One</a><a href='2.html'>Two</a></div>"
    repo = TaskRepository(tmp_path / "tasks.db")
    storage = Storage(tmp_path / "crawl.db", tmp_path / "data")
    pipeline = CrawlTaskPipeline(repo, storage, Registry(site_config()), Acquirer({url: html}))
    task = active_task(repo, url, {"crawl": {"concurrency": 1, "export": False}})
    assert pipeline.validating(context(repo, task), task) is TaskStatus.READY
    plan = repo.load_checkpoint(task.task_id, "crawl-plan").payload
    storage.conn.execute(
        "DELETE FROM chapters WHERE book_id=? AND chapter_index=?",
        (plan["book_id"], plan["chapter_end"]),
    )
    storage.conn.commit()
    ready = repo.transition(task.task_id, TaskStatus.READY, expected_version=task.version)
    crawling = repo.transition(task.task_id, TaskStatus.CRAWLING, expected_version=ready.version)
    with pytest.raises(ValueError, match="crawl_plan_invalid"):
        pipeline.crawling(context(repo, crawling), crawling)
    repo.close()
    storage.close()


@pytest.mark.parametrize(
    ("code", "recoverable", "expected_status"),
    [
        ("http_404", False, TaskStatus.TERMINAL_FAILED),
        ("timeout", True, TaskStatus.RECOVERABLE_FAILED),
    ],
)
def test_executor_classifies_chapter_acquisition_errors_without_generic_swallowing(
    tmp_path: Path,
    code: str,
    recoverable: bool,
    expected_status: TaskStatus,
) -> None:
    url = "https://example.test/books/index.html"
    index_html = "<h1 class=book>Book</h1><div id=list><a href='1.html'>One</a></div>"

    class FailingChapter:
        def fetch(self, requested: str, *, task_key=None, timeout=None):
            if requested == url:
                return Page(index_html)
            raise AcquisitionError(code, "https://example.test", recoverable)

    repo = TaskRepository(tmp_path / "tasks.db")
    storage = Storage(tmp_path / "crawl.db", tmp_path / "data")
    pipeline = CrawlTaskPipeline(repo, storage, Registry(site_config()), FailingChapter())
    executor = BackgroundTaskExecutor(
        repo,
        {TaskStatus.PROBING: lambda _context, _task: TaskStatus.VALIDATING, **pipeline.handlers},
        max_workers=1,
    )
    task = repo.create_task(url, metadata={"crawl": {"concurrency": 1, "export": False}})
    executor.submit(task.task_id)
    wait_status(repo, task.task_id, expected_status)
    failed = repo.get_task(task.task_id)
    assert failed.error_code == code
    if expected_status is TaskStatus.TERMINAL_FAILED:
        assert executor.resume(task.task_id).status is TaskStatus.TERMINAL_FAILED
    assert executor.shutdown(timeout=2)
    repo.close()
    storage.close()


def test_executor_classifies_nonrecoverable_validation_fetch_as_terminal(tmp_path: Path) -> None:
    class MissingIndex:
        def fetch(self, _requested: str, *, task_key=None, timeout=None):
            raise AcquisitionError("http_404", "https://example.test", False)

    url = "https://example.test/books/index.html"
    repo = TaskRepository(tmp_path / "tasks.db")
    storage = Storage(tmp_path / "crawl.db", tmp_path / "data")
    pipeline = CrawlTaskPipeline(repo, storage, Registry(site_config()), MissingIndex())
    executor = BackgroundTaskExecutor(
        repo,
        {TaskStatus.PROBING: lambda _context, _task: TaskStatus.VALIDATING, **pipeline.handlers},
        max_workers=1,
    )
    task = repo.create_task(url, metadata={"crawl": {"concurrency": 1, "export": False}})
    executor.submit(task.task_id)
    wait_status(repo, task.task_id, TaskStatus.TERMINAL_FAILED)
    assert repo.get_task(task.task_id).error_code == "http_404"
    assert executor.resume(task.task_id).status is TaskStatus.TERMINAL_FAILED
    assert executor.shutdown(timeout=2)
    repo.close()
    storage.close()

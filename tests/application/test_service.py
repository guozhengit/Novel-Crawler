from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from novel_crawler.adaptation.config_manager import ConfigResolution, ResolutionKind
from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.application import (
    ApplicationError,
    ApplicationService,
    CrawlOptions,
    TaskView,
)
from novel_crawler.browser.adaptive import AdaptiveResult
from novel_crawler.browser.models import VerificationStatus, VerificationTicket
from novel_crawler.core.storage import BookDeletionResult
from novel_crawler.easyvoice import EasyVoiceConversionResult, EasyVoiceExportResult
from novel_crawler.task_engine import (
    AdaptiveTaskController,
    BackgroundTaskExecutor,
    ExecutorClosed,
    ExecutorQueueFull,
    TaskRepository,
    TaskStatus,
)


class FakeExecutor:
    def __init__(self, repository: TaskRepository | None = None) -> None:
        self.repository = repository
        self.submitted: list[str] = []
        self.closed = False
        self.full = False

    def submit(self, task_id: str) -> bool:
        if self.closed:
            raise ExecutorClosed("private executor detail")
        if self.full:
            raise ExecutorQueueFull("private queue detail")
        self.submitted.append(task_id)
        return True

    def pause(self, task_id: str):
        raise AssertionError("service controls repository directly")

    def resume(self, task_id: str):
        self.submitted.append(task_id)
        if self.repository is None:
            return None
        task = self.repository.get_task(task_id)
        if task.resume_status is None:
            return task
        return self.repository.transition(
            task_id, task.resume_status, expected_version=task.version, reason="fake_resume"
        )

    def cancel(self, task_id: str):
        raise AssertionError("service controls repository directly")

    def shutdown(self, *, wait: bool, timeout: float | None) -> bool:
        self.closed = True
        return True


class FakeController:
    def __init__(self) -> None:
        self.actions: list[tuple[str, str]] = []
        self.closed = False

    def interaction(self, task_id: str):
        if task_id.endswith("0"):
            return None
        return FakeSummary()

    def continue_verification(self, task_id: str):
        self.actions.append(("continue", task_id))

    def confirm_config(self, task_id: str, selector_overrides=None):
        self.actions.append(("confirm", task_id))

    def cancel_interaction(self, task_id: str):
        self.actions.append(("cancel", task_id))

    def retry_cleanup(self, task_id: str):
        self.actions.append(("cleanup", task_id))

    def close(self) -> bool:
        self.closed = True
        return True


class FakeSummary:
    verification_required = True
    confirmation_required = False
    cleanup_required = False

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "kind": "verification",
            "safe_origin": "example.com",
            "attempt": 1,
            "expires_at": None,
            "cleanup_source": None,
            "verification_required": True,
            "confirmation_required": False,
            "cleanup_required": False,
        }


class FakeCrawler:
    def __init__(self) -> None:
        self.closed = False
        self.export_args: tuple[int, str] | None = None

    def list_books(self):
        return [{"id": 4, "title": "Safe title", "site": "demo", "url": "https://secret.test/a", "done": 2, "total": 3, "profile_path": "C:\\private"}]

    def progress(self, book_id: int):
        return {"total": 10, "done": 7, "failed": 1, "pending": 2, "private": "token=abc"}

    def report(self, book_id: int):
        return "URL: https://secret.test/a\npassword=hunter2\nprogress ok"

    def export(self, book_id: int, fmt: str):
        self.export_args = (book_id, fmt)
        return Path("C:/Users/private/output/secret.txt")

    def export_easyvoice(self, book_id: int, output: Path | None = None):
        return EasyVoiceExportResult(
            book_id=book_id,
            export_path=output or Path("C:/Users/private/output/book.json"),
            chapter_count=2,
        )

    def convert_easyvoice(
        self,
        book_id: int,
        *,
        export_path: Path | None = None,
        output_dir: Path | None = None,
        options=None,
    ):
        return EasyVoiceConversionResult(
            book_id=book_id,
            export_path=export_path or Path("C:/Users/private/output/book.json"),
            output_dir=output_dir or Path("C:/Users/private/audio"),
            manifest_path=Path("C:/Users/private/audio/book-4/manifest.json"),
            returncode=0,
            stdout="completed",
            stderr="",
        )

    def delete_book(self, book_id: int):
        return BookDeletionResult(3, "pending", "deletion_cleanup_retryable")

    def validate(self, book_id: int):
        return SimpleNamespace(
            book_id=book_id,
            total=3,
            done=2,
            failed=1,
            pending=0,
            ok=False,
            issues=[SimpleNamespace(level="error", code="FAILED", message="https://private.test")],
        )

    def fix_titles(self, book_id: int, dry_run: bool = False):
        return SimpleNamespace(total=3, fixed=1, details=["C:\\private\\chapter.txt"])

    def dedup(self, book_id: int, remove: bool = False):
        return SimpleNamespace(total=3, exact_dupes=1, similar_dupes=0, details=["token=private"])

    def logs(self, book_id: int | None = None, limit: int = 50):
        return [{"created_at": "2026-01-01T00:00:00+00:00", "level": "info", "book_id": book_id, "chapter_index": 1, "message": "https://private.test"}]

    def retry_failed(self, book_id: int, *, export: bool = True, concurrency: int = 1):
        return None

    def export_all(self, fmt: str = "txt"):
        return [Path("C:/private/one.txt")]

    def retry_all_failed(self):
        return 2

    def preview_chapter(self, book_id: int, chapter_index: int, length: int = 500):
        return "safe preview\nhttps://private.test"

    def stats(self):
        return {"books": 2, "chapters_total": 3, "chapters_done": 2, "chapters_failed": 1, "chapters_pending": 0, "completion_rate": 66.7, "sites": {"demo": 2}, "private": "token=x"}

    def validate_config(self, config_path: Path):
        return {"valid": False, "site": "demo", "domain": "private.test", "errors": ["C:\\private"], "warnings": ["safe"]}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def app(tmp_path: Path):
    repo = TaskRepository(tmp_path / "tasks.db")
    executor = FakeExecutor(repo)
    controller = FakeController()
    crawler = FakeCrawler()
    service = ApplicationService(repo, executor, controller=controller, crawler=crawler)
    yield service, repo, executor, controller, crawler
    service.close()


def test_create_validates_and_persists_only_bounded_safe_options(app) -> None:
    service, repo, executor, *_ = app
    view = service.create_crawl_task(
        "https://example.com/catalog?secret=query",
        CrawlOptions(start=2, count=20, max_chapters=10, concurrency=1, export=False, export_format="epub", chase=False),
    )

    assert isinstance(view, TaskView)
    assert executor.submitted == [view.task_id]
    record = repo.get_task(view.task_id)
    assert record.metadata == {
        "crawl": {
            "browser": "http",
            "chase": False,
            "concurrency": 1,
            "count": 20,
            "export": False,
            "export_format": "epub",
            "max_chapters": 10,
            "start": 2,
        }
    }
    assert "example.com" not in repr(view)
    assert "secret" not in str(view.to_safe_dict())


def test_create_blocks_third_party_public_urls_by_default(app, monkeypatch) -> None:
    service, repo, executor, *_ = app
    monkeypatch.delenv("NOVEL_CRAWLER_ALLOW_THIRD_PARTY", raising=False)

    with pytest.raises(ApplicationError) as caught:
        service.create_crawl_task("https://www.qidian.com/chapter/1/2/", {})

    assert caught.value.code == "third_party_crawl_disabled"
    assert repo.list_tasks() == []
    assert executor.submitted == []


def test_create_allows_third_party_public_urls_when_explicitly_enabled(app, monkeypatch) -> None:
    service, _repo, executor, *_ = app
    monkeypatch.setenv("NOVEL_CRAWLER_ALLOW_THIRD_PARTY", "1")

    view = service.create_crawl_task("https://www.qidian.com/chapter/1/2/", {})

    assert executor.submitted == [view.task_id]


def test_easyvoice_export_blocks_third_party_book_content_by_default(tmp_path: Path, monkeypatch) -> None:
    class Storage:
        def get_book(self, book_id: int):
            return SimpleNamespace(url="https://www.qidian.com/chapter/1/2/")

    class StoredCrawler(FakeCrawler):
        storage = Storage()

    monkeypatch.delenv("NOVEL_CRAWLER_ALLOW_THIRD_PARTY", raising=False)
    repo = TaskRepository(tmp_path / "tts-compliance.db")
    service = ApplicationService(repo, FakeExecutor(repo), crawler=StoredCrawler())

    with pytest.raises(ApplicationError) as caught:
        service.export_easyvoice_book(1)

    assert caught.value.code == "third_party_crawl_disabled"
    service.close()


def test_admin_book_facade_allowlists_and_redacts_legacy_results(app) -> None:
    service, *_ = app

    validation = service.validate_book(4)
    assert validation == {
        "book_id": 4,
        "total": 3,
        "done": 2,
        "failed": 1,
        "pending": 0,
        "ok": False,
        "issues": [{"level": "error", "code": "FAILED", "message": "[redacted]"}],
    }
    assert service.fix_book_titles(4) == {"total": 3, "fixed": 1, "details": ["[redacted]"]}
    assert service.deduplicate_book(4, True) == {
        "total": 3,
        "exact_dupes": 1,
        "similar_dupes": 0,
        "details": ["[redacted]"],
    }
    assert service.book_logs(4, 10)[0]["message"] == "[redacted]"
    assert service.retry_failed_chapters(4, False, 1) == {"completed": True}
    assert service.export_all_books("txt") == {
        "best_effort": True,
        "requested": 1,
        "attempted": 1,
        "succeeded": 1,
        "failed": 0,
        "remaining": 0,
        "format": "txt",
        "error_codes": [],
    }
    assert service.retry_all_failed_chapters() == {
        "best_effort": True,
        "requested": 0,
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "remaining": 0,
        "error_codes": [],
    }
    assert service.preview_book_chapter(4, 1, 100) == "safe preview\n[redacted]"
    assert service.book_stats() == {
        "books": 2,
        "chapters_total": 3,
        "chapters_done": 2,
        "chapters_failed": 1,
        "chapters_pending": 0,
        "completion_rate": 66.7,
        "sites": {"demo": 2},
    }
    assert service.validate_site_config(Path("config.json")) == {
        "valid": False,
        "site": "demo",
        "domain": "private.test",
        "errors": ["[redacted]"],
        "warnings": ["safe"],
    }


def test_batch_facade_reads_bounded_url_file_and_returns_only_safe_task_views(app, tmp_path) -> None:
    service, repo, executor, *_ = app
    source = tmp_path / "urls.txt"
    source.write_text("# comment\nhttps://one.test/book\n\nhttps://two.test/book\n", encoding="utf-8")

    result = service.create_crawl_tasks_from_file(source, concurrency=1, max_chapters=9)

    assert result["created"] == 2
    assert result["requested"] == 2
    assert result["submitted"] == 2
    assert result["failed"] == 0
    assert result["not_started"] == 0
    assert result["error_code"] is None
    assert len(result["tasks"]) == 2
    assert all(set(item) <= set(TaskView.__dataclass_fields__) for item in result["tasks"])
    assert len(executor.submitted) == 2
    assert all(repo.get_task(task_id).metadata["crawl"]["max_chapters"] == 9 for task_id in executor.submitted)

    with pytest.raises(ApplicationError, match="concurrency_unsupported"):
        service.create_crawl_tasks_from_file(source, concurrency=2)
    with pytest.raises(ApplicationError, match="concurrency_unsupported"):
        service.create_crawl_tasks_from_file(source, concurrency=True)
    with pytest.raises(ApplicationError, match="max_chapters_invalid"):
        service.create_crawl_tasks_from_file(source, max_chapters=0)
    assert len(executor.submitted) == 2


def test_batch_facade_returns_safe_partial_result_when_queue_fills(tmp_path: Path) -> None:
    class SecondSubmissionFull(FakeExecutor):
        def submit(self, task_id: str) -> bool:
            if self.submitted:
                raise ExecutorQueueFull("private url=https://secret.test token=x")
            return super().submit(task_id)

    repo = TaskRepository(tmp_path / "partial-batch.db")
    executor = SecondSubmissionFull(repo)
    service = ApplicationService(repo, executor)
    source = tmp_path / "urls.txt"
    source.write_text(
        "https://one.test/book\nhttps://two.test/book\nhttps://three.test/book\n",
        encoding="utf-8",
    )

    result = service.create_crawl_tasks_from_file(source)

    assert result["requested"] == 3
    assert result["created"] == 2
    assert result["submitted"] == 1
    assert result["failed"] == 1
    assert result["not_started"] == 1
    assert result["error_code"] == "task_queue_full"
    assert len(result["tasks"]) == 2
    assert all("source_url" not in task for task in result["tasks"])
    assert "secret.test" not in repr(result)
    service.close()


def test_batch_facade_counts_unstarted_inputs_when_failure_has_no_task_id(tmp_path: Path) -> None:
    class NoIdFailureService(ApplicationService):
        attempts = 0

        def create_crawl_task(self, url, options=None):
            self.attempts += 1
            if self.attempts == 2:
                raise ApplicationError("task_create_failed", retryable=True)
            return super().create_crawl_task(url, options)

    repo = TaskRepository(tmp_path / "batch-no-id.db")
    executor = FakeExecutor(repo)
    service = NoIdFailureService(repo, executor)
    source = tmp_path / "urls.txt"
    source.write_text(
        "https://one.test/book\nhttps://two.test/book\nhttps://three.test/book\n",
        encoding="utf-8",
    )

    result = service.create_crawl_tasks_from_file(source)

    assert result["requested"] == 3
    assert result["submitted"] == 1
    assert result["failed"] == 1
    assert result["not_started"] == 1
    assert result["created"] == 1
    service.close()


def test_best_effort_bulk_book_operations_report_partial_failures(tmp_path: Path) -> None:
    class PartialCrawler(FakeCrawler):
        def list_books(self):
            return [
                {"id": 1, "title": "one", "failed": 1},
                {"id": 2, "title": "two", "failed": 2},
            ]

        def export(self, book_id: int, fmt: str):
            if book_id == 2:
                raise RuntimeError("C:\\private\\two.txt token=x")
            return Path("C:/private/one.txt")

        def retry_failed(self, book_id: int, *, export: bool = True, concurrency: int = 1):
            if book_id == 2:
                raise RuntimeError("https://private.test token=x")

    repo = TaskRepository(tmp_path / "bulk-partial.db")
    crawler = PartialCrawler()
    service = ApplicationService(repo, FakeExecutor(repo), crawler=crawler)

    assert service.export_all_books("txt") == {
        "best_effort": True,
        "requested": 2,
        "attempted": 2,
        "succeeded": 1,
        "failed": 1,
        "remaining": 0,
        "format": "txt",
        "error_codes": ["export_failed"],
    }
    assert service.retry_all_failed_chapters() == {
        "best_effort": True,
        "requested": 2,
        "attempted": 2,
        "succeeded": 1,
        "failed": 1,
        "remaining": 0,
        "error_codes": ["retry_failed"],
    }
    service.close()


def test_retry_all_reports_bounded_remaining_work(tmp_path: Path) -> None:
    class ManyCrawler(FakeCrawler):
        def list_books(self):
            return [{"id": index, "failed": 1} for index in range(1, 1003)]

    repo = TaskRepository(tmp_path / "many-retries.db")
    service = ApplicationService(repo, FakeExecutor(repo), crawler=ManyCrawler())

    result = service.retry_all_failed_chapters()

    assert result["requested"] == 1002
    assert result["attempted"] == 1000
    assert result["succeeded"] == 1000
    assert result["failed"] == 0
    assert result["remaining"] == 2
    assert result["error_codes"] == ["retry_limit_reached"]
    service.close()


@pytest.mark.parametrize(
    ("options", "code"),
    [
        ({"start": 0}, "start_invalid"),
        ({"count": True}, "count_invalid"),
        ({"max_chapters": 1_000_001}, "max_chapters_invalid"),
        ({"concurrency": 65}, "concurrency_invalid"),
        ({"export": 1}, "export_invalid"),
        ({"export_format": "pdf"}, "export_format_invalid"),
        ({"chase": "yes"}, "chase_invalid"),
        ({"browser": "headless"}, "browser_invalid"),
        ({"unknown": 1}, "options_invalid"),
    ],
)
def test_create_rejects_invalid_options_without_creating_task(app, options, code) -> None:
    service, repo, *_ = app
    with pytest.raises(ApplicationError) as caught:
        service.create_crawl_task("https://example.com", options)
    assert caught.value.code == code
    assert repo.list_tasks() == []
    assert str(caught.value) == code


def test_queue_full_compensates_to_resumable_state_and_exposes_only_stable_error(app) -> None:
    service, repo, executor, *_ = app
    executor.full = True
    with pytest.raises(ApplicationError) as caught:
        service.create_crawl_task("https://example.com/private?q=token", {})

    assert caught.value.code == "task_queue_full"
    assert caught.value.retryable is True
    assert caught.value.task_id is not None
    record = repo.get_task(caught.value.task_id)
    assert record.status is TaskStatus.PAUSED
    assert record.resume_status is TaskStatus.CREATED
    assert record.error_code == "task_queue_full"
    assert "private" not in repr(caught.value)


def test_task_view_hides_private_record_event_and_checkpoint_data(app) -> None:
    service, repo, *_ = app
    record = repo.create_task("https://example.com/private?token=x", metadata={"title": "visible"})
    repo.save_checkpoint(record.task_id, "crawl", {"cursor": 991, "done": 2}, expected_version=None)

    view = service.get_task(record.task_id)
    events = service.task_events(record.task_id)
    encoded = repr((view, events, view.to_safe_dict()))

    assert view.checkpoint_count == 1
    assert "secret" not in encoded
    assert "private" not in encoded
    assert "cursor" not in encoded
    assert events[0].task_id == record.task_id


def test_pause_resume_cancel_are_idempotent_and_cleanup_gate_blocks_resume(app) -> None:
    service, repo, executor, *_ = app
    created = repo.create_task("https://example.com")
    paused = service.pause_task(created.task_id)
    assert service.pause_task(created.task_id) == paused
    resumed = service.resume_task(created.task_id)
    assert resumed.status == "created"
    assert executor.submitted[-1] == created.task_id
    cancelled = service.cancel_task(created.task_id)
    assert service.cancel_task(created.task_id) == cancelled

    gated = repo.create_task("https://example.org")
    waiting = repo.transition(gated.task_id, TaskStatus.PROBING, expected_version=0)
    repo.require_cleanup(
        waiting.task_id,
        expected_version=waiting.version,
        error_code="interaction_cleanup_required",
    )
    with pytest.raises(ApplicationError) as caught:
        service.resume_task(gated.task_id)
    assert caught.value.code == "cleanup_required"


def test_interaction_operations_delegate_and_return_refreshed_safe_view(app) -> None:
    service, repo, _, controller, _ = app
    task = repo.create_task("https://example.com")

    service.continue_interaction(task.task_id)
    service.confirm_interaction(task.task_id, {"content": ".chapter"})
    service.cancel_interaction(task.task_id)
    service.retry_cleanup(task.task_id)

    assert controller.actions == [
        ("continue", task.task_id),
        ("confirm", task.task_id),
        ("cancel", task.task_id),
        ("cleanup", task.task_id),
    ]


def test_legacy_queries_are_allowlisted_and_never_return_urls_paths_or_credentials(app) -> None:
    service, _, _, _, crawler = app
    books = service.list_books()
    progress = service.book_progress(4)
    report = service.book_report(4)
    exported = service.export_book(4, "txt")
    deletion = service.delete_book(4)
    encoded = repr((books, progress, report, exported, deletion))

    assert books == [{"id": 4, "title": "Safe title", "site": "demo", "done": 2, "total": 3}]
    assert progress == {"total": 10, "done": 7, "failed": 1, "pending": 2}
    assert report == "[redacted]\n[redacted]\nprogress ok"
    assert exported == {"completed": True, "format": "txt"}
    assert crawler.export_args == (4, "txt")
    assert deletion["cleanup_required"] is True
    assert not re.search(r"https?://|[A-Za-z]:\\|hunter2|token=", encoded, re.I)


def test_not_found_and_dependency_failures_map_to_stable_errors_without_exception_text(app) -> None:
    service, *_ = app
    with pytest.raises(ApplicationError) as missing:
        service.get_task("not-found")
    assert missing.value.code == "task_not_found"

    class BrokenCrawler(FakeCrawler):
        def list_books(self):
            raise RuntimeError("token=very-secret C:\\Users\\private")

    other = ApplicationService(app[1], app[2], crawler=BrokenCrawler())
    with pytest.raises(ApplicationError) as failed:
        other.list_books()
    assert failed.value.code == "crawler_operation_failed"
    assert str(failed.value) == "crawler_operation_failed"
    other.close()


def test_concurrent_create_and_control_and_hundred_task_listing_are_safe(app) -> None:
    service, *_ = app
    with ThreadPoolExecutor(max_workers=12) as pool:
        views = list(pool.map(lambda i: service.create_crawl_task(f"https://example.com/book/{i}", {}), range(100)))
        list(pool.map(lambda view: service.pause_task(view.task_id), views))
    listed = service.list_tasks(limit=100)
    assert len({view.task_id for view in listed}) == 100
    assert all(view.status == "paused" for view in listed)


def test_close_is_ordered_idempotent_and_rejects_new_work(tmp_path: Path) -> None:
    calls: list[str] = []

    class OrderedExecutor(FakeExecutor):
        def shutdown(self, *, wait: bool, timeout: float | None) -> bool:
            calls.append("executor")
            return super().shutdown(wait=wait, timeout=timeout)

    class OrderedController(FakeController):
        def close(self) -> bool:
            calls.append("controller")
            return super().close()

    class OrderedCrawler(FakeCrawler):
        def close(self) -> None:
            calls.append("crawler")
            super().close()

    class OrderedRepository(TaskRepository):
        def close(self) -> None:
            calls.append("repository")
            super().close()

    repo = OrderedRepository(tmp_path / "ordered.db")
    service = ApplicationService(repo, OrderedExecutor(), controller=OrderedController(), crawler=OrderedCrawler())
    service.close()
    service.close()
    assert calls == ["executor", "controller", "repository", "crawler"]
    with pytest.raises(ApplicationError) as caught:
        service.create_crawl_task("https://example.com", {})
    assert caught.value.code == "service_closed"


def test_context_manager_and_constructor_bounds(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "context.db")
    executor = FakeExecutor(repo)
    with ApplicationService(repo, executor) as service:
        assert service.list_tasks() == []
    assert executor.closed
    with pytest.raises(ApplicationError, match="service_closed"):
        service.list_tasks()
    invalid_repo = TaskRepository(tmp_path / "invalid.db")
    try:
        with pytest.raises(ValueError, match="close_timeout"):
            ApplicationService(invalid_repo, FakeExecutor(), close_timeout=-1)
    finally:
        invalid_repo.close()


def test_source_url_and_query_controls_are_mapped_to_stable_codes(app) -> None:
    service, repo, *_ = app
    with pytest.raises(ApplicationError) as invalid_url:
        service.create_crawl_task("file:///private/path", None)
    assert invalid_url.value.code == "source_url_invalid"
    with pytest.raises(ApplicationError) as invalid_list:
        service.list_tasks(limit=0)
    assert invalid_list.value.code == "task_query_invalid"
    with pytest.raises(ApplicationError) as missing_events:
        service.task_events("missing")
    assert missing_events.value.code == "task_not_found"
    with pytest.raises(ApplicationError) as missing_control:
        service.pause_task("missing")
    assert missing_control.value.code == "task_not_found"
    assert repo.list_tasks() == []


@pytest.mark.parametrize(
    ("options", "code"),
    [({"chase": True}, "chase_unsupported"), ({"concurrency": 2}, "concurrency_unsupported")],
)
def test_create_rejects_options_the_pipeline_cannot_honor(app, options, code) -> None:
    service, repo, executor, *_ = app
    with pytest.raises(ApplicationError) as caught:
        service.create_crawl_task("https://example.com/book", options)
    assert caught.value.code == code
    assert repo.list_tasks() == []
    assert executor.submitted == []


def test_closed_executor_compensates_and_resume_maps_capacity_errors(app) -> None:
    service, repo, executor, *_ = app
    executor.closed = True
    with pytest.raises(ApplicationError) as closed:
        service.create_crawl_task("https://example.com", {})
    assert closed.value.code == "task_executor_closed"
    assert repo.get_task(closed.value.task_id or "").status is TaskStatus.PAUSED

    executor.closed = False
    task = repo.create_task("https://example.org")
    executor.full = True
    with pytest.raises(ApplicationError) as full:
        service.resume_task(task.task_id)
    assert full.value.code == "task_queue_full"


def test_terminal_resume_and_task_filters_are_idempotent(app) -> None:
    service, repo, *_ = app
    task = repo.create_task("https://example.com")
    cancelled = service.cancel_task(task.task_id)
    assert service.resume_task(task.task_id) == cancelled
    assert service.list_tasks(statuses={TaskStatus.CANCELLED}) == [cancelled]
    assert service.task_events(task.task_id)[-1].to_safe_dict()["to_status"] == "cancelled"


def test_optional_facades_fail_closed_and_validate_book_inputs(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "optional.db")
    executor = FakeExecutor(repo)
    service = ApplicationService(repo, executor)
    task = repo.create_task("https://example.com")
    with pytest.raises(ApplicationError, match="interaction_unavailable"):
        service.continue_interaction(task.task_id)
    with pytest.raises(ApplicationError, match="crawler_unavailable"):
        service.list_books()
    service.close()

    repo2 = TaskRepository(tmp_path / "ids.db")
    service2 = ApplicationService(repo2, FakeExecutor(repo2), crawler=FakeCrawler())
    for operation in (service2.book_progress, service2.book_report, service2.delete_book):
        with pytest.raises(ApplicationError, match="book_id_invalid"):
            operation(True)
    with pytest.raises(ApplicationError, match="export_format_invalid"):
        service2.export_book(1, "pdf")
    service2.close()


@pytest.mark.parametrize("method", ["progress", "report", "export", "delete_book"])
def test_all_crawler_failures_are_redacted(tmp_path: Path, method: str) -> None:
    class BrokenCrawler(FakeCrawler):
        pass

    def fail(*_args, **_kwargs):
        raise RuntimeError("Authorization: Bearer super-secret C:\\private")

    crawler = BrokenCrawler()
    setattr(crawler, method, fail)
    repo = TaskRepository(tmp_path / f"{method}.db")
    service = ApplicationService(repo, FakeExecutor(repo), crawler=crawler)
    operation = {
        "progress": lambda: service.book_progress(1),
        "report": lambda: service.book_report(1),
        "export": lambda: service.export_book(1),
        "delete_book": lambda: service.delete_book(1),
    }[method]
    with pytest.raises(ApplicationError) as caught:
        operation()
    assert caught.value.code == "crawler_operation_failed"
    assert "secret" not in str(caught.value).casefold()
    service.close()


def test_interaction_and_safe_view_handle_malformed_dependency_data(tmp_path: Path) -> None:
    class UnsafeSummary(FakeSummary):
        def to_safe_dict(self) -> dict[str, object]:
            return {
                "kind": "verification" * 10,
                "attempt": -2,
                "expires_at": "https://private.test/token=x",
                "verification_required": 1,
                "confirmation_required": True,
                "cleanup_required": False,
            }

    class UnsafeController(FakeController):
        def interaction(self, task_id: str):
            return UnsafeSummary()

        def continue_verification(self, task_id: str):
            raise RuntimeError("token=secret")

    repo = TaskRepository(tmp_path / "unsafe-view.db")
    task = repo.create_task("https://example.com", metadata={"progress": {"done": 3, "failed": -1}})
    service = ApplicationService(repo, FakeExecutor(repo), controller=UnsafeController())
    view = service.get_task(task.task_id)
    assert view.interaction is not None
    assert view.interaction.kind == "unknown"
    assert view.interaction.attempt == 0
    assert view.interaction.expires_at is None
    assert view.interaction.verification_required is False
    assert dict(view.progress) == {"total": 0, "done": 3, "failed": 0, "pending": 0}
    with pytest.raises(ApplicationError) as failed:
        service.continue_interaction(task.task_id)
    assert failed.value.code == "interaction_failed"
    service.close()


def test_book_allowlist_redacts_malformed_counts_and_text(tmp_path: Path) -> None:
    class UnsafeCrawler(FakeCrawler):
        def list_books(self):
            return [{"id": True, "title": "token=secret", "site": 3, "done": -1, "pending": 99}]

    repo = TaskRepository(tmp_path / "unsafe-book.db")
    service = ApplicationService(repo, FakeExecutor(repo), crawler=UnsafeCrawler())
    assert service.list_books() == [
        {"id": 0, "title": "[redacted]", "site": "[redacted]", "done": 0, "pending": 99}
    ]
    service.close()


def test_close_falls_back_to_crawler_storage_and_contains_cleanup_failures(tmp_path: Path) -> None:
    class BrokenController(FakeController):
        def close(self) -> bool:
            raise RuntimeError("secret")

    class BrokenExecutor(FakeExecutor):
        def shutdown(self, *, wait: bool, timeout: float | None) -> bool:
            raise RuntimeError("secret")

    class StorageOnlyCrawler:
        class Storage:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        def __init__(self) -> None:
            self.storage = self.Storage()

    repo = TaskRepository(tmp_path / "close-failure.db")
    crawler = StorageOnlyCrawler()
    service = ApplicationService(repo, BrokenExecutor(repo), controller=BrokenController(), crawler=crawler)  # type: ignore[arg-type]
    assert service.close() is False
    assert crawler.storage.closed is False
    repo.close()
    crawler.storage.close()


def test_close_waits_for_an_inflight_query_before_closing_repository(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingRepository(TaskRepository):
        def get_task(self, task_id: str):
            entered.set()
            assert release.wait(5)
            return super().get_task(task_id)

    repo = BlockingRepository(tmp_path / "close-race.db")
    task = repo.create_task("https://example.com")
    service = ApplicationService(repo, FakeExecutor(repo))
    with ThreadPoolExecutor(max_workers=2) as pool:
        query = pool.submit(service.get_task, task.task_id)
        assert entered.wait(5)
        closing = pool.submit(service.close)
        assert not closing.done()
        release.set()
        assert query.result().task_id == task.task_id
        assert closing.result() is True


def test_close_is_retryable_and_keeps_dependencies_open_while_real_worker_is_blocked(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked_handler(_context, _task):
        entered.set()
        assert release.wait(5)

    class TrackingRepository(TaskRepository):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    repo = TrackingRepository(tmp_path / "retry-close.db")
    crawler = FakeCrawler()
    executor = BackgroundTaskExecutor(
        repo,
        {TaskStatus.PROBING: blocked_handler},
        max_workers=1,
        max_queue_size=2,
    )
    service = ApplicationService(repo, executor, crawler=crawler, close_timeout=0)
    view = service.create_crawl_task("https://example.com", {})
    assert entered.wait(5)

    assert service.close() is False
    assert repo.close_calls == 0
    assert crawler.closed is False
    assert repo.get_task(view.task_id).status is TaskStatus.PROBING
    with pytest.raises(ApplicationError, match="service_closing"):
        service.get_task(view.task_id)

    release.set()
    deadline = time.monotonic() + 5
    closed = service.close()
    while not closed and time.monotonic() < deadline:
        time.sleep(0.01)
        closed = service.close()
    assert closed is True
    assert repo.close_calls == 1
    assert crawler.closed is True
    assert service.close() is True


def test_close_drains_real_adaptive_worker_before_controller_cancels_new_handle(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    private_token = "private-verification-token"

    class Manager:
        def confirm(self, token: str, selector_overrides=None):
            return ConfigResolution(ResolutionKind.REGISTERED, config=cast(SiteConfig, object()))

        def cancel(self, token: str) -> bool:
            return True

    class BlockingAdaptive:
        def __init__(self) -> None:
            self.config_manager = Manager()
            self.cancelled: list[str] = []

        def resolve(self, _url: str, _task_key: str) -> AdaptiveResult:
            entered.set()
            assert release.wait(5)
            ticket = VerificationTicket(
                private_token,
                VerificationStatus.WAITING,
                "example.com",
                datetime.now(UTC) + timedelta(minutes=2),
                1,
            )
            return AdaptiveResult(ConfigResolution(ResolutionKind.WAITING_FOR_USER), ticket)

        def continue_verification(self, ticket):
            raise AssertionError("not used")

        def cancel(self, ticket) -> AdaptiveResult:
            token = ticket.token if isinstance(ticket, VerificationTicket) else ticket
            self.cancelled.append(token)
            return AdaptiveResult(ConfigResolution(ResolutionKind.CANCELLED))

        def retry_cleanup(self, ticket):
            raise AssertionError("not used")

        def expire_sweep(self) -> int:
            return 0

    class LeaseTrackingRepository(TaskRepository):
        lease_count_before_close: int | None = None

        def close(self) -> None:
            self.lease_count_before_close = int(
                self.connection.execute("SELECT COUNT(*) FROM task_interaction_leases").fetchone()[0]
            )
            super().close()

    repo = LeaseTrackingRepository(tmp_path / "adaptive-close.db")
    adaptive = BlockingAdaptive()
    controller = AdaptiveTaskController(repo, adaptive)
    executor = BackgroundTaskExecutor(
        repo,
        {TaskStatus.PROBING: controller.probe_handler},
        max_workers=1,
        max_queue_size=2,
    )
    service = ApplicationService(
        repo,
        executor,
        controller=controller,
        close_timeout=0,
    )
    view = service.create_crawl_task("https://example.com", {})
    assert entered.wait(5)

    assert service.close() is False
    assert adaptive.cancelled == []
    assert repo.connection.execute("SELECT 1").fetchone()[0] == 1
    release.set()
    deadline = time.monotonic() + 5
    closed = service.close()
    while not closed and time.monotonic() < deadline:
        time.sleep(0.01)
        closed = service.close()

    assert closed is True
    assert adaptive.cancelled == [private_token]
    assert "active_count=0" in repr(controller)
    assert repo.lease_count_before_close == 0
    assert private_token not in repr(view)


def test_close_retries_failed_controller_before_closing_downstream_resources(tmp_path: Path) -> None:
    class RetryController(FakeController):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        def close(self) -> bool:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("token=secret")
            return super().close()

    repo = TaskRepository(tmp_path / "controller-retry.db")
    executor = FakeExecutor(repo)
    controller = RetryController()
    crawler = FakeCrawler()
    service = ApplicationService(repo, executor, controller=controller, crawler=crawler)

    assert service.close() is False
    assert executor.closed is True
    assert crawler.closed is False
    assert repo.connection.execute("SELECT 1").fetchone()[0] == 1
    assert service.close() is True
    assert controller.close_calls == 2
    assert crawler.closed is True


@pytest.mark.parametrize("source", ["checkpoints", "interaction", "serialization"])
def test_created_task_view_dependency_failures_return_minimal_traceable_view(tmp_path: Path, source: str) -> None:
    class BrokenRepository(TaskRepository):
        def list_checkpoints(self, task_id: str):
            if source == "checkpoints":
                raise RuntimeError("token=secret C:\\private")
            return super().list_checkpoints(task_id)

    class BrokenSummary(FakeSummary):
        def to_safe_dict(self):
            if source == "serialization":
                raise RuntimeError("Authorization: Bearer secret")
            return super().to_safe_dict()

    class BrokenController(FakeController):
        def interaction(self, task_id: str):
            if source == "interaction":
                raise RuntimeError("password=secret /private")
            return BrokenSummary()

    repo = BrokenRepository(tmp_path / f"view-{source}.db")
    executor = FakeExecutor(repo)
    service = ApplicationService(repo, executor, controller=BrokenController())

    created = service.create_crawl_task("https://example.com", {})
    assert created.task_id
    assert created.status == "created"
    assert created.interaction is None
    assert created.checkpoint_count == 0
    assert created.checkpoint_version_total == 0
    assert dict(created.progress) == {}
    assert len(repo.list_tasks()) == 1
    assert executor.submitted == [created.task_id]
    assert "secret" not in repr(created).casefold()
    with pytest.raises(ApplicationError, match="task_view_failed"):
        service.get_task(created.task_id)
    service.close()


def test_batch_reports_created_and_submitted_when_full_view_dependencies_fail(tmp_path: Path) -> None:
    class BrokenRepository(TaskRepository):
        def list_checkpoints(self, task_id: str):
            raise RuntimeError("token=secret C:\\private")

    repo = BrokenRepository(tmp_path / "batch-minimal-view.db")
    executor = FakeExecutor(repo)
    service = ApplicationService(repo, executor)
    source = tmp_path / "urls.txt"
    source.write_text("https://one.test/book\n", encoding="utf-8")

    result = service.create_crawl_tasks_from_file(source)

    assert result["created"] == 1
    assert result["submitted"] == 1
    assert result["failed"] == 0
    assert result["not_started"] == 0
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["status"] == "created"
    assert "secret" not in repr(result).casefold()
    service.close()


def test_interaction_view_strictly_allowlists_kind_and_validates_fields(tmp_path: Path) -> None:
    class MalformedSummary(FakeSummary):
        def to_safe_dict(self):
            return {
                "kind": "https://evil.test/verification",
                "attempt": 10**30,
                "expires_at": "tomorrow at C:\\private",
                "safe_origin": "https://user:secret@evil.test/private?token=x",
                "verification_required": True,
                "confirmation_required": False,
                "cleanup_required": False,
            }

    class MalformedController(FakeController):
        def interaction(self, task_id: str):
            return MalformedSummary()

    repo = TaskRepository(tmp_path / "malformed-interaction.db")
    task = repo.create_task("https://example.com")
    service = ApplicationService(repo, FakeExecutor(repo), controller=MalformedController())
    view = service.get_task(task.task_id)
    assert view.interaction is not None
    assert view.interaction.kind == "unknown"
    assert view.interaction.attempt == 2_147_483_647
    assert view.interaction.expires_at is None
    assert view.interaction.safe_origin is None


def test_interaction_view_exposes_only_valid_safe_origin(tmp_path: Path) -> None:
    class AlwaysSummaryController(FakeController):
        def interaction(self, task_id: str):
            return FakeSummary()

    repo = TaskRepository(tmp_path / "safe-origin.db")
    service = ApplicationService(repo, FakeExecutor(repo), controller=AlwaysSummaryController())
    task = repo.create_task("https://private.test/path")

    view = service.get_task(task.task_id)

    assert view.interaction is not None
    assert view.interaction.safe_origin == "example.com"
    assert "token" not in str(view.interaction.to_safe_dict()).lower()
    assert "evil" not in repr(view)
    service.close()


@pytest.mark.parametrize(
    "unsafe_origin",
    [
        "https://user:secret@evil.test/",
        "https://evil.test/private?token=x",
        "https://evil..test/",
        "https://evil.test/#secret",
        "https://例子.测试/",
        "https://evil.test:0/",
        "https://[fe80::1%25eth0]/",
        "https://010.000.000.001/",
        "https://2130706433/",
        "https://evil.test./",
        "010.000.000.001",
        "2130706433",
    ],
)
def test_interaction_view_rejects_malicious_or_noncanonical_origin(
    tmp_path: Path, unsafe_origin: str
) -> None:
    class UnsafeSummary(FakeSummary):
        def to_safe_dict(self):
            return {**super().to_safe_dict(), "safe_origin": unsafe_origin}

    class UnsafeController(FakeController):
        def interaction(self, task_id: str):
            return UnsafeSummary()

    repo = TaskRepository(tmp_path / f"unsafe-origin-{len(unsafe_origin)}.db")
    task = repo.create_task("https://example.com")
    service = ApplicationService(repo, FakeExecutor(repo), controller=UnsafeController())
    assert service.get_task(task.task_id).interaction.safe_origin is None  # type: ignore[union-attr]
    service.close()


def test_interaction_view_accepts_only_timezone_aware_iso_expiration(tmp_path: Path) -> None:
    class TimestampSummary(FakeSummary):
        def to_safe_dict(self):
            return {**super().to_safe_dict(), "expires_at": "2026-07-12T05:00:00+08:00"}

    class TimestampController(FakeController):
        def interaction(self, task_id: str):
            return TimestampSummary()

    repo = TaskRepository(tmp_path / "timestamp.db")
    task = repo.create_task("https://example.com")
    service = ApplicationService(repo, FakeExecutor(repo), controller=TimestampController())
    assert service.get_task(task.task_id).interaction.expires_at == "2026-07-12T05:00:00+08:00"  # type: ignore[union-attr]
    service.close()


def test_close_supports_crawler_storage_fallback_after_upstreams_stop(tmp_path: Path) -> None:
    class StorageOnlyCrawler:
        class Storage:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        def __init__(self) -> None:
            self.storage = self.Storage()

    repo = TaskRepository(tmp_path / "storage-fallback.db")
    crawler = StorageOnlyCrawler()
    service = ApplicationService(repo, FakeExecutor(repo), crawler=crawler)  # type: ignore[arg-type]
    assert service.close() is True
    assert crawler.storage.closed is True


@pytest.mark.parametrize(
    "unsafe",
    [
        "output=C:\\Users\\name\\secret.txt",
        "output: \\\\server\\share\\secret.txt",
        "output(/home/name/private.txt)",
        "output=[/var/lib/novel/data.db]",
        "path=/opt/novel/data",
    ],
)
def test_report_redacts_absolute_paths_after_non_whitespace_boundaries(app, unsafe: str) -> None:
    service, _, _, _, crawler = app
    crawler.report = lambda _book_id: f"safe before\n{unsafe}\nsafe after"  # type: ignore[method-assign]
    assert service.book_report(1) == "safe before\n[redacted]\nsafe after"


@pytest.mark.parametrize(
    "safe",
    ["done/total: 2/3", "chapter 1/2 complete", "selector div/span", "ordinary prose"],
)
def test_report_does_not_redact_natural_relative_slashes(app, safe: str) -> None:
    service, _, _, _, crawler = app
    crawler.report = lambda _book_id: safe  # type: ignore[method-assign]
    assert service.book_report(1) == safe

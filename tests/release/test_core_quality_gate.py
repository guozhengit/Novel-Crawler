from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from novel_crawler.core.crawler import CrawlerService
from novel_crawler.core.fetcher import Fetcher, FetchOptions
from novel_crawler.core.models import Book, Chapter, ChapterStatus
from novel_crawler.core.validator import Validator

pytestmark = pytest.mark.release


class Adapter:
    fetch_options = object()
    requires_browser = False

    def get_book_info(self, html: str, url: str) -> Book:
        return Book("Book", url, "site", "Author")

    def get_chapter_list(self, html: str, url: str, **kwargs: object) -> list[Chapter]:
        return [Chapter(1, "One", url + "/1"), Chapter(2, "Two", url + "/2")]

    def parse_chapter(self, html: str, url: str) -> tuple[str, str]:
        return ("Parsed", "body") if html != "empty" else ("", "")

    def find_next_chapter(self, html: str, url: str) -> str | None:
        return url + "/next" if html == "first" else None


def service(tmp_path: Path) -> CrawlerService:
    value = CrawlerService.__new__(CrawlerService)
    value.ctx = SimpleNamespace(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    value.fetcher = MagicMock()
    value.storage = MagicMock()
    value.registry = MagicMock()
    return value


def test_crawl_resume_and_facade_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    crawler = service(tmp_path)
    adapter = Adapter()
    crawler.registry.find.return_value = adapter
    crawler.fetcher.fetch_text.return_value = "index"
    crawler.storage.upsert_book.return_value = 7
    crawler.storage.pending_chapters.return_value = [Chapter(1, "One", "https://x/1")]
    crawler.storage.get_book.return_value = Book("Book", "https://x", "site")
    crawler._download_batch = MagicMock()  # type: ignore[method-assign]
    crawler.export = MagicMock(return_value=tmp_path / "book.txt")  # type: ignore[method-assign]

    assert crawler.crawl("https://x", count=2, max_chapters=1) == 7
    crawler.resume(7, max_chapters=1)
    crawler.storage.upsert_chapters.assert_called_once()
    assert crawler._download_batch.call_count == 2

    exporter = MagicMock()
    monkeypatch.setattr("novel_crawler.core.crawler.get_exporter", lambda *args: exporter)
    for method in (crawler.export_txt, crawler.export_epub, crawler.export_markdown, crawler.export_jsonl):
        method(7)
    assert exporter.export.call_count == 4
    crawler.storage.progress.return_value = {"done": 1}
    crawler.storage.list_books.return_value = [{"id": 7}]
    crawler.storage.all_chapters.return_value = []
    assert crawler.progress(7) == {"done": 1}
    assert crawler.list_books() == [{"id": 7}]
    crawler.delete_book(7)
    crawler.validate(7)
    crawler.fix_titles(7, True)


def test_admin_batch_preview_stats_config_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    crawler = service(tmp_path)
    content = tmp_path / "chapter.txt"
    content.write_text("title\n\nbody text", encoding="utf-8")
    chapter = Chapter(1, "One", "https://x/1", status=ChapterStatus.DONE, content_path=content)
    crawler.storage.all_chapters.return_value = [chapter]
    crawler.storage.get_book.return_value = Book("Book", "https://x", "site", "Author")
    assert "body text" in crawler.preview_chapter(1, 1)
    assert "#9" in crawler.preview_chapter(1, 9)

    crawler.storage.list_books.return_value = [
        {"id": 1, "site": "a", "total": 4, "done": 3, "failed": 1, "pending": 0},
        {"id": 2, "site": "a", "total": 0, "done": 0, "failed": 0, "pending": 0},
    ]
    assert crawler.stats()["completion_rate"] == 75.0
    crawler.retry_failed = MagicMock()  # type: ignore[method-assign]
    assert crawler.retry_all_failed() == 1

    urls = tmp_path / "urls.txt"
    urls.write_text("# comment\nhttps://one\nhttps://bad\n", encoding="utf-8")
    crawler.crawl = MagicMock(side_effect=[11, RuntimeError("bad")])  # type: ignore[method-assign]
    assert crawler.crawl_batch(urls) == [11]

    valid = tmp_path / "site.json"
    valid.write_text('{"site":"s","domain":"x","book":{},"chapter":{"content_selector":"main"}}', encoding="utf-8")
    assert crawler.validate_config(valid)["valid"] is True
    invalid = tmp_path / "bad.txt"
    invalid.write_text("x", encoding="utf-8")
    assert crawler.validate_config(invalid)["valid"] is False

    crawler.progress = MagicMock(return_value={"done": 1})  # type: ignore[method-assign]
    crawler.validate = MagicMock(return_value=SimpleNamespace(to_text=lambda: "valid"))  # type: ignore[method-assign]
    crawler.logs = MagicMock(return_value=[{"created_at": "now", "level": "info", "chapter_index": 1, "message": "ok"}])  # type: ignore[method-assign]
    assert "valid" in crawler.report(1)


def test_chase_download_process_and_cache_paths(tmp_path: Path) -> None:
    crawler = service(tmp_path)
    adapter = Adapter()
    crawler.fetcher.fetch_text.return_value = "last"
    discovered = crawler._chase_chapters(adapter, "first", "https://x/1", 3, 2)
    assert len(discovered) == 2
    assert crawler.storage.mark_done.call_count == 2

    book = Book("Unsafe:/Book", "https://x", "site")
    chapter = Chapter(1, "One", "https://x/1")
    crawler.fetcher.fetch_text.return_value = "network"
    html, source = crawler._fetch_chapter_html(book, chapter, adapter)
    assert (html, source) == ("network", "net")
    assert crawler._fetch_chapter_html(book, chapter, adapter)[1] == "cache"

    crawler._process_chapter(1, adapter, chapter, "network", "net", 1, 1)
    crawler._process_chapter(1, adapter, chapter, "empty", "net", 1, 1)
    assert crawler.storage.mark_done.called
    assert crawler.storage.mark_failed.called

    crawler._fetch_chapter_html = MagicMock(side_effect=[("ok", "net"), RuntimeError("boom")])  # type: ignore[method-assign]
    crawler._download_batch(1, book, adapter, [Chapter(1, "A", "u1"), Chapter(2, "B", "u2")], 2)
    assert crawler.storage.mark_failed.called


def test_export_all_retry_and_dedup_paths(tmp_path: Path) -> None:
    crawler = service(tmp_path)
    crawler.list_books = MagicMock(return_value=[{"id": 1}, {"id": 2}])  # type: ignore[method-assign]
    crawler.export = MagicMock(side_effect=[tmp_path / "one.txt", RuntimeError("bad")])  # type: ignore[method-assign]
    assert crawler.export_all() == [tmp_path / "one.txt"]
    crawler.dedup(1)
    crawler.dedup(1, remove=True)
    crawler.resume = MagicMock()  # type: ignore[method-assign]
    crawler.retry_failed(1, export=False)
    crawler.storage.reset_failed.assert_called_once_with(1)


def test_validator_reports_structural_and_content_quality_issues(tmp_path: Path) -> None:
    short = tmp_path / "short.txt"
    short.write_text("title\n\nshort 한", encoding="utf-8")
    empty = tmp_path / "empty.txt"
    empty.write_text("title\n\n", encoding="utf-8")
    storage = MagicMock()
    storage.progress.return_value = {"done": 4, "failed": 1, "pending": 1}
    storage.all_chapters.return_value = [
        Chapter(1, "same", "u", status=ChapterStatus.DONE, content_path=short),
        Chapter(1, "same", "u", status=ChapterStatus.DONE, content_path=empty),
        Chapter(3, "same", "v", status=ChapterStatus.DONE, content_path=tmp_path / "missing"),
        Chapter(4, "same", "w", status=ChapterStatus.PENDING),
    ]

    report = Validator(storage).validate(9)

    codes = {issue.code for issue in report.issues}
    assert {"FAILED_CHAPTERS", "PENDING_CHAPTERS", "DUPLICATE_INDEX", "DUPLICATE_URL", "MISSING_INDEX"} <= codes
    assert {"EMPTY_CONTENT", "SHORT_CONTENT", "RESIDUAL_OBFUSCATION"} <= codes
    assert report.ok is False
    assert "issues:" in report.to_text()


def test_fetcher_retry_fallback_proxy_and_sleep_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    options = FetchOptions(
        retries=2,
        delay_min=0,
        delay_max=0,
        retry_backoff_min=0,
        retry_backoff_max=0,
        long_pause_min=0,
        long_pause_max=0,
        long_pause_every_min=1,
        long_pause_every_max=1,
    )
    fetcher = Fetcher(proxies={"http": "proxy"}, options=options)
    response = SimpleNamespace(status_code=503, content=b"")
    ok = SimpleNamespace(status_code=200, content=b"hello")
    fetcher.session.get = MagicMock(side_effect=[response, ok, ok])
    assert fetcher.fetch_bytes("https://x", referer="https://ref") == b"hello"
    assert fetcher.headers("https://ref")["Referer"] == "https://ref"
    assert fetcher.fetch_text("https://x") == "hello"
    monkeypatch.setattr("novel_crawler.core.fetcher.time.sleep", lambda value: None)
    fetcher.polite_sleep(1)
    fetcher.session.close()

    snapshot = SimpleNamespace(body=b"bytes", html="rendered")
    acquired = Fetcher(acquirer=SimpleNamespace(fetch=lambda url: snapshot))
    assert acquired.fetch_bytes("https://x") == b"bytes"
    assert acquired.fetch_text("https://x") == "rendered"
    acquired.session.close()

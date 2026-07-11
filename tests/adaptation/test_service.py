from __future__ import annotations

from datetime import UTC, datetime

import pytest

from novel_crawler.acquisition.http import AcquisitionError
from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.adaptation.service import ProbeService


def snapshot(url: str, html: str) -> PageSnapshot:
    body = html.encode()
    return PageSnapshot(url, url, 200, {}, "utf-8", html, body, "GET", (), datetime.now(UTC))


class FakeAcquirer:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def fetch_page(self, url: str) -> AcquiredPage:
        self.calls.append(url)
        return AcquiredPage(snapshot(url, self.pages[url]), url)


def test_probe_starting_at_chapter_fetches_index_and_two_adjacent_pages_only() -> None:
    pages = {
        "https://example.test/c1": '<h1>Chapter 1</h1><article><p>' + "a" * 80 + '</p><p>x</p></article><a rel="next" href="/c2">Next</a><a href="/book">Contents</a>',
        "https://example.test/book": '<h1>Book A</h1><div id="list"><a href="/c1">Chapter 1</a><a href="/c2">Chapter 2</a><a href="/c3">Chapter 3</a></div>',
        "https://example.test/c2": '<h1>Chapter 2</h1><article><p>' + "b" * 90 + '</p><p>y</p></article><a rel="next" href="/c3">Next</a>',
    }
    acquirer = FakeAcquirer(pages)
    pages["https://example.test/c1?token=secret#x"] = pages.pop("https://example.test/c1")
    result = ProbeService(acquirer=acquirer).probe("https://example.test/c1?token=secret#x")
    assert len(acquirer.calls) == 3
    assert acquirer.calls[0].endswith("?token=secret#x")
    assert acquirer.calls[1:] == ["https://example.test/book", "https://example.test/c2"]
    assert result.config_draft is not None
    assert "/c1" not in result.to_json() and "secret" not in result.to_json()


def test_probe_rejects_wrong_next_and_never_fetches_more_than_three_pages() -> None:
    pages = {
        "https://example.test/book": '<h1>Book A</h1><div id="list"><a href="/c1">Chapter 1</a><a href="/c2">Chapter 2</a><a href="/c3">Chapter 3</a></div>',
        "https://example.test/c1": '<h1>Chapter 1</h1><article><p>' + "a" * 80 + '</p></article><a rel="next" href="/c3">Next</a>',
        "https://example.test/c2": '<h1>Chapter 2</h1><article><p>' + "b" * 80 + '</p></article>',
    }
    acquirer = FakeAcquirer(pages)
    result = ProbeService(acquirer=acquirer).probe("https://example.test/book")
    assert not result.ok and "next_link_mismatch" in result.reason_ids
    assert len(acquirer.calls) == 3


def test_probe_budget_and_acquisition_failures_are_safe_rejections() -> None:
    with pytest.raises(ValueError):
        ProbeService(max_pages=2)
    pages = {"https://example.test/book": "x" * 201}
    result = ProbeService(acquirer=FakeAcquirer(pages), max_probe_bytes=200).probe("https://example.test/book")
    assert result.reason_ids == ("probe_invalid_content",)

    class Broken:
        def fetch_page(self, url: str) -> AcquiredPage:
            raise AcquisitionError("timeout", "https://example.test/", True)

    failed = ProbeService(acquirer=Broken()).probe("https://example.test/private?q=secret")
    assert failed.reason_ids == ("acquisition.timeout",)
    assert "private" not in failed.to_json() and "secret" not in failed.to_json()

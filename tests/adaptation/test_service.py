from __future__ import annotations

from datetime import UTC, datetime

from novel_crawler.acquisition.models import PageSnapshot
from novel_crawler.adaptation.service import ProbeService


def snapshot(url: str, html: str) -> PageSnapshot:
    body = html.encode()
    return PageSnapshot(url, url, 200, {}, "utf-8", html, body, "GET", (), datetime.now(UTC))


class FakeAcquirer:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def fetch(self, url: str) -> PageSnapshot:
        self.calls.append(url)
        return snapshot(url, self.pages[url])


def test_probe_starting_at_chapter_fetches_index_and_two_adjacent_pages_only() -> None:
    pages = {
        "https://example.test/c1": '<h1>Chapter 1</h1><article><p>' + "a" * 80 + '</p><p>x</p></article><a rel="next" href="/c2">Next</a><a href="/book">Contents</a>',
        "https://example.test/book": '<h1>Book A</h1><div id="list"><a href="/c1">Chapter 1</a><a href="/c2">Chapter 2</a><a href="/c3">Chapter 3</a></div>',
        "https://example.test/c2": '<h1>Chapter 2</h1><article><p>' + "b" * 90 + '</p><p>y</p></article><a rel="next" href="/c3">Next</a>',
    }
    acquirer = FakeAcquirer(pages)
    result = ProbeService(acquirer=acquirer).probe("https://example.test/c1?token=secret#x")
    assert len(acquirer.calls) == 3
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


from pathlib import Path

import pytest

from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.storage import ChapterContentConflict, Storage
from novel_crawler.sites.auto import AutoAdapter
from novel_crawler.sites.bqg import BqgAdapter
from novel_crawler.sites.router import AdapterRouter
from novel_crawler.sites.twbook import TwbookAdapter


def chapter_page(number: int, *, next_number: int | None) -> str:
    next_link = "" if next_number is None else f"<a href='/123/{next_number}.html'>下一章</a>"
    return (
        f"<h1 class='imgtext'>第{number}章</h1>"
        f"<div class='content'><p>第{number}章正文内容，长度足够用于适配测试。</p></div>"
        f"{next_link}"
    )


class StaticFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def fetch_text(self, url: str, referer: str | None = None) -> str:
        del referer
        return self.pages[url]


def test_twbook_follows_real_next_links_and_stops_at_last_chapter(tmp_path: Path) -> None:
    pages = {
        f"https://www.twbook.cc/123/{number}.html": chapter_page(
            number, next_number=number + 1 if number < 7 else None
        )
        for number in range(1, 8)
    }
    adapter = TwbookAdapter(tmp_path)
    adapter.set_fetcher(StaticFetcher(pages))  # type: ignore[arg-type]

    chapters = adapter.get_chapter_list(
        pages["https://www.twbook.cc/123/1.html"],
        "https://www.twbook.cc/123/1.html",
        start=1,
        count=20,
    )

    assert [chapter.index for chapter in chapters] == list(range(1, 8))
    assert chapters[-1].url == "https://www.twbook.cc/123/7.html"


def test_adapter_router_prefers_exact_dedicated_domain(tmp_path: Path) -> None:
    twbook = TwbookAdapter(tmp_path)
    router = AdapterRouter((twbook,))
    assert router.resolve("https://www.twbook.cc/123/1.html") is twbook
    assert isinstance(router.resolve("https://example.test/book"), AutoAdapter)
    assert not twbook.match("https://eviltwbook.cc/123/1.html")


def test_bqg_adapter_uses_current_series_host_for_api_and_chapters() -> None:
    url = "https://www.bqg107.xyz/#/book/1155/1.html"
    pages = {
        "https://www.bqg107.xyz/api/book?id=1155": '{"title":"书名","author":"作者"}',
        "https://www.bqg107.xyz/api/booklist?id=1155": '{"list":["第1章","第2章","第3章"]}',
    }
    adapter = BqgAdapter()
    adapter.set_fetcher(StaticFetcher(pages))  # type: ignore[arg-type]

    assert adapter.match(url)
    assert not adapter.match("https://www.bqg.example/#/book/1155/1.html")
    assert adapter.get_book_info("", url).title == "书名"

    chapters = adapter.get_chapter_list("", url, start=2, count=2)

    assert [chapter.title for chapter in chapters] == ["第2章", "第3章"]
    assert [chapter.url for chapter in chapters] == [
        "https://www.bqg107.xyz/#/book/1155/2.html",
        "https://www.bqg107.xyz/#/book/1155/3.html",
    ]


def test_storage_rejects_duplicate_content_across_chapters(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "crawler.db", tmp_path / "data")
    book_id = storage.upsert_book(Book("Book", "https://example.test/book", "fixture"))
    first = Chapter(1, "第1章", "https://example.test/book/1")
    second = Chapter(2, "第2章", "https://example.test/book/2")
    storage.upsert_chapters(book_id, [first, second])
    storage.mark_done(book_id, first, "第1章\n\n相同正文")
    with pytest.raises(ChapterContentConflict, match="chapter_content_duplicate"):
        storage.mark_done(
            book_id,
            second,
            "第1章\n\n相同正文",
            reject_duplicate_content=True,
        )
    storage.close()

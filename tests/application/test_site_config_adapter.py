from __future__ import annotations

import pytest

from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.application.site_adapter import SiteConfigAdapter


def config() -> SiteConfig:
    return SiteConfig.new(
        site="fixture",
        domain="example.test",
        url_patterns=["/books/**"],
        selectors={
            "clean": [".advert"],
            "book": {"title": "h1.book", "author": ".author", "chapter_list": "#list"},
            "chapter": {"chapter_title": "h1.chapter", "content": "article.content"},
        },
        request_policy={"timeout_seconds": 12, "max_retries": 3, "rate_limit_seconds": 0.25},
    )


def test_adapter_consumes_site_config_without_serializing_it() -> None:
    adapter = SiteConfigAdapter(config())
    assert adapter.match("https://example.test/books/1")
    assert not adapter.match("https://evil.test/books/1")
    assert vars(adapter.fetch_options) == {
        "timeout": 12,
        "retries": 4,
        "delay_min": 0.25,
        "delay_max": 0.25,
        "retry_backoff_min": 0.25,
        "retry_backoff_max": 0.25,
        "long_pause_min": 0.25,
        "long_pause_max": 0.25,
        "long_pause_every_min": 1,
        "long_pause_every_max": 1,
    }
    assert "example.test" not in repr(adapter)


def test_adapter_parses_book_list_and_joins_only_same_origin_urls() -> None:
    html = """
      <h1 class=book>Fixture Book</h1><div class=author>By Writer</div>
      <div id=list><a href='chapters/1.html'>One</a><a href='/books/chapters/2.html'>Two</a>
      <a href='https://evil.test/3'>Bad</a><a href='javascript:alert(1)'>Script</a></div>
    """
    adapter = SiteConfigAdapter(config())
    book = adapter.get_book_info(html, "https://example.test/books/index.html")
    chapters = adapter.get_chapter_list(html, book.url)
    assert (book.title, book.author, book.site) == ("Fixture Book", "Writer", "fixture")
    assert [(c.index, c.title, c.url) for c in chapters] == [
        (1, "One", "https://example.test/books/chapters/1.html"),
        (2, "Two", "https://example.test/books/chapters/2.html"),
    ]
    assert [c.title for c in adapter.get_chapter_list(html, book.url, start=2, count=1)] == ["Two"]


def test_adapter_cleans_chapter_content_and_rejects_empty_required_fields() -> None:
    adapter = SiteConfigAdapter(config())
    title, content = adapter.parse_chapter(
        "<h1 class=chapter>Chapter 1</h1><article class=content><script>secret</script>"
        "<p>Hello</p><form>credential</form><div class=advert>noise</div><p>World</p></article>",
        "https://example.test/books/chapters/1.html",
    )
    assert title == "Chapter 1"
    assert content == "Hello\nWorld"

    for html in ("<h1 class=book></h1><div id=list></div>", "<h1 class=chapter>x</h1>"):
        try:
            if "book" in html:
                adapter.get_book_info(html, "https://example.test/books/1")
            else:
                adapter.parse_chapter(html, "https://example.test/books/1")
        except ValueError as exc:
            assert str(exc) in {"book_title_missing", "chapter_content_missing"}
        else:
            raise AssertionError("missing required selector must fail")


def test_adapter_defensive_paths_remain_fail_closed() -> None:
    with pytest.raises(TypeError, match="SiteConfig"):
        SiteConfigAdapter(object())  # type: ignore[arg-type]
    adapter = SiteConfigAdapter(config())
    assert not adapter.match("not a url")
    assert not adapter.match("https://example.test/elsewhere")
    html = "<h1 class=book>Book</h1><div id=list><a>empty</a><a href='https://user:pass@example.test/books/1'>bad</a></div>"
    assert adapter.get_chapter_list(html, "https://example.test/books/index") == []


def test_chapter_selector_may_directly_select_links() -> None:
    raw = config().to_sensitive_dict()
    raw["selectors"]["book"]["chapter_list"] = "#list > a"  # type: ignore[index]
    adapter = SiteConfigAdapter(SiteConfig.from_dict(raw))
    chapters = adapter.get_chapter_list(
        "<h1 class=book>Book</h1><div id=list><a href='1'>One</a><a href='1'>Duplicate</a></div>",
        "https://example.test/books/index",
    )
    assert [chapter.title for chapter in chapters] == ["One"]

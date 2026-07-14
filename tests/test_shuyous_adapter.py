from __future__ import annotations

from novel_crawler.sites.shuyous import ShuyousAdapter

CATALOG_HTML = """
<html><head>
<meta property="og:novel:book_name" content="测试书" />
<meta property="og:novel:author" content="作者" />
</head><body>
<div class="chapterList"><div class="list"><ul>
<li><a href="/book/2579696-2.html">第2章 第二章</a></li>
<li><a href="/book/2579696-1.html">第1章 第一章</a></li>
<li><a href="/book/2579696-1-2.html">分页不应成为新章节</a></li>
</ul></div></div>
</body></html>
"""


PAGE_ONE = """
<html><body>
<h1 class="title">第1章 第一章</h1>
<div id="content"><p>第一页正文。</p><p>请记住本书首发域名：http://www.shuyous.com</p></div>
<a class="pageDown" href="/book/2579696-1-2.html">下一页</a>
</body></html>
"""


PAGE_TWO = """
<html><body>
<h1 class="title">第1章 第一章</h1>
<div id="content"><p>第二页正文。</p></div>
<a class="pageDown" href="/book/2579696-2.html">下一章</a>
</body></html>
"""


class FakeFetcher:
    def fetch_text(self, url: str, referer: str | None = None) -> str:
        assert url == "https://www.shuyous.com/book/2579696-1-2.html"
        assert referer == "https://www.shuyous.com/book/2579696-1.html"
        return PAGE_TWO


def test_shuyous_adapter_extracts_catalog_without_page_links() -> None:
    adapter = ShuyousAdapter()

    book = adapter.get_book_info(CATALOG_HTML, "https://www.shuyous.com/book/2579696.html")
    chapters = adapter.get_chapter_list(
        CATALOG_HTML,
        "https://www.shuyous.com/book/2579696.html",
        start=1,
        count=2,
    )

    assert book.title == "测试书"
    assert book.author == "作者"
    assert [(chapter.index, chapter.title) for chapter in chapters] == [
        (1, "第1章 第一章"),
        (2, "第2章 第二章"),
    ]


def test_shuyous_adapter_merges_chapter_pages() -> None:
    adapter = ShuyousAdapter()
    adapter.set_fetcher(FakeFetcher())

    title, body = adapter.parse_chapter(PAGE_ONE, "https://www.shuyous.com/book/2579696-1.html")

    assert title == "第1章 第一章"
    assert body == "第一页正文。\n第二页正文。"

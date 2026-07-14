from __future__ import annotations

import json
from datetime import UTC, datetime

from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.exploration import explore_site, propose_config_from_report, write_report

CATALOG = """
<html><head>
<meta property="og:novel:book_name" content="测试书" />
<meta property="og:novel:author" content="作者" />
</head><body>
<div class="chapterList"><div class="list"><ul>
<li><a href="/book/1-1.html">第1章 第一章</a></li>
<li><a href="/book/1-2.html">第2章 第二章</a></li>
<li><a href="/book/1-3.html">第3章 第三章</a></li>
</ul></div></div>
</body></html>
"""


CHAPTER_ONE = """
<html><body>
<h1 class="title">第1章 第一章</h1>
<div id="content"><p>第一段正文内容，足够长以通过内容检测，并且模拟真实章节中的连续叙述文字。</p><p>第二段正文内容，继续提供稳定的段落结构。</p></div>
</body></html>
"""


CHAPTER_TWO = """
<html><body>
<h1 class="title">第2章 第二章</h1>
<div id="content"><p>另一章第一段正文内容，足够长以通过内容检测，并且模拟真实章节中的连续叙述文字。</p><p>另一章第二段正文内容，继续提供稳定的段落结构。</p></div>
</body></html>
"""


class FakeAcquirer:
    pages = {
        "https://example.test/book/1.html": CATALOG,
        "https://example.test/book/1-1.html": CHAPTER_ONE,
        "https://example.test/book/1-2.html": CHAPTER_TWO,
    }

    def fetch_page(self, url: str, **_kwargs):
        html = self.pages[url]
        snapshot = PageSnapshot(
            requested_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": "text/html"},
            encoding="utf-8",
            html=html,
            body=html.encode("utf-8"),
            method="GET",
            redirects=(),
            retrieved_at=datetime.now(UTC),
        )
        return AcquiredPage(snapshot, url)


def test_explore_site_generates_report_and_generic_config(tmp_path) -> None:
    report = explore_site("https://example.test/book/1.html", sample=2, acquirer=FakeAcquirer())

    assert report["domain"] == "example.test"
    assert report["sample_count"] == 2
    assert report["requires_dedicated_adapter"] is False
    config = report["proposed_config"]
    assert config["book"]["title_selector"] == "meta[property='og:novel:book_name']"
    assert config["book"]["chapter_list_selector"].endswith(" a")
    assert config["chapter"]["content_selector"] == "#content"

    report_path = write_report(report, tmp_path / "report.json")
    output = propose_config_from_report(report_path, tmp_path / "site.json")
    assert json.loads(output.read_text(encoding="utf-8"))["site"] == "example"

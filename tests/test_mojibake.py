from pathlib import Path

import pytest

from novel_crawler.core.fetcher import Fetcher, FetchOptions
from novel_crawler.sites.auto import AutoAdapter
from novel_crawler.sites.detector import CHAPTER_TEXT_RE, SiteInspection

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml"}
KNOWN_MOJIBAKE = ("閫", "绔", "璇", "娴忚", "锛", "鈫", "闁")  # mojibake-fixture


@pytest.mark.parametrize("title", ["第一章", "第 12 章", "Chapter 9"])
def test_chapter_pattern_matches_readable_titles(title):
    assert CHAPTER_TEXT_RE.search(title)


def test_auto_adapter_removes_promotional_phrases_without_dropping_story_text(monkeypatch):
    monkeypatch.setattr(
        "novel_crawler.sites.auto.inspect_html",
        lambda html, url: SiteInspection(content_selector="#content"),
    )
    html = """
    <h1>第一章</h1>
    <div id="content">
      <p>正文开始。请收藏本站，最新网址：www.example.test</p>
      <p>手机阅读更方便，加入书签。正文继续。</p>
    </div>
    """

    _, content = AutoAdapter().parse_chapter(html, "https://example.test/1")

    assert "正文开始" in content
    assert "正文继续" in content
    assert all(phrase not in content for phrase in ("请收藏本站", "最新网址", "手机阅读", "加入书签"))


def test_fetcher_empty_content_error_is_readable_chinese(monkeypatch):
    fetcher = Fetcher(options=FetchOptions(retries=1))
    monkeypatch.setattr(fetcher, "fetch_bytes", lambda url, referer=None: b"")

    with pytest.raises(RuntimeError, match="抓取内容为空"):
        fetcher.fetch_text("https://example.test/book")


def test_fetcher_failure_error_is_readable_chinese(monkeypatch):
    fetcher = Fetcher(options=FetchOptions(retries=1, retry_backoff_min=0, retry_backoff_max=0))
    monkeypatch.setattr(fetcher.session, "get", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))

    with pytest.raises(RuntimeError, match="抓取失败"):
        fetcher.fetch_bytes("https://example.test/book")


def test_user_facing_source_has_no_known_mojibake():
    findings = []
    for path in ROOT.rglob("*"):
        if path.suffix not in TEXT_SUFFIXES or any(
            part in {".git", ".pytest_cache", "__pycache__"} for part in path.relative_to(ROOT).parts
        ):
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "mojibake-fixture" in line or ("assert" in line and "not in" in line):
                continue
            outside_code = "".join(line.split("`")[::2])
            for fragment in KNOWN_MOJIBAKE:
                if fragment in outside_code:
                    findings.append(f"{path.relative_to(ROOT)}:{line_number}: {fragment}")
    assert not findings, "发现疑似乱码：\n" + "\n".join(findings)

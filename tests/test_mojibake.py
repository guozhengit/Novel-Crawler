import subprocess
from pathlib import Path

import pytest

from novel_crawler.core.fetcher import Fetcher, FetchOptions
from novel_crawler.sites.auto import AutoAdapter
from novel_crawler.sites.detector import CHAPTER_TEXT_RE, SiteInspection

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml"}
PRODUCT_PREFIXES = ("novel_crawler/", "docs/")
INTERNAL_PREFIXES = ("docs/superpowers/",)
KNOWN_MOJIBAKE = (  # mojibake-fixture
    "閫氱敤",
    "绔犺妭",
    "璇峰厛",
    "娴忚鍣�",
    "锛�",
    "鈫�",
    "闁",
)


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

    assert content == "正文开始。\n正文继续。"


def test_auto_adapter_stops_url_cleanup_before_following_story(monkeypatch):
    monkeypatch.setattr(
        "novel_crawler.sites.auto.inspect_html",
        lambda html, url: SiteInspection(content_selector="#content"),
    )
    html = '<div id="content"><p>正文一。最新网址：www.example.test。正文二。</p></div>'

    _, content = AutoAdapter().parse_chapter(html, "https://example.test/1")

    assert content == "正文一。正文二。"


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


def _tracked_product_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = []
    for raw_path in result.stdout.decode("utf-8").split("\0"):
        if not raw_path:
            continue
        normalized = raw_path.replace("\\", "/")
        if normalized == "README.md" or (
            normalized.startswith(PRODUCT_PREFIXES) and not normalized.startswith(INTERNAL_PREFIXES)
        ):
            path = ROOT / raw_path
            if path.suffix in TEXT_SUFFIXES:
                paths.append(path)
    return sorted(paths)


def _known_mojibake_in(text: str) -> list[str]:
    return [fragment for fragment in KNOWN_MOJIBAKE if fragment in text]


def test_mojibake_tokens_do_not_reject_legitimate_chinese():
    assert _known_mojibake_in("璇玑在城门等候，小说章节内容正常。") == []


def test_guard_scans_only_tracked_product_sources():
    relative_paths = [path.relative_to(ROOT).as_posix() for path in _tracked_product_paths()]

    assert "README.md" in relative_paths
    assert "novel_crawler/core/fetcher.py" in relative_paths
    assert all(not path.startswith(("tests/", "dist/", "build/", "docs/superpowers/")) for path in relative_paths)


def test_user_facing_source_has_no_known_mojibake():
    findings = []
    for path in _tracked_product_paths():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "# mojibake-fixture" in line:
                continue
            outside_code = "".join(line.split("`")[::2])
            for fragment in _known_mojibake_in(outside_code):
                findings.append(f"{path.relative_to(ROOT)}:{line_number}: {fragment}")
    assert not findings, "发现疑似乱码：\n" + "\n".join(findings)

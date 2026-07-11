from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.acquisition.models import PageSnapshot
from novel_crawler.adaptation.extractor import CandidateExtractor
from novel_crawler.adaptation.models import Candidate, Evidence, ExtractionResult, FieldKind


def snapshot(html: str) -> PageSnapshot:
    return PageSnapshot(
        "https://reader.example/book/7?token=secret",
        "https://reader.example/book/7?token=secret",
        200,
        {},
        "utf-8",
        html,
        html.encode(),
        "GET",
        (),
        datetime.now(UTC),
    )


def test_models_are_immutable_validate_confidence_and_query_by_field() -> None:
    evidence = Evidence("title.h1", 0.8, "text_len=4")
    candidate = Candidate(FieldKind.TITLE, "h1", "星海纪元", 0.8, 0.8, (evidence,), {"rank": 1})
    result = ExtractionResult((candidate,))

    assert result.for_field(FieldKind.TITLE) == (candidate,)
    assert result.for_field(FieldKind.AUTHOR) == ()
    with pytest.raises(FrozenInstanceError):
        candidate.confidence = 0.1  # type: ignore[misc]
    with pytest.raises(TypeError):
        candidate.metadata["rank"] = 2  # type: ignore[index]
    with pytest.raises(ValueError, match="confidence"):
        Candidate(FieldKind.TITLE, "h1", "x", 1, 1.01, (), {})


def test_evidence_detail_only_accepts_short_structured_features() -> None:
    assert Evidence("content.length", 0.4, "text_len=120;paragraphs=3").detail == "text_len=120;paragraphs=3"
    for unsafe in ("https://private.example/read?id=7", "the complete secret narrative", "text_len=1\nsecret"):
        with pytest.raises(ValueError, match="structured"):
            Evidence("unsafe", 0.1, unsafe)


def test_extracts_multiple_index_candidates_and_ignores_comment_recommendation_links() -> None:
    html = """<html><head><title>星海纪元 - 章节目录</title></head><body>
    <main><h1>星海纪元</h1><p class="writer">作者：林舟</p>
      <section class="catalog"><a href="/c/1">第一章 启程</a><a href="/c/2">第二章 风暴</a>
      <a href="/c/3">第三章 星门</a><a href="/c/4">第四章 归途</a></section>
      <ul class="chapters"><li><a href="1.html">第1章</a></li><li><a href="2.html">第2章</a></li>
      <li><a href="3.html">第3章</a></li></ul></main>
    <section class="comments"><a href="/spam/1">第一章 真好看</a><a href="/spam/2">第二章 评论</a></section>
    <aside class="recommendations"><a href="/ad/1">第一章 推荐</a><a href="/ad/2">第二章 推荐</a></aside>
    </body></html>"""
    result = CandidateExtractor().extract(snapshot(html), PageKind.BOOK_INDEX)

    assert {item.value_preview for item in result.for_field(FieldKind.TITLE)} >= {"星海纪元"}
    assert result.for_field(FieldKind.AUTHOR)[0].value_preview == "林舟"
    lists = result.for_field(FieldKind.CHAPTER_LIST)
    assert len(lists) >= 2
    assert all("comments" not in item.selector and "recommend" not in item.selector for item in lists)
    assert all(item.metadata["link_count"] >= 3 for item in lists)


def test_extracts_english_chapter_candidates_navigation_content_and_noise() -> None:
    html = """<html><head><title>Chapter 12: The Gate | My Novel</title></head><body>
    <header><h1>Chapter 12: The Gate</h1><span>By Ada Stone</span></header>
    <div class="reader-body"><p>The rain crossed the old city and the travelers kept walking into the night.</p>
    <p>A second substantial paragraph makes this the likely narrative body for the chapter.</p></div>
    <article class="alternate-content"><p>Another plausible body is deliberately retained as a candidate.</p>
    <p>It contains enough continuous prose to compete without being selected prematurely.</p></article>
    <nav><a rel="prev" href="/chapter/11?session=private">Previous Chapter</a>
    <a href="/novel">Table of Contents</a><a rel="next" href="/chapter/13">Next Chapter</a></nav>
    <div id="comments">Reader comments and discussion</div><div class="ad-banner">Buy now sponsored offer</div>
    </body></html>"""
    result = CandidateExtractor().extract(snapshot(html), PageKind.CHAPTER)

    assert len(result.for_field(FieldKind.CONTENT)) >= 2
    assert result.for_field(FieldKind.CHAPTER_TITLE)[0].value_preview == "Chapter 12: The Gate"
    assert result.for_field(FieldKind.AUTHOR)[0].value_preview == "Ada Stone"
    assert result.for_field(FieldKind.PREV_LINK)
    assert result.for_field(FieldKind.NEXT_LINK)
    assert result.for_field(FieldKind.INDEX_LINK)
    noise = result.for_field(FieldKind.CLEAN_SELECTOR)
    assert {item.selector for item in noise} >= {"#comments", ".ad-banner"}


def test_previews_and_evidence_do_not_leak_body_or_urls() -> None:
    secret = "SECRET-NARRATIVE-" + "x" * 160
    html = f"""<h1>第九章 密令</h1><div id="content">{secret}</div>
    <a rel="next" href="https://private.example/chapter/10?token=hunter2">下一章</a>"""
    result = CandidateExtractor().extract(snapshot(html), PageKind.CHAPTER)

    candidates = result.candidates
    assert all(len(item.value_preview) <= 80 for item in candidates)
    assert all("hunter2" not in item.value_preview for item in candidates)
    assert all("private.example" not in item.value_preview for item in candidates)
    for item in candidates:
        for evidence in item.evidence:
            assert len(evidence.detail) <= 80
            assert secret not in evidence.detail
            assert "http" not in evidence.detail
            assert "hunter2" not in evidence.detail

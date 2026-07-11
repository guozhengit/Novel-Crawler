from __future__ import annotations

import re
from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.acquisition.models import PageSnapshot
from novel_crawler.adaptation.extractor import CandidateExtractor, ExtractionRule, ExtractorConfig
from novel_crawler.adaptation.models import Candidate, Evidence, ExtractionResult, FieldKind

FIXTURES = Path(__file__).parent / "fixtures"


def snapshot(html: str) -> PageSnapshot:
    return PageSnapshot("https://reader.example/book/7?token=secret", "https://reader.example/book/7?token=secret", 200, {}, "utf-8", html, html.encode(), "GET", (), datetime.now(UTC))


def test_models_are_deeply_immutable_private_and_validated() -> None:
    evidence = Evidence("title.h1", 0.8, "text_len=4")
    preview = "length_bucket=1-16"
    candidate = Candidate(FieldKind.TITLE, "#private-selector", preview, 0.8, 0.8, (evidence,), {"rank": 1})
    result = ExtractionResult((candidate,), "builtin", "v1")
    assert result.for_field(FieldKind.TITLE) == (candidate,)
    with pytest.raises(FrozenInstanceError):
        candidate.confidence = 0.1  # type: ignore[misc]
    with pytest.raises(TypeError):
        candidate.metadata["rank"] = 2  # type: ignore[index]
    assert "#private-selector" not in repr(candidate) and "#private-selector" not in repr(result)
    with pytest.raises(TypeError):
        asdict(candidate)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        asdict(result)  # type: ignore[arg-type]
    bad = (
        dict(selector="https://x.test?a=secret", value_preview=preview, raw_score=1.0, confidence=0.5),
        dict(selector="a[href*='token=secret']", value_preview=preview, raw_score=1.0, confidence=0.5),
        dict(selector="h1", value_preview="Secret title", raw_score=1.0, confidence=0.5),
        dict(selector="h1", value_preview=preview, raw_score=float("nan"), confidence=0.5),
        dict(selector="h1", value_preview=preview, raw_score=1.0, confidence=1.01),
    )
    for values in bad:
        with pytest.raises(ValueError):
            Candidate(FieldKind.CONTENT, evidence=(evidence,), metadata={}, **values)
    with pytest.raises(ValueError):
        Evidence("bad rule", float("inf"), "count=1")


def test_evidence_detail_only_accepts_short_structured_features() -> None:
    assert Evidence("content.length", 0.4, "text_len=120;paragraphs=3").detail
    for unsafe in ("https://private.example/read?id=7", "the complete secret narrative", "text_len=1\nsecret"):
        with pytest.raises(ValueError, match="structured"):
            Evidence("unsafe", 0.1, unsafe)


def test_index_candidates_are_anchor_selectors_and_noise_is_excluded() -> None:
    html = """<main><h1>星海纪元</h1><p class="writer">作者：林舟</p>
    <section class="catalog"><a href="/c/1">第一章</a><a href="/c/2">第二章</a><a href="/c/3">第三章</a></section>
    <ul class="chapters"><li><a href="1">Chapter 1</a></li><li><a href="2">Chapter 2</a></li><li><a href="3">Chapter 3</a></li></ul>
    <section class="comments"><a href="/s/1">第一章</a><a href="/s/2">第二章</a><a href="/s/3">第三章</a><a href="/s/4">第四章</a></section></main>"""
    result = CandidateExtractor().extract(snapshot(html), PageKind.BOOK_INDEX)
    soup = BeautifulSoup(html, "lxml")
    assert result.for_field(FieldKind.TITLE) and result.for_field(FieldKind.AUTHOR)
    lists = result.for_field(FieldKind.CHAPTER_LIST)
    assert len(lists) == 2
    assert all(item.metadata["link_count"] == 3 for item in lists)
    assert all(soup.select(item.selector) and all(node.name == "a" for node in soup.select(item.selector)) for item in lists)
    assert all("comments" not in item.selector for item in lists)


def test_chapter_extracts_multiple_content_navigation_and_noise_candidates() -> None:
    html = """<h1>Chapter 12: The Gate</h1><span>Written by Ada Stone</span>
    <div class="reader-body"><p>The rain crossed the old city and travelers walked into the night.</p><p>A second substantial paragraph makes the likely narrative body.</p></div>
    <article class="alternate-content"><p>Another plausible body is deliberately retained as a candidate.</p><p>Enough continuous prose competes without premature selection.</p></article>
    <nav><a rel="prev" href="/11?secret=x">Previous Chapter</a><a href="/novel">Table of Contents</a><a rel="next" href="/13">Next Chapter</a></nav>
    <div id="comments">discussion</div><div class="ad-banner">Buy now</div>"""
    result = CandidateExtractor().extract(snapshot(html), PageKind.CHAPTER)
    assert len(result.for_field(FieldKind.CONTENT)) >= 2
    for field in (FieldKind.CHAPTER_TITLE, FieldKind.AUTHOR, FieldKind.PREV_LINK, FieldKind.NEXT_LINK, FieldKind.INDEX_LINK):
        assert result.for_field(field)
    assert {item.selector for item in result.for_field(FieldKind.CLEAN_SELECTOR)} >= {"#comments", ".ad-banner"}


def test_outputs_never_leak_body_title_author_or_urls() -> None:
    secrets = ("SECRET-TITLE", "SECRET-AUTHOR", "SECRET-NARRATIVE-" + "x" * 160, "hunter2")
    html = f"<h1 id='{secrets[0]}'>Chapter 9 {secrets[0]}</h1><p id='{secrets[1]}'>By {secrets[1]}</p><div id='content'>{secrets[2]}</div><a rel='next' href='https://private.example/10?token={secrets[3]}'>Next Chapter</a>"
    result = CandidateExtractor().extract(snapshot(html), PageKind.CHAPTER)
    serialized = repr(result)
    assert all(secret not in serialized for secret in secrets)
    assert "private.example" not in serialized
    assert all(len(item.value_preview) <= 80 for item in result)
    assert "sha" not in serialized.casefold() and "hash" not in serialized.casefold()


def test_selectors_are_unique_with_duplicate_ids_classes_and_special_characters() -> None:
    html = """<main><div class="card"><div id="body:main" class="reader body"><p>First substantial narrative paragraph with enough words.</p><p>Second paragraph provides a distinct body candidate.</p></div></div>
    <div class="card"><div id="body:main" class="reader body"><p>Another substantial narrative paragraph with enough words.</p><p>Second paragraph provides another distinct body candidate.</p></div></div></main>"""
    soup = BeautifulSoup(html, "lxml")
    contents = CandidateExtractor().extract(snapshot(html), PageKind.CHAPTER).for_field(FieldKind.CONTENT)
    assert len(contents) >= 2
    assert len({item.selector for item in contents}) == len(contents)
    assert all(len(soup.select(item.selector)) == 1 for item in contents)


def test_nested_noise_is_removed_before_content_scoring() -> None:
    clean = "A modest real chapter paragraph."
    html = f"<article id='content'><p>{clean}</p><p>It continues.</p><div class='comments'>{'BUY SECRET TOKEN ' * 100}</div></article>"
    item = CandidateExtractor().extract(snapshot(html), PageKind.CHAPTER).for_field(FieldKind.CONTENT)[0]
    assert item.metadata["length_bucket"] == "17-64"


def test_navigation_uses_exact_rel_and_anchored_text() -> None:
    html = """<nav><a rel="nofollow prev" href="/1">Anything</a><a rel="next" href="/2">Anything</a><a href="/3">Previous Chapter</a><a href="/4">Next chapter</a>
    <a href="/x">Preview</a><a href="/x">Nextdoor</a><a href="/x">reindex</a><a rel="nofollow" href="/x">Next offer</a></nav>"""
    result = CandidateExtractor().extract(snapshot(html), PageKind.CHAPTER)
    assert len(result.for_field(FieldKind.PREV_LINK)) == 2
    assert len(result.for_field(FieldKind.NEXT_LINK)) == 2
    assert not result.for_field(FieldKind.INDEX_LINK)


@pytest.mark.parametrize("title", ["Prologue", "Epilogue", "Part II", "Book 3", "Chapter 1.5", "序章", "楔子", "番外 2", "第1.5章"])
def test_configurable_bilingual_chapter_titles(title: str) -> None:
    assert CandidateExtractor().extract(snapshot(f"<h1>{title}</h1>"), PageKind.CHAPTER).for_field(FieldKind.CHAPTER_TITLE)


@pytest.mark.parametrize("markup", ["<p>Written by Ursula</p>", "<p>Author: Ursula</p>", "<b>作者</b><span>余华</span>"])
def test_extended_author_labels(markup: str) -> None:
    assert CandidateExtractor().extract(snapshot(markup), PageKind.BOOK_INDEX).for_field(FieldKind.AUTHOR)


def test_config_can_limit_fields_and_reports_provenance() -> None:
    config = ExtractorConfig(enabled_fields=frozenset({FieldKind.TITLE}), version="review-v2")
    extractor = CandidateExtractor(config=config)
    assert isinstance(extractor.rules[0], ExtractionRule)
    result = extractor.extract(snapshot("<h1>Novel</h1><p>By Secret</p>"), PageKind.BOOK_INDEX)
    assert {item.field for item in result} == {FieldKind.TITLE}
    assert result.version == "review-v2" and result.provenance


def test_realistic_mixed_catalog_selects_only_chapter_anchors() -> None:
    html = (FIXTURES / "catalog_mixed.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")
    result = CandidateExtractor().extract(snapshot(html), PageKind.BOOK_INDEX)
    candidate = result.for_field(FieldKind.CHAPTER_LIST)[0]
    selected = soup.select(candidate.selector)
    assert [node.get_text(" ", strip=True) for node in selected] == ["Chapter One", "Chapter Twenty-One", "Interlude", "Chapter One Thousand"]
    assert all("Home" not in node.text and "Latest" not in node.text and "Next" not in node.text for node in selected)
    assert candidate.metadata["container_selector"]
    assert candidate.metadata["link_text_rule"] == "chapter_title.v2"


def test_large_mixed_catalog_uses_safe_href_structure_not_exact_selector_list() -> None:
    chapters = "".join(f'<a href="/read/{index}">Chapter {index}</a>' for index in range(1, 14))
    html = f'<section class="catalog-list"><a href="/">Home</a>{chapters}<a href="/catalog/2">Next</a></section>'
    soup = BeautifulSoup(html, "lxml")
    candidate = CandidateExtractor().extract(snapshot(html), PageKind.BOOK_INDEX).for_field(FieldKind.CHAPTER_LIST)[0]
    assert candidate.metadata["selector_strategy"] == "safe_href"
    assert len(soup.select(candidate.selector)) == 13
    assert "," not in candidate.selector


def test_realistic_chapter_fixture_removes_nested_noise() -> None:
    html = (FIXTURES / "chapter_nested_noise.html").read_text(encoding="utf-8")
    result = CandidateExtractor().extract(snapshot(html), PageKind.CHAPTER)
    assert result.for_field(FieldKind.CHAPTER_TITLE)
    content = result.for_field(FieldKind.CONTENT)[0]
    assert content.metadata["paragraph_count"] == 2


@pytest.mark.parametrize("title", ["Chapter One", "Chapter Twenty-One", "Chapter Nine Hundred Ninety-Nine", "Chapter One Thousand", "Foreword", "Afterword", "Interlude"])
def test_english_number_words_and_section_titles(title: str) -> None:
    assert CandidateExtractor().extract(snapshot(f"<h1>{title}</h1>"), PageKind.CHAPTER).for_field(FieldKind.CHAPTER_TITLE)


def test_opaque_pii_and_dynamic_attributes_never_enter_selectors() -> None:
    opaque = "user@example.com"
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    dynamic = "css-98765432109876543210"
    html = f"<main><article id='{opaque}' class='{uuid} {dynamic}'><p>{'Narrative words. ' * 8}</p></article></main>"
    candidates = CandidateExtractor().extract(snapshot(html), PageKind.CHAPTER).for_field(FieldKind.CONTENT)
    assert candidates
    assert all(opaque not in item.selector and uuid not in item.selector and dynamic not in item.selector for item in candidates)


def test_config_normalizes_mutable_inputs_and_cannot_be_changed_after_creation() -> None:
    patterns = [r"Chapter\s+\d+"]
    fields = {FieldKind.TITLE}
    config = ExtractorConfig(chapter_title_patterns=patterns, enabled_fields=fields)  # type: ignore[arg-type]
    patterns.append("Secret")
    fields.add(FieldKind.AUTHOR)
    assert config.chapter_title_patterns == (r"Chapter\s+\d+",)
    assert config.enabled_fields == frozenset({FieldKind.TITLE})


def test_invalid_config_rules_and_deep_result_values_are_rejected() -> None:
    for values in (
        {"min_chapter_links": 0},
        {"min_content_chars": 0},
        {"chapter_title_patterns": ()},
        {"author_patterns": ()},
        {"enabled_fields": frozenset()},
    ):
        with pytest.raises(ValueError):
            ExtractorConfig(**values)  # type: ignore[arg-type]
    with pytest.raises(re.error):
        ExtractorConfig(noise_pattern="[")
    with pytest.raises(TypeError):
        CandidateExtractor(rules=[object()])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        ExtractionResult(("bad",), "builtin", "v1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ExtractionResult((), "unsafe provenance", "v1")


def test_candidate_rejects_invalid_nested_evidence_and_metadata() -> None:
    evidence = Evidence("safe.rule", 1.0, "count=1")
    base = dict(field=FieldKind.TITLE, selector="h1", value_preview="count=1", raw_score=1.0, confidence=1.0)
    for extra in (
        {"evidence": ("bad",), "metadata": {}},
        {"evidence": (evidence,), "metadata": {"bad key": 1}},
        {"evidence": (evidence,), "metadata": {"value": float("inf")}},
        {"evidence": (evidence,), "metadata": {"value": "raw secret value"}},
    ):
        with pytest.raises((TypeError, ValueError)):
            Candidate(**base, **extra)  # type: ignore[arg-type]

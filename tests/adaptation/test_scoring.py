from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.acquisition.models import PageSnapshot
from novel_crawler.adaptation.extractor import CandidateExtractor
from novel_crawler.adaptation.models import Candidate, Evidence, FieldKind
from novel_crawler.adaptation.scoring import (
    CandidateIdentity,
    CandidateScorer,
    ScoreComponent,
    ScoredCandidate,
    ScoringConfig,
    ScoringContext,
    ScoringRule,
)

FIXTURES = Path(__file__).parent / "fixtures"


def snapshot(html: str, url: str = "https://reader.example/book/1") -> PageSnapshot:
    return PageSnapshot(url, url, 200, {}, "utf-8", html, html.encode(), "GET", (), datetime.now(UTC))


def item(field: FieldKind, selector: str, *, metadata: dict[str, str | int | float | bool] | None = None, raw: float = 999) -> Candidate:
    return Candidate(field, selector, "count=1", raw, 0.99, (Evidence("extract.safe", 1, "count=1"),), metadata or {})


def test_context_requires_real_snapshot_and_results_do_not_retain_derived_content() -> None:
    page = snapshot("<h1>Private Book Title</h1>")
    context = ScoringContext(PageKind.BOOK_INDEX, page)
    scored = CandidateScorer().score(item(FieldKind.TITLE, "h1"), context)
    assert scored.score > 0 and scored.confidence == scored.score
    assert "Private Book Title" not in repr(scored)
    assert "h1" not in repr(scored)
    with pytest.raises(TypeError):
        asdict(scored)  # type: ignore[arg-type]
    assert "Private Book Title" not in repr(context)
    assert "Private Book Title" not in str(context)
    with pytest.raises(AttributeError):
        context.page_kind = PageKind.CHAPTER  # type: ignore[misc]
    with pytest.raises(AttributeError):
        context.snapshot = snapshot("<p>replacement secret</p>")  # type: ignore[misc]
    with pytest.raises(TypeError):
        asdict(context)  # type: ignore[arg-type]
    assert scored.calibration_id == "heuristic-v1"
    with pytest.raises(TypeError):
        ScoringContext(PageKind.CHAPTER, {})  # type: ignore[arg-type]
    with pytest.raises(FrozenInstanceError):
        scored.score = 0.1  # type: ignore[misc]


def test_metadata_and_raw_score_cannot_spoof_quality() -> None:
    context = ScoringContext(PageKind.CHAPTER, snapshot("<div id='x'>tiny</div>"))
    honest = item(FieldKind.CONTENT, "#x", raw=-100, metadata={})
    spoofed = item(FieldKind.CONTENT, "#x", raw=10000, metadata={"paragraph_count": 999, "noise_ratio": 0.0, "semantic_role": "content"})
    assert CandidateScorer().score(honest, context).score == CandidateScorer().score(spoofed, context).score


@pytest.mark.parametrize("selector", ["#missing", "[", "script"])
def test_missing_invalid_or_wrong_semantic_selector_scores_zero(selector: str) -> None:
    context = ScoringContext(PageKind.CHAPTER, snapshot("<script>" + "private " * 100 + "</script><article>short</article>"))
    scored = CandidateScorer().score(item(FieldKind.CONTENT, selector), context)
    assert scored.score == 0 and all(component.score == 0 for component in scored.components)
    assert all(math.isfinite(component.score) for component in scored.components)


def test_extractor_to_scorer_scores_all_chapter_fields_and_comments_lose() -> None:
    html = (FIXTURES / "chapter_nested_noise.html").read_text(encoding="utf-8")
    extra = "<a rel='prev' href='/read/0'>Previous Chapter</a><a href='/book'>Table of Contents</a>"
    extra += "<section class='comments'><p>" + "discussion words " * 500 + "</p><p>more comments</p></section>"
    html = html.replace("</body>", extra + "</body>")
    page = snapshot(html)
    context = ScoringContext(PageKind.CHAPTER, page)
    candidates = CandidateExtractor().extract(page, PageKind.CHAPTER)
    scorer = CandidateScorer()
    fields = {candidate.field for candidate in candidates}
    for field in (FieldKind.CHAPTER_TITLE, FieldKind.CONTENT, FieldKind.PREV_LINK, FieldKind.NEXT_LINK, FieldKind.INDEX_LINK, FieldKind.CLEAN_SELECTOR):
        assert field in fields
        assert all(0 <= scorer.score(candidate, context).score <= 1 for candidate in candidates.for_field(field))
    comment = item(FieldKind.CONTENT, ".comments")
    article = candidates.for_field(FieldKind.CONTENT)[0]
    assert scorer.score(article, context).score > scorer.score(comment, context).score


def test_extractor_to_scorer_scores_catalog_title_author_and_mixed_list() -> None:
    html = (FIXTURES / "catalog_mixed.html").read_text(encoding="utf-8")
    html = "<h1>Example Novel</h1><p class='author'>Written by Ada Stone</p>" + html
    page = snapshot(html)
    context = ScoringContext(PageKind.BOOK_INDEX, page)
    result = CandidateExtractor().extract(page, PageKind.BOOK_INDEX)
    scorer = CandidateScorer()
    for field in (FieldKind.TITLE, FieldKind.AUTHOR, FieldKind.CHAPTER_LIST):
        assert result.for_field(field)
        assert max(scorer.score(candidate, context).score for candidate in result.for_field(field)) > 0.4
    selected = result.for_field(FieldKind.CHAPTER_LIST)[0]
    auxiliary = item(FieldKind.CHAPTER_LIST, ".catalog-list a")
    assert scorer.score(selected, context).score > scorer.score(auxiliary, context).score


@pytest.mark.parametrize(
    ("field", "html", "selector", "prefixes"),
    [
        (FieldKind.TITLE, "<h1>长夜余火</h1>", "h1", {"title."}),
        (FieldKind.AUTHOR, "<p>作者：余华</p>", "p", {"author."}),
        (FieldKind.CHAPTER_TITLE, "<h1>第十二章 风雪</h1>", "h1", {"chapter_title."}),
        (FieldKind.CHAPTER_LIST, "<nav><a href='/1'>第一章</a><a href='/2'>第二章</a><a href='/3'>第三章</a></nav>", "nav a", {"chapter_list."}),
        (FieldKind.CONTENT, "<article><p>" + "正文段落。" * 30 + "</p><p>继续。</p></article>", "article", {"content."}),
        (FieldKind.PREV_LINK, "<a rel='prev'>上一章</a>", "a", {"prev_link."}),
        (FieldKind.NEXT_LINK, "<a rel='next'>Next Chapter</a>", "a", {"next_link."}),
        (FieldKind.INDEX_LINK, "<a>目录</a>", "a", {"index_link."}),
        (FieldKind.CLEAN_SELECTOR, "<aside class='comments'>评论</aside>", "aside", {"clean_selector."}),
    ],
)
def test_every_field_has_prefixed_bounded_components(field: FieldKind, html: str, selector: str, prefixes: set[str]) -> None:
    scored = CandidateScorer().score(item(field, selector), ScoringContext(PageKind.CHAPTER, snapshot(html)))
    assert scored.components
    assert all(any(component.rule_id.startswith(prefix) for prefix in prefixes) for component in scored.components)
    assert all(math.isfinite(component.score) and 0 <= component.score <= 1 and component.weight > 0 for component in scored.components)


def test_component_weights_form_exact_normalized_mean() -> None:
    class FixedRule:
        rule_id = "title.custom"
        field = FieldKind.TITLE

        def components(self, identity: CandidateIdentity, features: object, config: ScoringConfig) -> tuple[ScoreComponent, ...]:
            assert identity == CandidateIdentity(FieldKind.TITLE, "h1")
            assert not hasattr(identity, "metadata") and not hasattr(identity, "raw_score") and not hasattr(identity, "evidence")
            assert config.calibration_id == "heuristic-v1"
            del features
            return (ScoreComponent("title.low", 0, 1), ScoreComponent("title.high", 1, 3))

    scored = CandidateScorer([FixedRule()]).score(item(FieldKind.TITLE, "h1"), ScoringContext(PageKind.BOOK_INDEX, snapshot("<h1>x</h1>")))
    assert isinstance(FixedRule(), ScoringRule)
    assert scored.score == 0.75


@pytest.mark.parametrize(
    ("field", "weak", "strong"),
    [
        (FieldKind.TITLE, "<div>Book</div>", "<h1>Book</h1>"),
        (FieldKind.AUTHOR, "<p>Ada</p>", "<p>Written by Ada</p>"),
        (FieldKind.CHAPTER_TITLE, "<div>Wind</div>", "<h1>Chapter 12: Wind</h1>"),
        (FieldKind.CONTENT, "<article><p>short</p></article>", "<article><p>" + "prose " * 100 + "</p><p>continued</p></article>"),
        (FieldKind.NEXT_LINK, "<a>link</a>", "<a rel='next'>Next Chapter</a>"),
    ],
)
def test_representative_signals_are_monotonic(field: FieldKind, weak: str, strong: str) -> None:
    scorer = CandidateScorer()
    weak_score = scorer.score(item(field, weak.split("<", 2)[1].split(">", 1)[0].split()[0]), ScoringContext(PageKind.CHAPTER, snapshot(weak))).score
    strong_score = scorer.score(item(field, strong.split("<", 2)[1].split(">", 1)[0].split()[0]), ScoringContext(PageKind.CHAPTER, snapshot(strong))).score
    assert strong_score >= weak_score


def test_rule_registry_and_malformed_outputs_fail_stably() -> None:
    class Rule:
        field = FieldKind.TITLE
        rule_id = "title.custom"

        def __init__(self, result: object) -> None:
            self.result = result

        def components(self, identity: CandidateIdentity, features: object, config: ScoringConfig) -> object:
            del identity, features, config
            return self.result

    with pytest.raises(ValueError, match="duplicate scoring field"):
        CandidateScorer([Rule((ScoreComponent("title.a", 1, 1),)), Rule((ScoreComponent("title.b", 1, 1),))])  # type: ignore[list-item]
    for bad in ([], [object()]):
        with pytest.raises(TypeError, match="ScoringRule"):
            CandidateScorer(bad)  # type: ignore[arg-type]
    for result in ([], (ScoreComponent("title.a", 1, 1), ScoreComponent("title.a", 0, 1)), (ScoreComponent("author.a", 1, 1),)):
        with pytest.raises(ValueError, match="components"):
            CandidateScorer([Rule(result)]).score(item(FieldKind.TITLE, "h1"), ScoringContext(PageKind.BOOK_INDEX, snapshot("<h1>x</h1>")))  # type: ignore[list-item]
    with pytest.raises(ValueError):
        ScoreComponent("bad id", 1, 1)


def test_config_is_versioned_and_score_model_rejects_invalid_values() -> None:
    config = ScoringConfig(version="v3", calibration_id="heuristic-v1")
    assert config.version == "v3" and config.calibration_id == "heuristic-v1"
    with pytest.raises(ValueError):
        ScoringConfig(calibration_id="secret value")
    with pytest.raises(ValueError):
        ScoreComponent("title.x", float("nan"), 1)
    with pytest.raises(ValueError):
        ScoredCandidate(item(FieldKind.TITLE, "h1"), 2, (), "heuristic-v1", "v1", "sample-1", "https://example.test", PageKind.BOOK_INDEX)


def test_identity_is_frozen_and_selector_is_safe() -> None:
    identity = CandidateIdentity(FieldKind.CONTENT, "article.chapter-content")
    with pytest.raises(FrozenInstanceError):
        identity.selector = "body"  # type: ignore[misc]
    for selector in ("", "https://private.test", "a[href*='token=secret']"):
        with pytest.raises(ValueError):
            CandidateIdentity(FieldKind.CONTENT, selector)
    with pytest.raises(TypeError):
        CandidateIdentity("content", "article")  # type: ignore[arg-type]


def test_chapter_continuity_requires_adjacent_numbers() -> None:
    scorer = CandidateScorer()

    def continuity(html: str) -> float:
        scored = scorer.score(item(FieldKind.CHAPTER_LIST, "nav a"), ScoringContext(PageKind.BOOK_INDEX, snapshot(html)))
        return next(component.score for component in scored.components if component.rule_id == "chapter_list.continuity")

    adjacent = "<nav><a href='/1'>Chapter 1</a><a href='/2'>Chapter 2</a><a href='/3'>Chapter 3</a></nav>"
    jumped = "<nav><a href='/1'>Chapter 1</a><a href='/99'>Chapter 99</a></nav>"
    volume_boundary = "<nav><a href='/11'>Book 1 Chapter 1</a><a href='/21'>Book 2 Chapter 1</a></nav>"
    special = "<nav><a href='/1'>Prologue</a><a href='/2'>Interlude</a></nav>"
    assert continuity(adjacent) == 1
    assert continuity(jumped) == 0
    assert continuity(volume_boundary) == 0
    assert continuity(special) == 0


def test_remaining_validation_boundaries_are_stable() -> None:
    with pytest.raises(ValueError):
        ScoringConfig(target_paragraphs=0)
    for score, weight in ((-0.1, 1), (1.1, 1), (0.5, 0), (0.5, float("inf"))):
        with pytest.raises(ValueError):
            ScoreComponent("title.boundary", score, weight)
    with pytest.raises(TypeError):
        ScoringContext("chapter", snapshot(""))  # type: ignore[arg-type]


def test_scoring_provenance_is_safe_and_set_only_from_context() -> None:
    page = snapshot("<h1>Book</h1>")
    first = ScoringContext(PageKind.BOOK_INDEX, page)
    second = ScoringContext(PageKind.BOOK_INDEX, page)
    assert first.sample_id == second.sample_id == page.sample_id
    with pytest.raises(TypeError):
        ScoringContext(PageKind.BOOK_INDEX, page, "caller-id")  # type: ignore[call-arg]
    scored_value = CandidateScorer().score(item(FieldKind.TITLE, "h1"), first)
    assert scored_value.sample_id == first.sample_id
    assert scored_value.origin_key == "https://reader.example"
    assert scored_value.page_kind is PageKind.BOOK_INDEX


def test_two_contexts_for_same_snapshot_share_the_snapshot_identity() -> None:
    from novel_crawler.adaptation.decision import ScoredPageBatch

    page = snapshot("<h1>Book</h1>")
    scorer = CandidateScorer()
    first = ScoringContext(PageKind.BOOK_INDEX, page)
    second = ScoringContext(PageKind.BOOK_INDEX, page)
    values = (scorer.score(item(FieldKind.TITLE, "h1"), first), scorer.score(item(FieldKind.TITLE, "h1"), second))
    batch = ScoredPageBatch(first.sample_id, first.origin_key, PageKind.BOOK_INDEX, values)
    assert len(batch.candidates) == 2

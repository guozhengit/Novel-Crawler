from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

import novel_crawler.adaptation as adaptation
from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.adaptation.models import Candidate, Evidence, FieldKind
from novel_crawler.adaptation.scoring import (
    CandidateScorer,
    ScoreComponent,
    ScoredCandidate,
    ScorerConfig,
    ScoringContext,
    ScoringRule,
)


def candidate(field: FieldKind, metadata: dict[str, str | int | float | bool], *, raw: float = 99.0) -> Candidate:
    return Candidate(field, "main > div", "count=1", raw, 0.01, (Evidence("extract.stable", 1.0, "count=1"),), metadata)


def test_value_objects_are_frozen_validated_and_context_is_structural() -> None:
    context = ScoringContext(PageKind.CHAPTER, {"dom_position": "primary", "sibling_count": 3})
    component = ScoreComponent("title.dom", 0.8, 2.0)
    result = ScoredCandidate(candidate(FieldKind.TITLE, {}), 0.8, (component,))
    assert context.snapshot["sibling_count"] == 3 and result.confidence == 0.8
    with pytest.raises(FrozenInstanceError):
        component.value = 0.2  # type: ignore[misc]
    with pytest.raises(TypeError):
        context.snapshot["x"] = 1  # type: ignore[index]
    for bad in (float("nan"), float("inf"), -0.1, 1.1):
        with pytest.raises(ValueError):
            ScoreComponent("safe.id", bad, 1.0)
    with pytest.raises(ValueError):
        ScoreComponent("safe.id", 0.5, 0)
    for unsafe in ({"url": "https://secret.test"}, {"html": "<p>secret</p>"}, {"hash": "deadbeef"}, {"title": "private words"}):
        with pytest.raises(ValueError):
            ScoringContext(PageKind.CHAPTER, unsafe)


def test_scoring_api_is_exported_from_adaptation_package() -> None:
    assert adaptation.CandidateScorer is CandidateScorer
    assert adaptation.ScoringContext is ScoringContext


def test_title_has_separate_semantic_dom_and_length_components() -> None:
    item = candidate(FieldKind.TITLE, {"semantic_role": "book_title", "dom_role": "h1", "length_bucket": "17-64"})
    scored = CandidateScorer().score(item, ScoringContext(PageKind.BOOK_INDEX, {}))
    assert {part.rule_id for part in scored.components} == {"title.semantic", "title.dom", "title.length"}
    assert 0 <= scored.confidence <= 1


@pytest.mark.parametrize(
    ("field", "metadata", "expected"),
    [
        (FieldKind.AUTHOR, {"semantic_role": "author_label", "length_bucket": "1-16"}, {"author.semantic", "author.length"}),
        (FieldKind.CHAPTER_TITLE, {"semantic_role": "chapter_title", "dom_role": "h1", "length_bucket": "1-16"}, {"chapter_title.semantic", "chapter_title.dom", "chapter_title.length"}),
        (FieldKind.CHAPTER_LIST, {"link_count": 20, "continuity_ratio": 0.9, "same_origin_ratio": 1.0, "selector_precision": 0.95}, {"chapter_list.count", "chapter_list.continuity", "chapter_list.same_origin", "chapter_list.selector_precision"}),
        (FieldKind.CONTENT, {"length_bucket": "1000+", "paragraph_count": 12, "link_density": 0.02, "noise_ratio": 0.01}, {"content.length", "content.paragraphs", "content.link_density", "content.noise"}),
        (FieldKind.NEXT_LINK, {"rel_match": True, "text_match": True, "order_match": True}, {"navigation.rel", "navigation.text", "navigation.order"}),
    ],
)
def test_each_field_uses_its_own_rule_family(field: FieldKind, metadata: dict[str, str | int | float | bool], expected: set[str]) -> None:
    scored = CandidateScorer().score(candidate(field, metadata), ScoringContext(PageKind.CHAPTER, {}))
    assert {part.rule_id for part in scored.components} == expected


def test_weighted_mean_is_normalized_and_raw_score_is_ignored() -> None:
    class Rule:
        rule_id = "custom.fixed"
        fields = frozenset({FieldKind.TITLE})

        def components(self, item: Candidate, context: ScoringContext, config: ScorerConfig) -> tuple[ScoreComponent, ...]:
            del item, context, config
            return (ScoreComponent("custom.low", 0.0, 1.0), ScoreComponent("custom.high", 1.0, 3.0))

    scorer = CandidateScorer(rules=[Rule()])
    low_raw = scorer.score(candidate(FieldKind.TITLE, {}, raw=-1000), ScoringContext(PageKind.UNKNOWN, {}))
    high_raw = scorer.score(candidate(FieldKind.TITLE, {}, raw=1000), ScoringContext(PageKind.UNKNOWN, {}))
    assert isinstance(Rule(), ScoringRule)
    assert low_raw.confidence == high_raw.confidence == 0.75


def test_boundaries_are_monotonic_and_never_nan() -> None:
    scorer = CandidateScorer()
    context = ScoringContext(PageKind.BOOK_INDEX, {})
    values = [scorer.score(candidate(FieldKind.CHAPTER_LIST, {"link_count": count}), context).confidence for count in (0, 3, 10, 30, 1000000)]
    assert values == sorted(values)
    assert all(math.isfinite(value) and 0 <= value <= 1 for value in values)
    malformed = candidate(FieldKind.CONTENT, {"paragraph_count": -99, "link_density": 999.0, "noise_ratio": -5.0, "length_bucket": "unknown"})
    assert math.isfinite(scorer.score(malformed, ScoringContext(PageKind.CHAPTER, {})).confidence)


def test_comments_with_more_text_do_not_beat_clean_main_content() -> None:
    scorer = CandidateScorer()
    context = ScoringContext(PageKind.CHAPTER, {})
    comments = candidate(FieldKind.CONTENT, {"length_bucket": "1000+", "paragraph_count": 30, "link_density": 0.4, "noise_ratio": 0.9})
    正文 = candidate(FieldKind.CONTENT, {"length_bucket": "257-1000", "paragraph_count": 8, "link_density": 0.02, "noise_ratio": 0.0})
    assert scorer.score(正文, context).confidence > scorer.score(comments, context).confidence


def test_catalog_auxiliary_links_reduce_precision_and_continuity() -> None:
    scorer = CandidateScorer()
    context = ScoringContext(PageKind.BOOK_INDEX, {})
    clean = candidate(FieldKind.CHAPTER_LIST, {"link_count": 20, "continuity_ratio": 1.0, "same_origin_ratio": 1.0, "selector_precision": 1.0})
    mixed = candidate(FieldKind.CHAPTER_LIST, {"link_count": 24, "continuity_ratio": 0.55, "same_origin_ratio": 1.0, "selector_precision": 0.7})
    assert scorer.score(clean, context).confidence > scorer.score(mixed, context).confidence


@pytest.mark.parametrize("semantic", ["chapter_title", "chapter_title_zh"])
def test_chinese_and_english_semantic_tokens_are_supported(semantic: str) -> None:
    item = candidate(FieldKind.CHAPTER_TITLE, {"semantic_role": semantic, "dom_role": "h1", "length_bucket": "1-16"})
    assert CandidateScorer().score(item, ScoringContext(PageKind.CHAPTER, {})).confidence > 0.7


def test_fields_are_not_ranked_together_and_config_has_versioned_thresholds() -> None:
    config = ScorerConfig(min_chapter_links=5, target_chapter_links=25, version="score-v3")
    scorer = CandidateScorer(config=config)
    assert scorer.config.version == "score-v3"
    with pytest.raises(ValueError, match="same field"):
        scorer.rank([candidate(FieldKind.TITLE, {}), candidate(FieldKind.AUTHOR, {})], ScoringContext(PageKind.BOOK_INDEX, {}))
    with pytest.raises(ValueError):
        ScorerConfig(min_chapter_links=10, target_chapter_links=5)


def test_invalid_protocol_context_and_nested_results_are_rejected() -> None:
    with pytest.raises(TypeError):
        ScoringContext("chapter", {})  # type: ignore[arg-type]
    for snapshot in ({"sibling_count": object()}, {"sibling_count": float("nan")}, {"dom_role": "private words"}):
        with pytest.raises(ValueError):
            ScoringContext(PageKind.CHAPTER, snapshot)  # type: ignore[arg-type]
    base = candidate(FieldKind.TITLE, {})
    with pytest.raises(TypeError):
        ScoredCandidate("bad", 0.5, (ScoreComponent("safe.id", 1, 1),))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ScoredCandidate(base, float("nan"), (ScoreComponent("safe.id", 1, 1),))
    with pytest.raises(ValueError):
        ScoredCandidate(base, 0.5, ())
    with pytest.raises(TypeError):
        CandidateScorer(rules=[object()])  # type: ignore[list-item]
    class TitleOnly:
        rule_id = "title.only"
        fields = frozenset({FieldKind.TITLE})

        def components(self, item: Candidate, context: ScoringContext, config: ScorerConfig) -> tuple[ScoreComponent, ...]:
            del item, context, config
            return (ScoreComponent("title.only", 1, 1),)

    with pytest.raises(ValueError, match="no scoring rule"):
        CandidateScorer(rules=[TitleOnly()]).score(candidate(FieldKind.AUTHOR, {}), ScoringContext(PageKind.BOOK_INDEX, {}))


def test_clean_selector_and_navigation_variants_are_field_local() -> None:
    scorer = CandidateScorer()
    context = ScoringContext(PageKind.CHAPTER, {})
    clean = scorer.score(candidate(FieldKind.CLEAN_SELECTOR, {"noise_marker": True}), context)
    assert clean.confidence == 1
    for field in (FieldKind.PREV_LINK, FieldKind.NEXT_LINK, FieldKind.INDEX_LINK):
        assert scorer.score(candidate(field, {"text_match": True}), context).components[1].value == 1
    assert scorer.rank([], context) == ()

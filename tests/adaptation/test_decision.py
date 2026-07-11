from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict

import pytest

from novel_crawler.acquisition.classifier import Classification, PageKind
from novel_crawler.adaptation.decision import DecisionKind, DecisionPolicy
from novel_crawler.adaptation.diagnostics import Diagnostic, DiagnosticCode
from novel_crawler.adaptation.models import Candidate, Evidence, FieldKind
from novel_crawler.adaptation.scoring import ScoreComponent, ScoredCandidate


def scored(field: FieldKind, selector: str, score: float) -> ScoredCandidate:
    candidate = Candidate(field, selector, "count=1", 0, 0, (Evidence("extract.test", 1, "count=1"),), {})
    return ScoredCandidate(candidate, score, (ScoreComponent("title.test", score, 1),), "heuristic-v1", "score-v1")


@pytest.mark.parametrize(
    ("score", "kind"),
    [(0.5999, DecisionKind.REJECT), (0.60, DecisionKind.REQUIRE_CONFIRMATION), (0.8499, DecisionKind.REQUIRE_CONFIRMATION), (0.85, DecisionKind.AUTO_ACCEPT)],
)
def test_threshold_boundaries(score: float, kind: DecisionKind) -> None:
    values = [scored(FieldKind.TITLE, "h1", score), scored(FieldKind.CHAPTER_LIST, "#list a", score)]
    assert DecisionPolicy().decide(Classification(PageKind.BOOK_INDEX, 1, ()), values, "https://example.test/private?q=x").kind is kind


def test_overall_score_cannot_mask_weak_critical_field() -> None:
    values = [scored(FieldKind.TITLE, "h1", 0.59), scored(FieldKind.CHAPTER_LIST, "#list a", 1.0)]
    result = DecisionPolicy().decide(Classification(PageKind.BOOK_INDEX, 1, ()), values)
    assert result.overall_score > 0.6
    assert result.kind is DecisionKind.REJECT


def test_ties_are_deterministic_and_report_ambiguity() -> None:
    values = [scored(FieldKind.CHAPTER_TITLE, "h2", 0.9), scored(FieldKind.CHAPTER_TITLE, "h1", 0.9), scored(FieldKind.CONTENT, "article", 0.9)]
    result = DecisionPolicy().decide(Classification(PageKind.CHAPTER, 1, ()), values)
    title = next(item for item in result.fields if item.field is FieldKind.CHAPTER_TITLE)
    assert title.best_selector == "h1"
    assert title.reason_ids == ("ambiguous_candidates",)
    assert result.kind is DecisionKind.REQUIRE_CONFIRMATION
    assert result.diagnostic.codes == (DiagnosticCode.AMBIGUOUS_CANDIDATES,)


def test_missing_fields_reject_for_each_supported_page_kind() -> None:
    for kind, field in ((PageKind.BOOK_INDEX, FieldKind.TITLE), (PageKind.CHAPTER, FieldKind.CHAPTER_TITLE)):
        result = DecisionPolicy().decide(Classification(kind, 1, ()), ())
        assert result.kind is DecisionKind.REJECT
        assert result.fields[0].field is field
        assert DiagnosticCode.MISSING_FIELD in result.diagnostic.codes


@pytest.mark.parametrize(
    ("page_kind", "code"),
    [(PageKind.AUTH_OR_CHALLENGE, DiagnosticCode.AUTH_REQUIRED), (PageKind.ERROR, DiagnosticCode.ERROR_PAGE), (PageKind.UNKNOWN, DiagnosticCode.UNSUPPORTED_PAGE), (PageKind.SEARCH_OR_LIST, DiagnosticCode.UNSUPPORTED_PAGE)],
)
def test_classifier_terminal_pages_reject(page_kind: PageKind, code: DiagnosticCode) -> None:
    result = DecisionPolicy().decide(Classification(page_kind, 1, ("private.payload",)), ())
    assert result.kind is DecisionKind.REJECT
    assert result.diagnostic.codes == (code,)


def test_diagnostic_serialization_is_frozen_and_privacy_safe() -> None:
    result = DecisionPolicy().decide(Classification(PageKind.BOOK_INDEX, 1, ()), (), "https://user:pass@example.test:8443/a?token=secret")
    payload = result.diagnostic.to_dict()
    encoded = result.diagnostic.to_json()
    assert payload["origin"] == "https://example.test:8443"
    assert json.loads(encoded) == payload
    assert "secret" not in encoded and "pass" not in encoded and "/a" not in encoded
    assert asdict(result)["diagnostic"]["origin"] == "https://example.test:8443"
    with pytest.raises(FrozenInstanceError):
        result.diagnostic.origin = "x"  # type: ignore[misc]


def test_malformed_inputs_fail_closed() -> None:
    policy = DecisionPolicy()
    with pytest.raises(TypeError):
        policy.decide("chapter", ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        policy.decide(Classification(PageKind.CHAPTER, 1, ()), [object()])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        Diagnostic((DiagnosticCode.MISSING_FIELD,), ("unsafe evidence text",), "https://example.test", {"fields": 1})

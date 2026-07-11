from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict

import pytest

from novel_crawler.acquisition.classifier import Classification, PageKind
from novel_crawler.adaptation.decision import DecisionConfig, DecisionKind, DecisionPolicy, ScoredPageBatch
from novel_crawler.adaptation.diagnostics import Diagnostic, DiagnosticCode, safe_origin
from novel_crawler.adaptation.models import Candidate, Evidence, FieldKind
from novel_crawler.adaptation.scoring import ScoreComponent, ScoredCandidate


def scored(field: FieldKind, selector: str, score: float) -> ScoredCandidate:
    candidate = Candidate(field, selector, "count=1", 0, 0, (Evidence("extract.test", 1, "count=1"),), {})
    return ScoredCandidate(candidate, score, (ScoreComponent("title.test", score, 1),), "heuristic-v1", "score-v1")


def batch(kind: PageKind, values: list[ScoredCandidate] | tuple[ScoredCandidate, ...] = ()) -> ScoredPageBatch:
    return ScoredPageBatch("sample-1", "https://example.test", kind, tuple(values))


@pytest.mark.parametrize(
    ("score", "kind"),
    [(0.5999, DecisionKind.REJECT), (0.60, DecisionKind.REQUIRE_CONFIRMATION), (0.8499, DecisionKind.REQUIRE_CONFIRMATION), (0.85, DecisionKind.AUTO_ACCEPT)],
)
def test_threshold_boundaries(score: float, kind: DecisionKind) -> None:
    values = [scored(FieldKind.TITLE, "h1", score), scored(FieldKind.CHAPTER_LIST, "#list a", score)]
    assert DecisionPolicy().decide(Classification(PageKind.BOOK_INDEX, 1, ()), batch(PageKind.BOOK_INDEX, values)).kind is kind


def test_overall_score_cannot_mask_weak_critical_field() -> None:
    values = [scored(FieldKind.TITLE, "h1", 0.59), scored(FieldKind.CHAPTER_LIST, "#list a", 1.0)]
    result = DecisionPolicy().decide(Classification(PageKind.BOOK_INDEX, 1, ()), batch(PageKind.BOOK_INDEX, values))
    assert result.overall_score > 0.6
    assert result.kind is DecisionKind.REJECT


def test_ties_are_deterministic_and_report_ambiguity() -> None:
    values = [scored(FieldKind.CHAPTER_TITLE, "h2", 0.9), scored(FieldKind.CHAPTER_TITLE, "h1", 0.9), scored(FieldKind.CONTENT, "article", 0.9)]
    result = DecisionPolicy().decide(Classification(PageKind.CHAPTER, 1, ()), batch(PageKind.CHAPTER, values))
    title = next(item for item in result.fields if item.field is FieldKind.CHAPTER_TITLE)
    assert title.best_selector == "h1"
    assert title.reason_ids == ("ambiguous_candidates",)
    assert result.kind is DecisionKind.REQUIRE_CONFIRMATION
    assert result.diagnostic.codes == (DiagnosticCode.AMBIGUOUS_CANDIDATES,)


def test_missing_fields_reject_for_each_supported_page_kind() -> None:
    for kind, field in ((PageKind.BOOK_INDEX, FieldKind.TITLE), (PageKind.CHAPTER, FieldKind.CHAPTER_TITLE)):
        result = DecisionPolicy().decide(Classification(kind, 1, ()), batch(kind))
        assert result.kind is DecisionKind.REJECT
        assert result.fields[0].field is field
        assert DiagnosticCode.MISSING_FIELD in result.diagnostic.codes


@pytest.mark.parametrize(
    ("page_kind", "code"),
    [(PageKind.AUTH_OR_CHALLENGE, DiagnosticCode.AUTH_REQUIRED), (PageKind.ERROR, DiagnosticCode.ERROR_PAGE), (PageKind.UNKNOWN, DiagnosticCode.UNSUPPORTED_PAGE), (PageKind.SEARCH_OR_LIST, DiagnosticCode.UNSUPPORTED_PAGE)],
)
def test_classifier_terminal_pages_reject(page_kind: PageKind, code: DiagnosticCode) -> None:
    result = DecisionPolicy().decide(Classification(page_kind, 1, ("private.payload",)), batch(page_kind))
    assert result.kind is DecisionKind.REJECT
    assert result.diagnostic.codes == (code,)


def test_diagnostic_serialization_is_frozen_and_privacy_safe() -> None:
    result = DecisionPolicy().decide(Classification(PageKind.BOOK_INDEX, 1, ()), ScoredPageBatch("sample-1", "https://example.test:8443", PageKind.BOOK_INDEX, ()))
    payload = result.diagnostic.to_dict()
    encoded = result.diagnostic.to_json()
    assert payload["origin"] == "https://example.test:8443"
    assert json.loads(encoded) == payload
    assert "secret" not in encoded and "pass" not in encoded and "/a" not in encoded
    with pytest.raises(TypeError):
        asdict(result)  # type: ignore[arg-type]
    serialized = result.to_json()
    assert "best_selector" not in serialized and "h1" not in serialized
    with pytest.raises(FrozenInstanceError):
        result.diagnostic.origin = "x"  # type: ignore[misc]


def test_malformed_inputs_fail_closed() -> None:
    policy = DecisionPolicy()
    with pytest.raises(TypeError):
        policy.decide("chapter", batch(PageKind.CHAPTER))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        policy.decide(Classification(PageKind.CHAPTER, 1, ()), [object()])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Diagnostic((DiagnosticCode.MISSING_FIELD,), ("unsafe evidence text",), "https://example.test", {"fields": 1})


def test_diagnostic_counts_are_deeply_immutable_and_copied() -> None:
    source = {"fields": 1}
    diagnostic = Diagnostic((DiagnosticCode.MISSING_FIELD,), (), "redacted", source)
    source["fields"] = 2
    assert diagnostic.counts["fields"] == 1
    with pytest.raises(TypeError):
        diagnostic.counts["fields"] = 3  # type: ignore[index]
    copied = diagnostic.to_dict()["counts"]
    copied["fields"] = 4
    assert diagnostic.counts["fields"] == 1
    for bad in ({"Bad": 1}, {"ok": -1}, {"ok": True}):
        with pytest.raises(ValueError):
            Diagnostic((), (), "redacted", bad)


@pytest.mark.parametrize(("delta", "ambiguous"), [(0.029999, True), (0.03, False)])
def test_ambiguity_margin_has_exact_deterministic_boundary(delta: float, ambiguous: bool) -> None:
    values = [scored(FieldKind.TITLE, "h1", 0.90), scored(FieldKind.TITLE, "h2", 0.90 - delta), scored(FieldKind.CHAPTER_LIST, "#list a", 0.90)]
    result = DecisionPolicy(DecisionConfig()).decide(Classification(PageKind.BOOK_INDEX, 1, ()), batch(PageKind.BOOK_INDEX, values))
    assert (result.kind is DecisionKind.REQUIRE_CONFIRMATION) is ambiguous
    assert (DiagnosticCode.AMBIGUOUS_CANDIDATES in result.diagnostic.codes) is ambiguous


def test_batch_validates_identity_origin_kind_and_single_page_contract() -> None:
    for sample_id in ("", "https://example.test", "sample secret"):
        with pytest.raises(ValueError):
            ScoredPageBatch(sample_id, "https://example.test", PageKind.CHAPTER, ())
    with pytest.raises(ValueError):
        ScoredPageBatch("sample-1", "https://example.test/path", PageKind.CHAPTER, ())
    with pytest.raises(ValueError, match="page_kind"):
        DecisionPolicy().decide(Classification(PageKind.BOOK_INDEX, 1, ()), batch(PageKind.CHAPTER))


def test_decision_repr_and_serialization_never_expose_selector_or_private_markup() -> None:
    private = "#account-938.member-class"
    values = [scored(FieldKind.TITLE, private, 0.9), scored(FieldKind.CHAPTER_LIST, "#list a", 0.9)]
    result = DecisionPolicy().decide(Classification(PageKind.BOOK_INDEX, 1, ()), batch(PageKind.BOOK_INDEX, values))
    field = result.fields[0]
    assert field.best_selector == private
    assert private not in repr(field) and private not in repr(result)
    assert private not in result.to_json()
    assert private not in repr(batch(PageKind.BOOK_INDEX, values))
    with pytest.raises(TypeError):
        asdict(field)  # type: ignore[arg-type]


def test_safe_origin_brackets_ipv6_and_drops_credentials_path_and_query() -> None:
    assert safe_origin("https://user:pass@[2001:db8::1]:8443/private?q=secret") == "https://[2001:db8::1]:8443"
    assert safe_origin(None) == "redacted"
    assert safe_origin("ftp://example.test/path") == "redacted"
    assert safe_origin("https://[broken") == "redacted"


def test_new_models_reject_malformed_values_and_are_immutable() -> None:
    with pytest.raises(ValueError):
        DecisionConfig(high=float("nan"))
    with pytest.raises(ValueError):
        DecisionConfig(high=0.5, medium=0.6)
    with pytest.raises(ValueError):
        DecisionConfig(version="unsafe version")
    with pytest.raises(TypeError):
        ScoredPageBatch("sample-1", "redacted", "chapter", ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ScoredPageBatch("sample-1", "redacted", PageKind.CHAPTER, (object(),))  # type: ignore[arg-type]
    result = DecisionPolicy().decide(Classification(PageKind.BOOK_INDEX, 1, ()), batch(PageKind.BOOK_INDEX))
    with pytest.raises(AttributeError):
        result.fields[0].score = 1  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.kind = DecisionKind.AUTO_ACCEPT  # type: ignore[misc]

from __future__ import annotations

import json

import pytest
from bs4 import BeautifulSoup

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.adaptation.decision import DecisionKind
from novel_crawler.adaptation.service import ProbeService
from novel_crawler.adaptation.validation import ConfigDraft, MultiPageValidator, PageValidation


def page(name: str, *, selector: str = "#content", title: str = "book-a", length: int = 500,
         paragraphs: int = 5, next_matches: bool = True, auth: bool = False) -> PageValidation:
    return PageValidation(name, PageKind.CHAPTER, DecisionKind.AUTO_ACCEPT, title == "book-a", selector,
                          length, paragraphs, next_matches, auth, "html/body/article/content")


def test_validation_accepts_semantically_compatible_content_containers() -> None:
    draft = ConfigDraft("draft-v1", "example.test", {"content": 0.91}, {"content": "main > div:nth-of-type(2)"})
    result = MultiPageValidator().validate(
        page("chapter-1", selector="main > div:nth-of-type(2)"),
        page("chapter-2", selector="main > div:nth-of-type(3)"), draft,
    )
    assert result.ok and result.confidence >= 0.85
    assert result.outcome is DecisionKind.AUTO_ACCEPT
    assert draft.selector("content") == "main > div:nth-of-type(2)"
    encoded = json.dumps(result.to_dict())
    assert "nth-of-type" not in encoded and "selector" not in encoded


def test_validation_rejects_wrong_next_auth_duplicate_and_bad_structure() -> None:
    draft = ConfigDraft("draft-v1", "example.test", {"content": 0.9}, {"content": "#content"})
    result = MultiPageValidator().validate(
        page("same", selector="#content", next_matches=False),
        PageValidation("same", PageKind.CHAPTER, DecisionKind.AUTO_ACCEPT, True, "article", 500, 5, True, True, "html/body/main/content"), draft,
    )
    assert not result.ok and result.outcome is DecisionKind.REJECT
    assert {"next_link_mismatch", "url_duplicate", "auth_or_error", "content_structure_mismatch"} <= set(result.reason_ids)
    assert "same" not in result.to_json()


def test_validation_medium_confidence_requires_confirmation() -> None:
    draft = ConfigDraft("draft-v1", "example.test", {"content": 0.72}, {"content": "article"})
    result = MultiPageValidator().validate(page("one", selector="article"), page("two", selector="article"), draft)
    assert result.ok and result.outcome is DecisionKind.REQUIRE_CONFIRMATION


def test_config_draft_is_deeply_immutable_and_has_explicit_sensitive_export() -> None:
    scores = {"content": 0.9}
    selectors = {"content": "#private-content"}
    draft = ConfigDraft("draft-v1", "example.test", scores, selectors)
    scores["content"] = 0
    selectors["content"] = "changed"
    assert draft.scores["content"] == 0.9 and draft.selector("content") == "#private-content"
    assert "selectors" not in draft.to_dict() and "private" not in json.dumps(draft.to_dict())
    assert draft.to_config()["selectors"]["content"] == "#private-content"
    with pytest.raises(TypeError):
        draft.scores["content"] = 0  # type: ignore[index]
    with pytest.raises(AttributeError):
        draft.domain = "other.test"  # type: ignore[misc]


def test_real_dom_fingerprint_accepts_nth_variation_but_rejects_sidebar_container() -> None:
    first_dom = BeautifulSoup('<main><div></div><div class="content"><p>x</p></div></main>', "lxml")
    second_dom = BeautifulSoup('<main><div></div><div></div><div class="content"><p>x</p></div></main>', "lxml")
    sidebar_dom = BeautifulSoup('<aside><div class="content"><p>x</p></div></aside>', "lxml")
    first_fp = ProbeService._fingerprint(first_dom.select_one("main > div:nth-of-type(2)"))  # type: ignore[arg-type]
    second_fp = ProbeService._fingerprint(second_dom.select_one("main > div:nth-of-type(3)"))  # type: ignore[arg-type]
    sidebar_fp = ProbeService._fingerprint(sidebar_dom.select_one("aside > div"))  # type: ignore[arg-type]
    draft = ConfigDraft("draft-v1", "example.test", {"content": 0.9}, {"content": "main > article"})
    accepted = MultiPageValidator().validate(
        PageValidation("one", PageKind.CHAPTER, DecisionKind.AUTO_ACCEPT, True, "main > div:nth-of-type(2)", 100, 1, True, False, first_fp),
        PageValidation("two", PageKind.CHAPTER, DecisionKind.AUTO_ACCEPT, True, "main > div:nth-of-type(3)", 100, 1, True, False, second_fp), draft,
    )
    rejected = MultiPageValidator().validate(
        PageValidation("one", PageKind.CHAPTER, DecisionKind.AUTO_ACCEPT, True, "main > article", 100, 1, True, False, first_fp),
        PageValidation("two", PageKind.CHAPTER, DecisionKind.AUTO_ACCEPT, True, "aside > article", 100, 1, True, False, sidebar_fp), draft,
    )
    assert accepted.ok
    assert not rejected.ok and "content_structure_mismatch" in rejected.reason_ids


def test_index_decision_is_aggregated_with_both_chapters() -> None:
    draft = ConfigDraft("draft-v1", "example.test", {"content": 0.9}, {"content": ".content"})
    first, second = page("one"), page("two")
    rejected = MultiPageValidator().validate(first, second, draft, index_decision=DecisionKind.REJECT)
    confirmed = MultiPageValidator().validate(first, second, draft, index_decision=DecisionKind.REQUIRE_CONFIRMATION)
    assert not rejected.ok and rejected.outcome is DecisionKind.REJECT
    assert confirmed.ok and confirmed.outcome is DecisionKind.REQUIRE_CONFIRMATION
    assert len(confirmed.page_decisions) == 3

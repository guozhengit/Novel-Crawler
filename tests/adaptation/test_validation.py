from __future__ import annotations

import json

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.adaptation.decision import DecisionKind
from novel_crawler.adaptation.validation import ConfigDraft, MultiPageValidator, PageValidation


def page(name: str, *, selector: str = "#content", title: str = "book-a", length: int = 500,
         paragraphs: int = 5, next_matches: bool = True, auth: bool = False) -> PageValidation:
    return PageValidation(name, PageKind.CHAPTER, DecisionKind.AUTO_ACCEPT, title, selector,
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
        PageValidation("same", PageKind.CHAPTER, DecisionKind.AUTO_ACCEPT, "book-a", "article", 500, 5, True, True, "html/body/main/content"), draft,
    )
    assert not result.ok and result.outcome is DecisionKind.REJECT
    assert {"next_link_mismatch", "url_duplicate", "auth_or_error", "content_structure_mismatch"} <= set(result.reason_ids)
    assert "same" not in result.to_json()


def test_validation_medium_confidence_requires_confirmation() -> None:
    draft = ConfigDraft("draft-v1", "example.test", {"content": 0.72}, {"content": "article"})
    result = MultiPageValidator().validate(page("one", selector="article"), page("two", selector="article"), draft)
    assert result.ok and result.outcome is DecisionKind.REQUIRE_CONFIRMATION

"""Bounded, read-only orchestration for scored site probing."""

from __future__ import annotations

import hashlib
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from novel_crawler.acquisition.classifier import Classification, PageClassifier, PageKind
from novel_crawler.acquisition.http import HttpPageAcquirer
from novel_crawler.acquisition.models import PageSnapshot

from .decision import AdaptationDecision, DecisionPolicy, ScoredPageBatch
from .extractor import CandidateExtractor
from .models import FieldKind
from .scoring import CandidateScorer, ScoringContext
from .validation import ConfigDraft, MultiPageValidator, PageValidation, ValidationResult


class PageAcquirer(Protocol):
    def fetch(self, url: str) -> PageSnapshot: ...


class ProbeService:
    """Probe at most an index and two chapters; never starts a crawler."""

    def __init__(self, acquirer: PageAcquirer | None = None, classifier: PageClassifier | None = None, extractor: CandidateExtractor | None = None, scorer: CandidateScorer | None = None, decision_policy: DecisionPolicy | None = None, validator: MultiPageValidator | None = None, max_pages: int = 3, max_probe_chars: int = 20_000) -> None:
        self.acquirer = acquirer or HttpPageAcquirer(max_body_bytes=max_probe_chars)
        self.classifier = classifier or PageClassifier()
        self.extractor = extractor or CandidateExtractor()
        self.scorer = scorer or CandidateScorer()
        self.decision_policy = decision_policy or DecisionPolicy()
        self.validator = validator or MultiPageValidator()
        self.max_pages = min(3, max_pages)
        self.max_probe_chars = min(20_000, max_probe_chars)
        self._fetches = 0

    def probe(self, book_or_chapter_url: str) -> ValidationResult:
        self._fetches = 0
        start_url = self._strip_sensitive(book_or_chapter_url)
        start = self._fetch(start_url)
        start_class = self.classifier.classify(start)
        start_decision = self._decide(start, start_class)
        if start_class.kind in {PageKind.AUTH_OR_CHALLENGE, PageKind.ERROR, PageKind.UNKNOWN, PageKind.SEARCH_OR_LIST}:
            return self._terminal(start_decision)
        index = start
        first_prefetched: PageSnapshot | None = None
        if start_class.kind is PageKind.CHAPTER:
            first_prefetched = start
            href = self._selected_href(start, start_decision, FieldKind.INDEX_LINK)
            if not href:
                return self._terminal(start_decision, "missing_index_link")
            index = self._fetch(urljoin(start.final_url, href))
        index_class = self.classifier.classify(index)
        index_decision = self._decide(index, index_class)
        links = self._chapter_links(index, index_decision)
        if len(links) < 2:
            return self._terminal(index_decision, "catalog_order_invalid")
        first_url, second_url = (urljoin(index.final_url, value) for value in links[:2])
        first = first_prefetched if first_prefetched and self._same_resource(start_url, first_url) else self._fetch(first_url)
        if self._fetches >= self.max_pages and first_prefetched is None:
            return self._terminal(index_decision, "probe_limit")
        second = self._fetch(second_url)
        first_item = self._page_validation(first, first_url, second_url)
        second_item = self._page_validation(second, second_url, None)
        draft = self._draft(index, index_decision, first, self._decide(first, self.classifier.classify(first)))
        return self.validator.validate(first_item, second_item, draft)

    def _fetch(self, url: str) -> PageSnapshot:
        if self._fetches >= self.max_pages:
            raise RuntimeError("probe page limit exceeded")
        self._fetches += 1
        snapshot = self.acquirer.fetch(url)
        if len(snapshot.html) > self.max_probe_chars:
            raise ValueError("probe response exceeds safe character limit")
        return snapshot

    def _decide(self, snapshot: PageSnapshot, classification: Classification) -> AdaptationDecision:
        extraction = self.extractor.extract(snapshot, classification.kind)
        context = ScoringContext(classification.kind, snapshot)
        scored = tuple(self.scorer.score(item, context) for item in extraction)
        batch = ScoredPageBatch(context.sample_id, context.origin_key, classification.kind, scored)
        return self.decision_policy.decide(classification, batch)

    def _chapter_links(self, snapshot: PageSnapshot, decision: AdaptationDecision) -> list[str]:
        selector = self._selector(decision, FieldKind.CHAPTER_LIST)
        if not selector:
            return []
        soup = BeautifulSoup(snapshot.html, "lxml")
        return [str(node.get("href")) for node in soup.select(selector) if isinstance(node, Tag) and node.get("href")]

    def _selected_href(self, snapshot: PageSnapshot, decision: AdaptationDecision, field: FieldKind) -> str | None:
        selector = self._selector(decision, field)
        soup = BeautifulSoup(snapshot.html, "lxml")
        node = soup.select_one(selector) if selector else None
        if node is None:
            if field is FieldKind.NEXT_LINK:
                node = soup.select_one('a[rel~=next]') or next((link for link in soup.find_all("a", href=True) if link.get_text(" ", strip=True).casefold() in {"next", "next chapter"}), None)
            elif field is FieldKind.INDEX_LINK:
                node = next((link for link in soup.find_all("a", href=True) if link.get_text(" ", strip=True).casefold() in {"contents", "index", "table of contents"}), None)
        return str(node.get("href")) if isinstance(node, Tag) and node.get("href") else None

    def _page_validation(self, snapshot: PageSnapshot, resource_url: str, expected_next: str | None) -> PageValidation:
        classification = self.classifier.classify(snapshot)
        decision = self._decide(snapshot, classification)
        content = self._selector(decision, FieldKind.CONTENT) or ""
        soup = BeautifulSoup(snapshot.html, "lxml")
        node = soup.select_one(content) if content else None
        text_length = len(node.get_text(" ", strip=True)) if isinstance(node, Tag) else 0
        paragraphs = len(node.find_all("p")) if isinstance(node, Tag) else 0
        next_href = self._selected_href(snapshot, decision, FieldKind.NEXT_LINK)
        matches = expected_next is None or next_href is not None and self._same_resource(urljoin(snapshot.final_url, next_href), expected_next)
        return PageValidation(self._page_id(resource_url), classification.kind, decision.kind, "", content, text_length, paragraphs, matches, classification.kind in {PageKind.AUTH_OR_CHALLENGE, PageKind.ERROR})

    def _draft(self, index: PageSnapshot, index_decision: AdaptationDecision, chapter: PageSnapshot, chapter_decision: AdaptationDecision) -> ConfigDraft:
        fields = (*index_decision.fields, *chapter_decision.fields)
        return ConfigDraft("draft-v1", urlsplit(index.final_url).hostname or "redacted", {item.field.value: item.score for item in fields}, {item.field.value: item.best_selector for item in fields if item.best_selector})

    @staticmethod
    def _selector(decision: AdaptationDecision, field: FieldKind) -> str | None:
        return next((item.best_selector for item in decision.fields if item.field is field), None)

    @staticmethod
    def _strip_sensitive(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    @staticmethod
    def _same_resource(left: str, right: str) -> bool:
        return ProbeService._strip_sensitive(left) == ProbeService._strip_sensitive(right)

    @staticmethod
    def _page_id(url: str) -> str:
        return hashlib.sha256(ProbeService._strip_sensitive(url).encode()).hexdigest()[:16]

    @staticmethod
    def _terminal(decision: AdaptationDecision, reason: str = "page_rejected") -> ValidationResult:
        return ValidationResult(False, 0.0, (reason,), (decision.kind,), {"pages": 1, "failures": 1}, None)

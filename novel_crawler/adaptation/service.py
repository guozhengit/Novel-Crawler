"""Bounded, read-only orchestration for scored site probing."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from soupsieve.util import SelectorSyntaxError

from novel_crawler.acquisition.classifier import Classification, PageClassifier, PageKind
from novel_crawler.acquisition.http import AcquisitionError, HttpPageAcquirer
from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot

from .config_schema import validate_candidate_selectors
from .decision import AdaptationDecision, DecisionKind, DecisionPolicy, FieldDecision, ScoredPageBatch
from .extractor import CandidateExtractor
from .fingerprint import fingerprint_html
from .models import ExtractionResult, FieldKind
from .scoring import CandidateScorer, ScoredCandidate, ScoringContext
from .url_paths import canonical_path
from .validation import ConfigDraft, MultiPageValidator, PageValidation, ValidationResult


class PageAcquirer(Protocol):
    def fetch_page(self, url: str, *, max_body_bytes: int | None = None, locked_origin: str | None = None) -> AcquiredPage: ...


@dataclass(frozen=True)
class _Analysis:
    classification: Classification
    extraction: ExtractionResult
    scored: tuple[ScoredCandidate, ...]
    decision: AdaptationDecision


@dataclass
class _ProbeRunContext:
    origin: tuple[str, str, int]
    fingerprint_salt: bytes
    fetches: int = 0
    bytes_read: int = 0


class ProbeService:
    def __init__(self, acquirer: PageAcquirer | None = None, classifier: PageClassifier | None = None, extractor: CandidateExtractor | None = None, scorer: CandidateScorer | None = None, decision_policy: DecisionPolicy | None = None, validator: MultiPageValidator | None = None, max_pages: int = 3, max_probe_bytes: int = 20_000) -> None:
        if max_pages < 3 or max_probe_bytes <= 0:
            raise ValueError("probe budgets must be positive and permit three pages")
        self.max_pages = 3
        self.max_probe_bytes = min(20 * 1024, max_probe_bytes)
        self.acquirer = acquirer or HttpPageAcquirer(max_body_bytes=self.max_probe_bytes)
        self.classifier = classifier or PageClassifier()
        self.extractor = extractor or CandidateExtractor()
        self.scorer = scorer or CandidateScorer()
        self.decision_policy = decision_policy or DecisionPolicy()
        self.validator = validator or MultiPageValidator()
    def probe(self, book_or_chapter_url: str, *, overrides: Mapping[str, str] | None = None) -> ValidationResult:
        try:
            try:
                context = _ProbeRunContext(self._origin_key(book_or_chapter_url), secrets.token_bytes(32))
                validated_overrides = dict(overrides or {})
                validate_candidate_selectors(validated_overrides)
                if not set(validated_overrides) <= {field.value for field in FieldKind}:
                    raise ValueError("selector override contains an unknown field")
            except ValueError:
                return self._safe_failure("probe_invalid_url")
            start = self._fetch(book_or_chapter_url, context)
            start_analysis = self._analyze(start.snapshot)
            if start_analysis.classification.kind not in {PageKind.BOOK_INDEX, PageKind.CHAPTER}:
                return self._reject(start_analysis.decision, "page_rejected")
            index = start
            index_analysis = start_analysis
            prefetched: AcquiredPage | None = None
            if start_analysis.classification.kind is PageKind.CHAPTER:
                prefetched = start
                index_href = self._selected_href(start, validated_overrides.get(FieldKind.INDEX_LINK.value)) or self._candidate_href(start, start_analysis.extraction, FieldKind.INDEX_LINK)
                if not index_href:
                    return self._reject(start_analysis.decision, "missing_index_link")
                index = self._fetch(urljoin(start.navigation_url, index_href), context)
                index_analysis = self._analyze(index.snapshot)
            if index_analysis.classification.kind is not PageKind.BOOK_INDEX:
                return self._reject(index_analysis.decision, "catalog_order_invalid")
            links = self._chapter_links(index, index_analysis.decision, validated_overrides.get(FieldKind.CHAPTER_LIST.value))
            if len(links) < 2 or len(set(map(self._canonical, links))) != len(links):
                return self._reject(index_analysis.decision, "catalog_order_invalid")
            if prefetched:
                current = self._catalog_key(prefetched.navigation_url)
                position = next((i for i, link in enumerate(links) if self._catalog_key(link) == current), -1)
                if position < 0:
                    return self._reject(index_analysis.decision, "chapter_not_in_catalog")
                neighbor = position + 1 if position + 1 < len(links) else position - 1
                first, second = (prefetched, self._fetch(links[neighbor], context)) if neighbor > position else (self._fetch(links[neighbor], context), prefetched)
            else:
                first, second = self._fetch(links[0], context), self._fetch(links[1], context)
            first_analysis, second_analysis = self._analyze(first.snapshot), self._analyze(second.snapshot)
            index_identity = self._book_identity(index.snapshot.html)
            first_item = self._page_validation(first, first_analysis, second.navigation_url, index_identity, validated_overrides)
            second_item = self._page_validation(second, second_analysis, None, index_identity, validated_overrides)
            draft = self._draft(index, index_analysis, first_analysis, second_analysis, first, second, first_item, second_item, context, validated_overrides)
            if draft is None:
                return self._safe_failure("selector_not_reusable")
            return self.validator.validate(first_item, second_item, draft, index_decision=index_analysis.decision.kind)
        except AcquisitionError as exc:
            return self._safe_failure(f"acquisition.{exc.code}")
        except (SelectorSyntaxError, ValueError, TypeError, RuntimeError):
            return self._safe_failure("probe_invalid_content")

    def _fetch(self, url: str, context: _ProbeRunContext) -> AcquiredPage:
        if context.fetches >= self.max_pages:
            raise RuntimeError("probe page limit")
        requested_origin = self._origin_key(url)
        if requested_origin != context.origin:
            raise AcquisitionError("cross_origin", self._origin_display(url), False)
        remaining = self.max_probe_bytes - context.bytes_read
        page = self.acquirer.fetch_page(url, max_body_bytes=remaining, locked_origin=self._origin_display(url))
        actual_origin = self._origin_key(page.navigation_url)
        if actual_origin != context.origin:
            raise AcquisitionError("cross_origin", self._origin_display(page.navigation_url), False)
        size = len(page.snapshot.body)
        if size > self.max_probe_bytes or context.bytes_read + size > self.max_probe_bytes:
            raise ValueError("probe byte budget")
        context.fetches += 1
        context.bytes_read += size
        return page

    def _analyze(self, snapshot: PageSnapshot) -> _Analysis:
        classification = self.classifier.classify(snapshot)
        extraction = self.extractor.extract(snapshot, classification.kind)
        context = ScoringContext(classification.kind, snapshot)
        values = tuple(self.scorer.score(item, context) for item in extraction)
        decision = self.decision_policy.decide(classification, ScoredPageBatch(context.sample_id, context.origin_key, classification.kind, values))
        return _Analysis(classification, extraction, values, decision)

    def _chapter_links(self, index: AcquiredPage, decision: AdaptationDecision, override: str | None = None) -> list[str]:
        selector = override or self._selector(decision, FieldKind.CHAPTER_LIST)
        if not selector:
            return []
        soup = BeautifulSoup(index.snapshot.html, "lxml")
        return [urljoin(index.navigation_url, str(node.get("href"))) for node in soup.select(selector) if isinstance(node, Tag) and node.get("href")]

    @staticmethod
    def _candidate_href(page: AcquiredPage, extraction: ExtractionResult, field: FieldKind) -> str | None:
        soup = BeautifulSoup(page.snapshot.html, "lxml")
        for candidate in sorted(extraction.for_field(field), key=lambda item: (-item.raw_score, item.selector)):
            node = soup.select_one(candidate.selector)
            if isinstance(node, Tag) and node.get("href"):
                return str(node.get("href"))
        return None

    @staticmethod
    def _selected_href(page: AcquiredPage, selector: str | None) -> str | None:
        if not selector:
            return None
        nodes = BeautifulSoup(page.snapshot.html, "lxml").select(selector)
        return str(nodes[0].get("href")) if len(nodes) == 1 and isinstance(nodes[0], Tag) and nodes[0].get("href") else None

    def _page_validation(self, page: AcquiredPage, analysis: _Analysis, expected_next: str | None, index_identity: str | None, overrides: Mapping[str, str]) -> PageValidation:
        content = overrides.get(FieldKind.CONTENT.value) or self._selector(analysis.decision, FieldKind.CONTENT) or ""
        soup = BeautifulSoup(page.snapshot.html, "lxml")
        nodes = soup.select(content) if content else []
        node = nodes[0] if len(nodes) == 1 and isinstance(nodes[0], Tag) else None
        next_href = self._selected_href(page, overrides.get(FieldKind.NEXT_LINK.value)) or self._candidate_href(page, analysis.extraction, FieldKind.NEXT_LINK)
        matches = expected_next is None or next_href is not None and self._canonical(urljoin(page.navigation_url, next_href)) == self._canonical(expected_next)
        fingerprint = self._fingerprint(node) if node else ""
        page_identity = self._book_identity(page.snapshot.html)
        identity_matches = index_identity is None or page_identity is None or index_identity == page_identity
        return PageValidation(self._page_id(page.navigation_url), analysis.classification.kind, analysis.decision.kind, identity_matches, content, len(node.get_text(" ", strip=True)) if node else 0, len(node.find_all("p")) if node else 0, matches, analysis.classification.kind in {PageKind.AUTH_OR_CHALLENGE, PageKind.ERROR}, fingerprint)

    @staticmethod
    def _fingerprint(node: Tag) -> str:
        ancestry = [parent.name for parent in list(node.parents)[:2] if isinstance(parent, Tag)]
        role = str(node.get("role", ""))
        stable = next((str(value) for value in [node.get("id"), *node.get("class", [])] if value and not re.search(r"\d", str(value))), "")
        return "/".join([*reversed(ancestry), node.name, role, stable])

    @staticmethod
    def _book_identity(html: str) -> str | None:
        soup = BeautifulSoup(html, "lxml")
        meta = soup.select_one('meta[property="og:novel:book_name"], meta[name="book_name"]')
        value = str(meta.get("content", "")) if isinstance(meta, Tag) else ""
        if not value:
            node = soup.select_one('[data-book-title], .book-title, .breadcrumb [itemprop="book"], .breadcrumb .book, .breadcrumb .book-name, .breadcrumb [itemprop="name"].book')
            value = node.get_text(" ", strip=True) if isinstance(node, Tag) else ""
        normalized = re.sub(r"\s+", " ", value).strip().casefold()
        return normalized or None

    def _draft(self, index: AcquiredPage, index_analysis: _Analysis, first_analysis: _Analysis, second_analysis: _Analysis, first_acquired: AcquiredPage, second_acquired: AcquiredPage, first_page: PageValidation, second_page: PageValidation, context: _ProbeRunContext, overrides: Mapping[str, str]) -> ConfigDraft | None:
        index_decision = index_analysis.decision
        first, second = first_analysis.decision, second_analysis.decision
        all_fields = (*index_decision.fields, *first.fields, *second.fields)
        grouped: dict[FieldKind, list[FieldDecision]] = {}
        for item in all_fields:
            grouped.setdefault(item.field, []).append(item)
        scores = {field.value: min(item.score for item in items) for field, items in grouped.items()}
        selectors = {field.value: items[0].best_selector for field, items in grouped.items() if all(item.best_selector == items[0].best_selector for item in items) and items[0].best_selector}
        first_candidates = {item.selector for item in first_analysis.extraction.for_field(FieldKind.CONTENT)}
        second_candidates = {item.selector for item in second_analysis.extraction.for_field(FieldKind.CONTENT)}
        if not first_page.content_selector or not second_page.content_selector:
            return None
        first_soup = BeautifulSoup(first_acquired.snapshot.html, "lxml")
        second_soup = BeautifulSoup(second_acquired.snapshot.html, "lxml")
        index_soup = BeautifulSoup(index.snapshot.html, "lxml")
        override_scores = self._override_scores(overrides, index_analysis, first_analysis, second_analysis, index_soup, first_soup, second_soup)
        if override_scores is None:
            return None
        scores.update(override_scores)
        first_selected = first_soup.select(first_page.content_selector)
        second_selected = second_soup.select(second_page.content_selector)
        if len(first_selected) != 1 or len(second_selected) != 1:
            return None
        if FieldKind.CONTENT.value in overrides:
            selectors[FieldKind.CONTENT.value] = overrides[FieldKind.CONTENT.value]
        else:
            reusable = []
            for selector in sorted(first_candidates & second_candidates):
                first_nodes = first_soup.select(selector)
                second_nodes = second_soup.select(selector)
                if len(first_nodes) == len(second_nodes) == 1 and first_nodes[0] is first_selected[0] and second_nodes[0] is second_selected[0]:
                    reusable.append(selector)
            if not reusable:
                return None
            selectors[FieldKind.CONTENT.value] = reusable[0]
        selectors.update(overrides)
        book_selectors = {key: value for key, value in selectors.items() if key in {"title", "author", "chapter_list"}}
        chapter_selectors = {key: value for key, value in selectors.items() if key not in book_selectors and key != "clean_selector"}
        fingerprints = {
            "book": fingerprint_html(index.snapshot.html, "book", book_selectors, context.fingerprint_salt),
            "chapter_first": fingerprint_html(first_acquired.snapshot.html, "chapter", chapter_selectors, context.fingerprint_salt),
            "chapter_second": fingerprint_html(second_acquired.snapshot.html, "chapter", chapter_selectors, context.fingerprint_salt),
        }
        return ConfigDraft(
            "draft-v1",
            urlsplit(index.navigation_url).hostname or "redacted",
            scores,
            selectors,
            fingerprints=fingerprints,
            fingerprint_salt=context.fingerprint_salt,
            navigation_paths=(index.navigation_url, first_acquired.navigation_url, second_acquired.navigation_url),
        )

    @staticmethod
    def _override_scores(overrides: Mapping[str, str], index_analysis: _Analysis, first_analysis: _Analysis, second_analysis: _Analysis, index: BeautifulSoup, first: BeautifulSoup, second: BeautifulSoup) -> dict[str, float] | None:
        book_fields = {"title", "author", "chapter_list"}
        optional_links = {"prev_link", "next_link", "index_link"}
        scores: dict[str, float] = {}
        pairs: tuple[tuple[BeautifulSoup, _Analysis], ...]
        for field, selector in overrides.items():
            try:
                field_kind = FieldKind(field)
            except ValueError:
                return None
            if field == "clean_selector":
                pairs = ((index, index_analysis), (first, first_analysis), (second, second_analysis))
            else:
                pairs = ((index, index_analysis),) if field in book_fields else ((first, first_analysis), (second, second_analysis))
            soups = tuple(pair[0] for pair in pairs)
            counts = [len(soup.select(selector)) for soup in soups]
            if field == "clean_selector":
                if not any(counts):
                    return None
            elif field == "chapter_list":
                if counts[0] < 2:
                    return None
            elif field in optional_links:
                if not any(counts) or any(count > 1 for count in counts):
                    return None
            elif any(count != 1 for count in counts):
                return None
            matched_scores = [
                item.score
                for (soup, analysis), count in zip(pairs, counts, strict=True)
                for item in analysis.scored
                if count and item.candidate.field is field_kind and item.candidate.selector == selector
            ]
            if sum(1 for count in counts if count) != len(matched_scores):
                return None
            if matched_scores:
                scores[field] = min(matched_scores)
        return scores

    @staticmethod
    def _origin_key(url: str) -> tuple[str, str, int]:
        try:
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            raw_host = parts.hostname or ""
            host = raw_host.encode("idna").decode("ascii").lower()
            port = parts.port or (443 if scheme == "https" else 80)
        except (UnicodeError, ValueError):
            raise ValueError("invalid origin") from None
        if scheme not in {"http", "https"} or not host:
            raise ValueError("invalid origin")
        return scheme, host, port

    @staticmethod
    def _origin_display(url: str) -> str:
        scheme, host, port = ProbeService._origin_key(url)
        default = 443 if scheme == "https" else 80
        return f"{scheme}://{host}" + (f":{port}" if port != default else "")

    @staticmethod
    def _selector(decision: AdaptationDecision, field: FieldKind) -> str | None:
        return next((item.best_selector for item in decision.fields if item.field is field), None)

    @staticmethod
    def _canonical(url: str) -> str:
        parts = urlsplit(url)
        scheme, host, port = ProbeService._origin_key(url)
        default = 443 if scheme == "https" else 80
        authority = host + (f":{port}" if port != default else "")
        return urlunsplit((scheme, authority, canonical_path(parts.path or "/"), parts.query, ""))

    @staticmethod
    def _catalog_key(url: str) -> str:
        parts = urlsplit(ProbeService._canonical(url))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    @staticmethod
    def _page_id(url: str) -> str:
        return hashlib.sha256(ProbeService._canonical(url).encode()).hexdigest()[:16]

    @staticmethod
    def _reject(decision: AdaptationDecision, reason: str) -> ValidationResult:
        return ValidationResult(False, 0, (reason,), (decision.kind,), {"pages": 1, "failures": 1}, None)

    @staticmethod
    def _safe_failure(reason: str) -> ValidationResult:
        return ValidationResult(False, 0, (reason,), (DecisionKind.REJECT,), {"pages": 0, "failures": 1}, None)

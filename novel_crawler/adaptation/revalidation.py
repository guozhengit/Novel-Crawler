"""Bounded replay of stored selectors against fresh, same-origin pages."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from soupsieve.util import SelectorSyntaxError

from novel_crawler.acquisition.classifier import Classification, PageClassifier, PageKind
from novel_crawler.acquisition.http import AcquisitionError, HttpPageAcquirer
from novel_crawler.acquisition.models import AcquiredPage

from .config_schema import SiteConfig
from .decision import AdaptationDecision, DecisionKind, DecisionPolicy, ScoredPageBatch
from .extractor import CandidateExtractor
from .fingerprint import StructureFingerprint, fingerprint_html
from .models import ExtractionResult
from .registry import ConfigConflictError, ConfigRegistry, ConfigStatus, RegistryEntry, RegistryError
from .scoring import CandidateScorer, ScoredCandidate, ScoringContext
from .url_paths import canonical_path

_SAFE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_FINGERPRINT_KINDS = {"book": PageKind.BOOK_INDEX, "chapter_first": PageKind.CHAPTER, "chapter_second": PageKind.CHAPTER}
_CORE_FIELDS = frozenset({"title", "chapter_list", "chapter_title", "content"})
_OPTIONAL_ANCHORS = frozenset({"prev_link", "next_link"})


class RevalidationStatus(StrEnum):
    VALID = "valid"
    STALE = "stale"
    TRANSIENT_FAILURE = "transient_failure"
    INVALID = "invalid"


class RevalidationResult:
    """Immutable public result containing structural summaries only."""

    __slots__ = ("_checked_at", "_entry", "_field_scores", "_fingerprint_matches", "_reason_ids", "_status")

    def __init__(
        self,
        status: RevalidationStatus,
        reason_ids: tuple[str, ...],
        field_scores: Mapping[str, float],
        fingerprint_matches: Mapping[str, bool],
        checked_at: str,
        entry: RegistryEntry | None = None,
    ) -> None:
        if not isinstance(status, RevalidationStatus):
            raise TypeError("status must be RevalidationStatus")
        reasons = tuple(dict.fromkeys(reason_ids))
        if not all(isinstance(item, str) and _SAFE_ID.fullmatch(item) for item in reasons):
            raise ValueError("reason_ids must be safe identifiers")
        scores = dict(field_scores)
        if not all(
            isinstance(key, str)
            and _SAFE_ID.fullmatch(key)
            and isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
            and 0 <= value <= 1
            for key, value in scores.items()
        ):
            raise ValueError("field_scores must be finite and bounded")
        matches = dict(fingerprint_matches)
        if not all(isinstance(key, str) and key in _FINGERPRINT_KINDS and isinstance(value, bool) for key, value in matches.items()):
            raise ValueError("fingerprint_matches must use supported page kinds")
        try:
            parsed = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("checked_at must be an ISO timestamp") from exc
        if parsed.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        object.__setattr__(self, "_status", status)
        object.__setattr__(self, "_reason_ids", reasons)
        object.__setattr__(self, "_field_scores", MappingProxyType({key: float(value) for key, value in scores.items()}))
        object.__setattr__(self, "_fingerprint_matches", MappingProxyType(matches))
        object.__setattr__(self, "_checked_at", checked_at)
        object.__setattr__(self, "_entry", entry)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("RevalidationResult is immutable")

    status = property(lambda self: self._status)
    reason_ids = property(lambda self: self._reason_ids)
    field_scores = property(lambda self: self._field_scores)
    fingerprint_matches = property(lambda self: self._fingerprint_matches)
    checked_at = property(lambda self: self._checked_at)
    entry = property(lambda self: self._entry)

    def __repr__(self) -> str:
        return (
            f"RevalidationResult(status={self.status.value!r}, reason_ids={self.reason_ids!r}, "
            f"field_count={len(self.field_scores)}, fingerprint_matches={dict(self.fingerprint_matches)!r}, "
            f"checked_at={self.checked_at!r})"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reason_ids": list(self.reason_ids),
            "field_scores": dict(self.field_scores),
            "fingerprint_matches": dict(self.fingerprint_matches),
            "checked_at": self.checked_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class PageAcquirer(Protocol):
    def fetch_page(
        self, url: str, *, max_body_bytes: int | None = None, locked_origin: str | None = None
    ) -> AcquiredPage: ...


@dataclass(frozen=True)
class _AnalyzedPage:
    acquired: AcquiredPage
    classification: Classification
    extraction: ExtractionResult
    scored: tuple[ScoredCandidate, ...]
    decision: AdaptationDecision


class _AuthRequired(RuntimeError):
    pass


class _HardPageError(RuntimeError):
    pass


class ConfigRevalidator:
    """Revalidate an existing config without widening the probe surface."""

    def __init__(
        self,
        acquirer: PageAcquirer | None = None,
        classifier: PageClassifier | None = None,
        extractor: CandidateExtractor | None = None,
        scorer: CandidateScorer | None = None,
        decision: DecisionPolicy | None = None,
        registry: ConfigRegistry | None = None,
        *,
        max_pages: int = 3,
        max_revalidation_bytes: int = 20 * 1024,
        minimum_score: float = 0.85,
        minor_drift_tolerance: float = 0.0,
    ) -> None:
        if registry is None:
            raise TypeError("registry is required")
        if max_pages < 3 or max_revalidation_bytes <= 0:
            raise ValueError("revalidation budgets must permit three bounded pages")
        if not math.isfinite(minimum_score) or not 0 <= minimum_score <= 1:
            raise ValueError("minimum_score must be finite and bounded")
        if not math.isfinite(minor_drift_tolerance) or not 0 <= minor_drift_tolerance <= 1:
            raise ValueError("minor_drift_tolerance must be finite and bounded")
        self.acquirer = acquirer or HttpPageAcquirer(max_body_bytes=min(20 * 1024, max_revalidation_bytes))
        self.classifier = classifier or PageClassifier()
        self.extractor = extractor or CandidateExtractor()
        self.scorer = scorer or CandidateScorer()
        self.decision = decision or DecisionPolicy()
        self.registry = registry
        self.max_pages = 3
        self.max_revalidation_bytes = min(20 * 1024, max_revalidation_bytes)
        self.minimum_score = float(minimum_score)
        self.minor_drift_tolerance = float(minor_drift_tolerance)
        self._fetches = 0
        self._bytes = 0
        self._origin: tuple[str, str, int] | None = None

    def revalidate(self, entry: RegistryEntry, input_url: str) -> RevalidationResult:
        checked_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        self._fetches = self._bytes = 0
        if entry.status is ConfigStatus.REVOKED:
            return RevalidationResult(RevalidationStatus.INVALID, ("config_revoked",), {}, {}, checked_at)
        try:
            config = self.registry.load(entry)
        except (KeyError, RegistryError, TypeError, ValueError):
            return self._invalid(entry, ("config_invalid",), checked_at)
        if config.config_id != entry.config_id or config.domain != entry.domain:
            return self._invalid(entry, ("config_identity_mismatch",), checked_at)
        if self._fingerprint_baselines(config) is None:
            return self._stale(entry, ("fingerprint_baseline_missing",), {}, {}, checked_at)
        try:
            self._origin = self._origin_key(input_url)
        except ValueError:
            return self._invalid(entry, ("input_url_invalid",), checked_at)
        if self._origin[1] != config.domain:
            return self._invalid(entry, ("domain_mismatch",), checked_at)
        try:
            start = self._fetch(input_url)
            index, first, second = self._collect_pages(start, config)
            scores: dict[str, float] = {}
            self._replay(config, "book", index, scores)
            self._replay(config, "chapter", first, scores)
            self._replay(config, "chapter", second, scores)
            decisions_are_high_confidence = all(
                page.decision.kind is DecisionKind.AUTO_ACCEPT for page in (index, first, second)
            )
            required_fields = _CORE_FIELDS | ({"clean_selector"} if config.selectors["clean"] else set())
            low = tuple(
                sorted(
                    key
                    for key in required_fields
                    if scores.get(key, 0.0) < self.minimum_score
                    or scores.get(key, 0.0)
                    < float(config.field_scores.get(key, scores.get(key, 0.0))) - self.minor_drift_tolerance
                )
            )
            if low or not decisions_are_high_confidence:
                return self._stale(entry, ("score_below_threshold",), scores, {}, checked_at)
            matches = self._fingerprint_matches(config, index, first, second)
            if not matches or not all(matches.values()):
                return self._stale(entry, ("fingerprint_mismatch",), scores, matches, checked_at)
            validated_entry = self._mark_valid(entry, checked_at)
            if validated_entry is None:
                return self._conflict_result(entry, scores, matches, checked_at)
            return RevalidationResult(RevalidationStatus.VALID, (), scores, matches, checked_at, validated_entry)
        except _AuthRequired:
            return self._stale(entry, ("auth_required",), {}, {}, checked_at)
        except _HardPageError:
            return self._invalid(entry, ("hard_error",), checked_at)
        except AcquisitionError as exc:
            if exc.recoverable or exc.code in {"timeout", "http_408", "http_429"} or exc.code.startswith("http_5"):
                return RevalidationResult(RevalidationStatus.TRANSIENT_FAILURE, ("acquisition_transient",), {}, {}, checked_at)
            return self._invalid(entry, ("hard_error",), checked_at)
        except (SelectorSyntaxError, TypeError, ValueError):
            return self._invalid(entry, ("selector_match_invalid",), checked_at)

    def _collect_pages(
        self, start: _AnalyzedPage, config: SiteConfig
    ) -> tuple[_AnalyzedPage, _AnalyzedPage, _AnalyzedPage]:
        book = self._selector_group(config, "book")
        links = self._catalog_links(start.acquired, book.get("chapter_list", "")) if start.classification.kind is PageKind.BOOK_INDEX else []
        prefetched: _AnalyzedPage | None = None
        if start.classification.kind is PageKind.BOOK_INDEX and links:
            index = start
        elif start.classification.kind is PageKind.CHAPTER:
            chapter = self._selector_group(config, "chapter")
            index_selector = chapter.get("index_link")
            if not index_selector:
                raise ValueError("chapter config requires index_link")
            index_url = self._single_anchor(start.acquired, index_selector, required=True)
            assert index_url is not None
            prefetched = start
            index = self._fetch(index_url)
            if index.classification.kind is not PageKind.BOOK_INDEX:
                raise ValueError("index page kind is invalid")
            links = self._catalog_links(index.acquired, book.get("chapter_list", ""))
        else:
            raise ValueError("start page kind is unsupported")
        if len(links) < 2:
            raise ValueError("catalog requires adjacent chapters")
        if prefetched is None:
            return index, self._fetch(links[0]), self._fetch(links[1])
        current = self._canonical(prefetched.acquired.navigation_url)
        position = next((number for number, link in enumerate(links) if self._canonical(link) == current), -1)
        if position < 0:
            raise ValueError("chapter missing from catalog")
        neighbor = position + 1 if position + 1 < len(links) else position - 1
        other = self._fetch(links[neighbor])
        return (index, prefetched, other) if neighbor > position else (index, other, prefetched)

    def _replay(self, config: SiteConfig, kind: str, page: _AnalyzedPage, scores: dict[str, float]) -> None:
        expected_kind = PageKind.BOOK_INDEX if kind == "book" else PageKind.CHAPTER
        if page.classification.kind is not expected_kind:
            raise ValueError("page kind does not satisfy replay contract")
        selectors = self._selector_group(config, kind)
        snapshot = page.acquired.snapshot
        soup = BeautifulSoup(snapshot.html, "lxml")
        for clean_selector in config.selectors["clean"]:
            selector = str(clean_selector)
            nodes = soup.select(selector)
            if not nodes:
                continue
            scored_clean = next(
                (
                    item
                    for item in page.scored
                    if item.candidate.field.value == "clean_selector" and item.candidate.selector == selector
                ),
                None,
            )
            if scored_clean is None:
                raise ValueError("saved clean selector is absent from extracted candidates")
            scores["clean_selector"] = min(scores.get("clean_selector", 1.0), scored_clean.score)
        for field, selector in selectors.items():
            nodes = soup.select(selector)
            if field == "chapter_list":
                if len(nodes) < 2 or any(not isinstance(node, Tag) or node.name != "a" or not node.get("href") for node in nodes):
                    raise ValueError("invalid chapter list selector")
            elif field in _OPTIONAL_ANCHORS:
                if len(nodes) > 1 or any(not isinstance(node, Tag) or node.name != "a" or not node.get("href") for node in nodes):
                    raise ValueError("invalid optional anchor selector")
                if not nodes:
                    continue
            elif len(nodes) != 1:
                raise ValueError("selector must match uniquely")
            elif field.endswith("_link") and (
                not isinstance(nodes[0], Tag) or nodes[0].name != "a" or not nodes[0].get("href")
            ):
                raise ValueError("navigation selector must match an anchor")
            scored = next(
                (
                    item
                    for item in page.scored
                    if item.candidate.field.value == field and item.candidate.selector == selector
                ),
                None,
            )
            if scored is None:
                raise ValueError("saved selector is absent from extracted candidates")
            score = scored.score
            scores[field] = min(scores.get(field, 1.0), score)

    def _fingerprint_matches(
        self,
        config: SiteConfig,
        index: _AnalyzedPage,
        first: _AnalyzedPage,
        second: _AnalyzedPage,
    ) -> dict[str, bool]:
        expected = self._fingerprint_baselines(config)
        if expected is None:
            return {}
        matches: dict[str, bool] = {}
        for label, page in (("book", index), ("chapter_first", first), ("chapter_second", second)):
            kind = "book" if label == "book" else "chapter"
            candidates = self._selector_group(config, kind)
            actual = fingerprint_html(page.acquired.snapshot.html, kind, candidates, config.fingerprint_salt)
            matches[label] = actual == expected[label]
        return matches

    @staticmethod
    def _fingerprint_baselines(config: SiteConfig) -> dict[str, StructureFingerprint] | None:
        expected: dict[str, StructureFingerprint] = {}
        for sample in config.validation_samples:
            label, raw = sample.get("page_kind"), sample.get("fingerprint")
            if isinstance(label, str) and label in _FINGERPRINT_KINDS and isinstance(raw, Mapping):
                expected[label] = StructureFingerprint.from_dict(raw)
        return expected if set(expected) == set(_FINGERPRINT_KINDS) else None

    def _fetch(self, url: str) -> _AnalyzedPage:
        if self._fetches >= self.max_pages:
            raise ValueError("revalidation page budget exceeded")
        if self._origin_key(url) != self._origin:
            raise AcquisitionError("cross_origin", self._origin_display(url), False)
        remaining = self.max_revalidation_bytes - self._bytes
        if remaining <= 0:
            raise AcquisitionError("response_too_large", self._origin_display(url), False)
        page = self.acquirer.fetch_page(url, max_body_bytes=remaining, locked_origin=self._origin_display(url))
        if self._origin_key(page.navigation_url) != self._origin:
            raise AcquisitionError("cross_origin", self._origin_display(page.navigation_url), False)
        size = len(page.snapshot.body)
        if size > remaining:
            raise AcquisitionError("response_too_large", self._origin_display(url), False)
        self._fetches += 1
        self._bytes += size
        return self._analyze(page)

    def _analyze(self, page: AcquiredPage) -> _AnalyzedPage:
        snapshot = page.snapshot
        classification = self.classifier.classify(snapshot)
        if classification.kind is PageKind.AUTH_OR_CHALLENGE:
            raise _AuthRequired
        if classification.kind is PageKind.ERROR:
            raise _HardPageError
        extraction = self.extractor.extract(snapshot, classification.kind)
        context = ScoringContext(classification.kind, snapshot)
        scored = tuple(self.scorer.score(candidate, context) for candidate in extraction)
        decision = self.decision.decide(
            classification,
            ScoredPageBatch(context.sample_id, context.origin_key, classification.kind, scored),
        )
        return _AnalyzedPage(page, classification, extraction, scored, decision)

    def _catalog_links(self, page: AcquiredPage, selector: str) -> list[str]:
        if not selector:
            raise ValueError("chapter_list selector is required")
        soup = BeautifulSoup(page.snapshot.html, "lxml")
        nodes = soup.select(selector)
        if len(nodes) < 2:
            return []
        links: list[str] = []
        for node in nodes:
            if not isinstance(node, Tag) or node.name != "a" or not node.get("href"):
                raise ValueError("chapter list must contain anchors")
            link = urljoin(page.navigation_url, str(node.get("href")))
            if self._origin_key(link) != self._origin:
                raise AcquisitionError("cross_origin", self._origin_display(link), False)
            links.append(link)
        canonical = [self._canonical(link) for link in links]
        if len(set(canonical)) != len(canonical):
            raise ValueError("chapter links must be unique")
        return links

    def _single_anchor(self, page: AcquiredPage, selector: str, *, required: bool) -> str | None:
        nodes = BeautifulSoup(page.snapshot.html, "lxml").select(selector)
        if not nodes and not required:
            return None
        if len(nodes) != 1 or not isinstance(nodes[0], Tag) or nodes[0].name != "a" or not nodes[0].get("href"):
            raise ValueError("navigation selector must match one anchor")
        link = urljoin(page.navigation_url, str(nodes[0].get("href")))
        if self._origin_key(link) != self._origin:
            raise AcquisitionError("cross_origin", self._origin_display(link), False)
        return link

    @staticmethod
    def _selector_group(config: SiteConfig, kind: str) -> dict[str, str]:
        raw = config.selectors[kind]
        if not isinstance(raw, Mapping) or not all(isinstance(key, str) and isinstance(value, str) for key, value in raw.items()):
            raise ValueError("selector group is invalid")
        return dict(raw)

    def _mark_valid(self, entry: RegistryEntry, checked_at: str) -> RegistryEntry | None:
        try:
            transitioned = self.registry.mark_validated(
                entry.config_id,
                checked_at,
                expected_version=entry.version,
                expected_status=entry.status,
            )
            return transitioned or RegistryEntry(entry.config_id, entry.domain, ConfigStatus.ACTIVE, entry.version + 1, entry.created, checked_at)
        except ConfigConflictError:
            self.registry.load(entry.config_id)
            return None

    def _stale(
        self,
        entry: RegistryEntry,
        reasons: tuple[str, ...],
        scores: Mapping[str, float],
        matches: Mapping[str, bool],
        checked_at: str,
    ) -> RevalidationResult:
        try:
            transitioned = self.registry.mark_stale(
                entry.config_id,
                expected_version=entry.version,
                expected_status=entry.status,
            )
        except ConfigConflictError:
            self.registry.load(entry.config_id)
            reasons = (*reasons, "concurrent_revision")
            transitioned = None
        return RevalidationResult(RevalidationStatus.STALE, reasons, scores, matches, checked_at, transitioned)

    def _invalid(self, entry: RegistryEntry, reasons: tuple[str, ...], checked_at: str) -> RevalidationResult:
        try:
            transitioned = self.registry.mark_invalid(
                entry.config_id,
                reasons,
                expected_version=entry.version,
                expected_status=entry.status,
            )
        except (ConfigConflictError, KeyError, RegistryError):
            try:
                self.registry.load(entry.config_id)
            except (KeyError, RegistryError, TypeError, ValueError):
                pass
            reasons = (*reasons, "concurrent_revision")
            transitioned = None
        return RevalidationResult(RevalidationStatus.INVALID, reasons, {}, {}, checked_at, transitioned)

    def _conflict_result(
        self,
        entry: RegistryEntry,
        scores: Mapping[str, float],
        matches: Mapping[str, bool],
        checked_at: str,
    ) -> RevalidationResult:
        del entry
        return RevalidationResult(RevalidationStatus.STALE, ("concurrent_revision",), scores, matches, checked_at)

    @staticmethod
    def _origin_key(url: str) -> tuple[str, str, int]:
        try:
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
            port = parts.port or (443 if scheme == "https" else 80)
        except (UnicodeError, ValueError):
            raise ValueError("invalid origin") from None
        if scheme not in {"http", "https"} or not host or parts.username or parts.password:
            raise ValueError("invalid origin")
        return scheme, host, port

    @staticmethod
    def _origin_display(url: str) -> str:
        scheme, host, port = ConfigRevalidator._origin_key(url)
        default = 443 if scheme == "https" else 80
        return f"{scheme}://{host}" + (f":{port}" if port != default else "")

    @staticmethod
    def _canonical(url: str) -> str:
        scheme, host, port = ConfigRevalidator._origin_key(url)
        parts = urlsplit(url)
        default = 443 if scheme == "https" else 80
        authority = host + (f":{port}" if port != default else "")
        return f"{scheme}://{authority}{canonical_path(parts.path or '/')}" + (f"?{parts.query}" if parts.query else "")


__all__ = ["ConfigRevalidator", "RevalidationResult", "RevalidationStatus"]

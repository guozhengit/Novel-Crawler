"""Bounded replay of stored selectors against fresh, same-origin pages."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from soupsieve.util import SelectorSyntaxError

from novel_crawler.acquisition.classifier import PageClassifier, PageKind
from novel_crawler.acquisition.http import AcquisitionError, HttpPageAcquirer
from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot

from .config_schema import SiteConfig
from .fingerprint import fingerprint_html
from .models import Candidate, FieldKind
from .registry import ConfigConflictError, ConfigRegistry, RegistryEntry, RegistryError
from .scoring import CandidateScorer, ScoringContext

_SAFE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_FINGERPRINT_KINDS = {"book": PageKind.BOOK_INDEX, "chapter": PageKind.CHAPTER}
_FIELD_KINDS = {item.value: item for item in FieldKind}
_CORE_FIELDS = frozenset({"title", "chapter_list", "chapter_title", "content"})
_OPTIONAL_ANCHORS = frozenset({"prev_link", "next_link"})


class RevalidationStatus(StrEnum):
    VALID = "valid"
    STALE = "stale"
    TRANSIENT_FAILURE = "transient_failure"
    INVALID = "invalid"


class RevalidationResult:
    """Immutable public result containing structural summaries only."""

    __slots__ = ("_checked_at", "_field_scores", "_fingerprint_matches", "_reason_ids", "_status")

    def __init__(
        self,
        status: RevalidationStatus,
        reason_ids: tuple[str, ...],
        field_scores: Mapping[str, float],
        fingerprint_matches: Mapping[str, bool],
        checked_at: str,
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

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("RevalidationResult is immutable")

    status = property(lambda self: self._status)
    reason_ids = property(lambda self: self._reason_ids)
    field_scores = property(lambda self: self._field_scores)
    fingerprint_matches = property(lambda self: self._fingerprint_matches)
    checked_at = property(lambda self: self._checked_at)

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


class ConfigRevalidator:
    """Revalidate an existing config without widening the probe surface."""

    def __init__(
        self,
        acquirer: PageAcquirer | None = None,
        classifier: object | None = None,
        extractor: object | None = None,
        scorer: object | None = None,
        decision: object | None = None,
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
        self.extractor = extractor
        self.scorer = scorer or CandidateScorer()
        self.decision = decision
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
        try:
            config = self.registry.load(entry)
        except (KeyError, RegistryError, TypeError, ValueError):
            return self._invalid(entry, ("config_invalid",), checked_at)
        if config.config_id != entry.config_id or config.domain != entry.domain:
            return self._invalid(entry, ("config_identity_mismatch",), checked_at)
        try:
            self._origin = self._origin_key(input_url)
        except ValueError:
            return self._invalid(entry, ("input_url_invalid",), checked_at)
        if self._origin[1] != config.domain:
            return self._invalid(entry, ("domain_mismatch",), checked_at)
        try:
            start = self._fetch(input_url)
            terminal = self._terminal_status(start.snapshot)
            if terminal is RevalidationStatus.STALE:
                return self._stale(entry, ("auth_required",), {}, {}, checked_at)
            if terminal is RevalidationStatus.INVALID:
                return self._invalid(entry, ("hard_error",), checked_at)

            index, first, second = self._collect_pages(start, config)
            scores: dict[str, float] = {}
            self._replay(config, "book", index.snapshot, scores)
            self._replay(config, "chapter", first.snapshot, scores)
            self._replay(config, "chapter", second.snapshot, scores)
            low = tuple(
                sorted(
                    key
                    for key in _CORE_FIELDS
                    if scores.get(key, 0.0) < self.minimum_score
                    or scores.get(key, 0.0)
                    < float(config.field_scores.get(key, scores.get(key, 0.0))) - self.minor_drift_tolerance
                )
            )
            if low:
                return self._stale(entry, ("score_below_threshold",), scores, {}, checked_at)
            matches = self._fingerprint_matches(config, index.snapshot, first.snapshot)
            if not matches or not all(matches.values()):
                return self._stale(entry, ("fingerprint_mismatch",), scores, matches, checked_at)
            if not self._mark_valid(entry, checked_at):
                return self._conflict_result(entry, scores, matches, checked_at)
            return RevalidationResult(RevalidationStatus.VALID, (), scores, matches, checked_at)
        except AcquisitionError as exc:
            if exc.recoverable or exc.code in {"timeout", "http_408", "http_429"} or exc.code.startswith("http_5"):
                return RevalidationResult(RevalidationStatus.TRANSIENT_FAILURE, ("acquisition_transient",), {}, {}, checked_at)
            return self._invalid(entry, ("hard_error",), checked_at)
        except (SelectorSyntaxError, TypeError, ValueError):
            return self._invalid(entry, ("selector_match_invalid",), checked_at)

    def _collect_pages(self, start: AcquiredPage, config: SiteConfig) -> tuple[AcquiredPage, AcquiredPage, AcquiredPage]:
        book = self._selector_group(config, "book")
        links = self._catalog_links(start, book.get("chapter_list", ""))
        prefetched: AcquiredPage | None = None
        if links:
            index = start
        else:
            chapter = self._selector_group(config, "chapter")
            index_selector = chapter.get("index_link")
            if not index_selector:
                raise ValueError("chapter config requires index_link")
            index_url = self._single_anchor(start, index_selector, required=True)
            assert index_url is not None
            prefetched = start
            index = self._fetch(index_url)
            links = self._catalog_links(index, book.get("chapter_list", ""))
        if len(links) < 2:
            raise ValueError("catalog requires adjacent chapters")
        if prefetched is None:
            return index, self._fetch(links[0]), self._fetch(links[1])
        current = self._canonical(prefetched.navigation_url)
        position = next((number for number, link in enumerate(links) if self._canonical(link) == current), -1)
        if position < 0:
            raise ValueError("chapter missing from catalog")
        neighbor = position + 1 if position + 1 < len(links) else position - 1
        other = self._fetch(links[neighbor])
        return (index, prefetched, other) if neighbor > position else (index, other, prefetched)

    def _replay(self, config: SiteConfig, kind: str, snapshot: PageSnapshot, scores: dict[str, float]) -> None:
        selectors = self._selector_group(config, kind)
        soup = BeautifulSoup(snapshot.html, "lxml")
        for clean_selector in config.selectors["clean"]:
            soup.select(str(clean_selector))
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
            score = self._score(field, selector, snapshot, _FINGERPRINT_KINDS[kind], len(nodes))
            scores[field] = min(scores.get(field, 1.0), score)

    def _score(self, field: str, selector: str, snapshot: PageSnapshot, kind: PageKind, count: int) -> float:
        direct = getattr(self.scorer, "score_selector", None)
        if callable(direct):
            value = direct(field, selector, snapshot, kind)
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise TypeError("score_selector returned an invalid score")
            return max(0.0, min(1.0, float(value)))
        field_kind = _FIELD_KINDS.get(field)
        if field_kind is None:
            return 1.0
        candidate = Candidate(field_kind, selector, f"matches={count}", 1.0, 1.0, (), {})
        score_method = getattr(self.scorer, "score", None)
        if not callable(score_method):
            raise TypeError("scorer must provide score or score_selector")
        scored = score_method(candidate, ScoringContext(kind, snapshot))
        value = getattr(scored, "score", None)
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise TypeError("scorer returned an invalid score")
        return max(0.0, min(1.0, float(value)))

    def _fingerprint_matches(
        self, config: SiteConfig, index: PageSnapshot, chapter: PageSnapshot
    ) -> dict[str, bool]:
        expected: dict[str, set[str]] = {}
        for sample in config.validation_samples:
            fingerprint = sample.get("fingerprint")
            kind = sample.get("page_kind")
            if isinstance(kind, str) and isinstance(fingerprint, str):
                expected.setdefault(kind, set()).add(fingerprint)
        matches: dict[str, bool] = {}
        for kind, snapshot in (("book", index), ("chapter", chapter)):
            candidates = self._selector_group(config, kind)
            actual = fingerprint_html(snapshot.html, kind, candidates, config.fingerprint_salt).digest
            matches[kind] = actual in expected.get(kind, set())
        return matches

    def _fetch(self, url: str) -> AcquiredPage:
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
        return page

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

    def _terminal_status(self, snapshot: PageSnapshot) -> RevalidationStatus | None:
        classify = getattr(self.classifier, "classify", None)
        classification = classify(snapshot) if callable(classify) else PageClassifier().classify(snapshot)
        kind = getattr(classification, "kind", None)
        if kind is PageKind.AUTH_OR_CHALLENGE:
            return RevalidationStatus.STALE
        if kind is PageKind.ERROR or snapshot.status_code == 404:
            return RevalidationStatus.INVALID
        return None

    @staticmethod
    def _selector_group(config: SiteConfig, kind: str) -> dict[str, str]:
        raw = config.selectors[kind]
        if not isinstance(raw, Mapping) or not all(isinstance(key, str) and isinstance(value, str) for key, value in raw.items()):
            raise ValueError("selector group is invalid")
        return dict(raw)

    def _mark_valid(self, entry: RegistryEntry, checked_at: str) -> bool:
        try:
            self.registry.mark_validated(entry.config_id, checked_at, expected_version=entry.version)
            return True
        except ConfigConflictError:
            self.registry.load(entry.config_id)
            return False

    def _stale(
        self,
        entry: RegistryEntry,
        reasons: tuple[str, ...],
        scores: Mapping[str, float],
        matches: Mapping[str, bool],
        checked_at: str,
    ) -> RevalidationResult:
        try:
            self.registry.mark_stale(entry.config_id, expected_version=entry.version)
        except ConfigConflictError:
            self.registry.load(entry.config_id)
            reasons = (*reasons, "concurrent_revision")
        return RevalidationResult(RevalidationStatus.STALE, reasons, scores, matches, checked_at)

    def _invalid(self, entry: RegistryEntry, reasons: tuple[str, ...], checked_at: str) -> RevalidationResult:
        try:
            self.registry.mark_invalid(entry.config_id, reasons, expected_version=entry.version)
        except (ConfigConflictError, KeyError, RegistryError):
            try:
                self.registry.load(entry.config_id)
            except (KeyError, RegistryError, TypeError, ValueError):
                pass
            reasons = (*reasons, "concurrent_revision")
        return RevalidationResult(RevalidationStatus.INVALID, reasons, {}, {}, checked_at)

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
        return f"{scheme}://{authority}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")


__all__ = ["ConfigRevalidator", "RevalidationResult", "RevalidationStatus"]

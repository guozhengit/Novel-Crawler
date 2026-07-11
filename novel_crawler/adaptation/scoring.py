"""Trusted, deterministic heuristic scoring for extracted field candidates.

Scores are comparable only within a field.  ``confidence`` is retained as a
compatibility alias for the heuristic score; it is not a probability.
"""

from __future__ import annotations

import math
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeAlias, runtime_checkable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.acquisition.models import PageSnapshot

from .diagnostics import safe_origin
from .models import Candidate, FieldKind

_SAFE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_CHAPTER = re.compile(
    r"(?:第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节回卷]|chapter\s+(?:\d+|[a-z -]+)|part\s+(?:\d+|[ivxlcdm]+)|prologue|epilogue|foreword|afterword|interlude|序章|楔子|番外)",
    re.I,
)
_AUTHOR = re.compile(r"^(?:作者\s*[：:]?|by\s+|written\s+by\s+|author\s*[：:]?\s*)\S+", re.I)
_NOISE = re.compile(r"(?:comment|recommend|related|advert|\bad\b|banner|footer|share|评论|推荐|广告)", re.I)
_NAV_TEXT = {
    FieldKind.PREV_LINK: re.compile(r"^(?:上一[章节页]?|前一[章节页]?|previous(?:\s+chapter)?|prev)$", re.I),
    FieldKind.NEXT_LINK: re.compile(r"^(?:下一[章节页]?|后一[章节页]?|next(?:\s+chapter)?)$", re.I),
    FieldKind.INDEX_LINK: re.compile(r"^(?:目录|章节列表|返回书页|table\s+of\s+contents|contents|index)$", re.I),
}
_UNSAFE_SELECTOR = re.compile(r"(?:https?://|[?&]|token|secret|session|password)", re.I)


class ScoringContext:
    """Ephemeral scoring input whose representation never includes response data."""

    __slots__ = ("_locked", "_origin_key", "_page_kind", "_sample_id", "_snapshot")
    _locked: bool
    _origin_key: str
    _page_kind: PageKind
    _sample_id: str
    _snapshot: PageSnapshot

    def __init__(self, page_kind: PageKind, snapshot: PageSnapshot) -> None:
        """Create one scoring context per snapshot and reuse it for every candidate."""
        if not isinstance(page_kind, PageKind) or not isinstance(snapshot, PageSnapshot):
            raise TypeError("ScoringContext requires PageKind and PageSnapshot")
        sample_id = f"sample-{secrets.token_hex(16)}"
        object.__setattr__(self, "_page_kind", page_kind)
        object.__setattr__(self, "_snapshot", snapshot)
        object.__setattr__(self, "_sample_id", sample_id)
        object.__setattr__(self, "_origin_key", safe_origin(snapshot.final_url))
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("ScoringContext is immutable")

    @property
    def page_kind(self) -> PageKind:
        return self._page_kind

    @property
    def snapshot(self) -> PageSnapshot:
        return self._snapshot

    @property
    def sample_id(self) -> str:
        return self._sample_id

    @property
    def origin_key(self) -> str:
        return self._origin_key

    def __repr__(self) -> str:
        snapshot = self.snapshot
        origin = urlsplit(snapshot.final_url)
        safe_origin = f"{origin.scheme}://{origin.netloc}" if origin.scheme and origin.netloc else "redacted"
        return f"ScoringContext(page_kind={self.page_kind.value!r}, origin={safe_origin!r}, method={snapshot.method!r}, status={snapshot.status_code})"

    __str__ = __repr__


@dataclass(frozen=True)
class CandidateIdentity:
    """Only candidate information a pluggable scoring rule may observe."""

    field: FieldKind
    selector: str

    def __post_init__(self) -> None:
        if not isinstance(self.field, FieldKind):
            raise TypeError("field must be FieldKind")
        if not self.selector or len(self.selector) > 512 or _UNSAFE_SELECTOR.search(self.selector):
            raise ValueError("selector must be bounded and free of URLs or query-like secrets")


@dataclass(frozen=True)
class ScoreComponent:
    rule_id: str
    score: float
    weight: float

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.rule_id):
            raise ValueError("rule_id must be a safe stable identifier")
        if not math.isfinite(self.score) or not 0 <= self.score <= 1:
            raise ValueError("component score must be finite and between zero and one")
        if not math.isfinite(self.weight) or self.weight <= 0:
            raise ValueError("component weight must be finite and positive")

    @property
    def value(self) -> float:
        """Compatibility alias for older consumers."""
        return self.score


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    score: float
    components: tuple[ScoreComponent, ...]
    calibration_id: str
    version: str
    sample_id: str
    origin_key: str
    page_kind: PageKind

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Candidate):
            raise TypeError("candidate must be Candidate")
        if not math.isfinite(self.score) or not 0 <= self.score <= 1:
            raise ValueError("heuristic score must be finite and between zero and one")
        values = tuple(self.components)
        if not values or not all(isinstance(value, ScoreComponent) for value in values):
            raise ValueError("components must be a nonempty tuple of ScoreComponent")
        if not _SAFE_ID.fullmatch(self.calibration_id) or not _SAFE_ID.fullmatch(self.version):
            raise ValueError("calibration_id and version must be stable identifiers")
        if not _SAFE_ID.fullmatch(self.sample_id):
            raise ValueError("sample_id must be a safe structural identifier")
        if self.origin_key != "redacted" and safe_origin(self.origin_key) != self.origin_key:
            raise ValueError("origin_key must contain only a safe origin")
        if not isinstance(self.page_kind, PageKind):
            raise TypeError("page_kind must be PageKind")
        object.__setattr__(self, "components", values)

    @property
    def confidence(self) -> float:
        """Alias for the versioned heuristic score, not a probability."""
        return self.score


@dataclass(frozen=True)
class ScoringConfig:
    version: str = "score-v1"
    calibration_id: str = "heuristic-v1"
    target_chapter_links: int = 20
    target_content_chars: int = 1000
    target_paragraphs: int = 8

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.version) or not _SAFE_ID.fullmatch(self.calibration_id):
            raise ValueError("version identifiers must be safe and stable")
        if min(self.target_chapter_links, self.target_content_chars, self.target_paragraphs) <= 0:
            raise ValueError("thresholds must be positive")


ScorerConfig = ScoringConfig


@dataclass(frozen=True)
class HeadingFeatures:
    valid: bool
    tag: str = ""
    text_length: int = 0
    semantic: float = 0


@dataclass(frozen=True)
class ChapterListFeatures:
    valid: bool
    count: int = 0
    continuity: float = 0
    same_origin: float = 0
    precision: float = 0


@dataclass(frozen=True)
class ContentFeatures:
    valid: bool
    text_length: int = 0
    paragraphs: int = 0
    link_density: float = 1
    clean_ratio: float = 0


@dataclass(frozen=True)
class NavigationFeatures:
    valid: bool
    semantic: float = 0
    rel: float = 0
    order: float = 0


@dataclass(frozen=True)
class CleanFeatures:
    valid: bool
    semantic: float = 0


FieldFeatures: TypeAlias = HeadingFeatures | ChapterListFeatures | ContentFeatures | NavigationFeatures | CleanFeatures


@runtime_checkable
class ScoringRule(Protocol):
    @property
    def rule_id(self) -> str: ...

    @property
    def field(self) -> FieldKind: ...

    def components(self, identity: CandidateIdentity, features: FieldFeatures, config: ScoringConfig) -> tuple[ScoreComponent, ...]: ...


@dataclass(frozen=True)
class _BuiltinRule:
    field: FieldKind

    @property
    def rule_id(self) -> str:
        return f"{self.field.value}.builtin"

    def components(self, identity: CandidateIdentity, features: FieldFeatures, config: ScoringConfig) -> tuple[ScoreComponent, ...]:
        del identity
        prefix = self.field.value
        if self.field is FieldKind.TITLE:
            heading = features if isinstance(features, HeadingFeatures) else HeadingFeatures(False)
            return (_component(prefix, "semantic", heading.semantic, 3), _component(prefix, "dom", _tag_score(heading.tag, {"h1": 1, "title": 0.7, "h2": 0.5}), 2), _component(prefix, "length", _length_score(heading.text_length, 2, 80), 1))
        if self.field is FieldKind.AUTHOR:
            author = features if isinstance(features, HeadingFeatures) else HeadingFeatures(False)
            return (_component(prefix, "semantic", author.semantic, 4), _component(prefix, "length", _length_score(author.text_length, 2, 64), 1))
        if self.field is FieldKind.CHAPTER_TITLE:
            chapter_title = features if isinstance(features, HeadingFeatures) else HeadingFeatures(False)
            return (_component(prefix, "semantic", chapter_title.semantic, 4), _component(prefix, "dom", _tag_score(chapter_title.tag, {"h1": 1, "h2": 0.6, "title": 0.5}), 2), _component(prefix, "length", _length_score(chapter_title.text_length, 2, 100), 1))
        if self.field is FieldKind.CHAPTER_LIST:
            chapter_list = features if isinstance(features, ChapterListFeatures) else ChapterListFeatures(False)
            return (_component(prefix, "count", min(1, chapter_list.count / config.target_chapter_links), 2), _component(prefix, "continuity", chapter_list.continuity, 3), _component(prefix, "same_origin", chapter_list.same_origin, 1), _component(prefix, "selector_precision", chapter_list.precision, 4))
        if self.field is FieldKind.CONTENT:
            content = features if isinstance(features, ContentFeatures) else ContentFeatures(False)
            return (_component(prefix, "length", min(1, content.text_length / config.target_content_chars), 2), _component(prefix, "paragraphs", min(1, content.paragraphs / config.target_paragraphs), 2), _component(prefix, "link_density", max(0, 1 - content.link_density * 3) if content.valid else 0, 3), _component(prefix, "cleanliness", content.clean_ratio, 5))
        if self.field in _NAV_TEXT:
            navigation = features if isinstance(features, NavigationFeatures) else NavigationFeatures(False)
            return (_component(prefix, "rel", navigation.rel, 3), _component(prefix, "text", navigation.semantic, 2), _component(prefix, "order", navigation.order, 1))
        clean = features if isinstance(features, CleanFeatures) else CleanFeatures(False)
        return (_component(prefix, "noise_marker", clean.semantic, 1),)


def _component(prefix: str, name: str, score: float, weight: float) -> ScoreComponent:
    return ScoreComponent(f"{prefix}.{name}", max(0.0, min(1.0, score)) if math.isfinite(score) else 0.0, weight)


def _tag_score(tag: str, scores: dict[str, float]) -> float:
    return scores.get(tag, 0.0)


def _length_score(length: int, minimum: int, maximum: int) -> float:
    if length < minimum or length > maximum:
        return 0.0
    return min(1.0, length / 12)


class CandidateScorer:
    """Resolve selectors against a trusted snapshot and score field-local evidence."""

    def __init__(self, rules: Sequence[ScoringRule] | None = None, config: ScoringConfig | None = None) -> None:
        self.config = config or ScoringConfig()
        values: tuple[ScoringRule, ...] = tuple(rules) if rules is not None else tuple(_BuiltinRule(field) for field in FieldKind)
        if not values or not all(isinstance(rule, ScoringRule) for rule in values):
            raise TypeError("rules must be a nonempty sequence of ScoringRule")
        seen: set[FieldKind] = set()
        for rule in values:
            if not _SAFE_ID.fullmatch(rule.rule_id) or not isinstance(rule.field, FieldKind):
                raise TypeError("rules must implement ScoringRule with stable identifiers")
            if rule.field in seen:
                raise ValueError("duplicate scoring field")
            seen.add(rule.field)
        self.rules = values

    def score(self, candidate: Candidate, context: ScoringContext) -> ScoredCandidate:
        rule = next((value for value in self.rules if value.field is candidate.field), None)
        if rule is None:
            raise ValueError(f"no scoring rule for field {candidate.field.value}")
        features = self._derive(candidate, context)
        identity = CandidateIdentity(candidate.field, candidate.selector)
        raw = rule.components(identity, features, self.config)
        if not isinstance(raw, tuple) or not raw or not all(isinstance(value, ScoreComponent) for value in raw):
            raise ValueError("components must be a nonempty tuple of ScoreComponent")
        ids = [value.rule_id for value in raw]
        prefix = candidate.field.value + "."
        if len(ids) != len(set(ids)) or any(not value.startswith(prefix) for value in ids):
            raise ValueError("components must have unique field-prefixed rule IDs")
        total = sum(value.weight for value in raw)
        score = sum(value.score * value.weight for value in raw) / total
        return ScoredCandidate(candidate, score, raw, self.config.calibration_id, self.config.version, context.sample_id, context.origin_key, context.page_kind)

    def rank(self, candidates: Sequence[Candidate], context: ScoringContext) -> tuple[ScoredCandidate, ...]:
        if len({candidate.field for candidate in candidates}) > 1:
            raise ValueError("different fields are not comparable")
        return tuple(sorted((self.score(candidate, context) for candidate in candidates), key=lambda value: (-value.score, value.candidate.selector)))

    @staticmethod
    def _derive(candidate: Candidate, context: ScoringContext) -> FieldFeatures:
        soup = BeautifulSoup(context.snapshot.html, "lxml")
        try:
            nodes = soup.select(candidate.selector)
        except Exception:
            return _empty_features(candidate.field)
        tags = [node for node in nodes if isinstance(node, Tag)]
        if not tags:
            return _empty_features(candidate.field)
        return _field_features(candidate.field, tags, context, soup)


def _empty_features(field: FieldKind) -> FieldFeatures:
    if field in {FieldKind.TITLE, FieldKind.AUTHOR, FieldKind.CHAPTER_TITLE}:
        return HeadingFeatures(False)
    if field is FieldKind.CHAPTER_LIST:
        return ChapterListFeatures(False)
    if field is FieldKind.CONTENT:
        return ContentFeatures(False)
    if field in _NAV_TEXT:
        return NavigationFeatures(False)
    return CleanFeatures(False)


def _field_features(field: FieldKind, nodes: list[Tag], context: ScoringContext, soup: BeautifulSoup) -> FieldFeatures:
    text = " ".join(node.get_text(" ", strip=True) for node in nodes).strip()
    first = nodes[0]
    tag = first.name
    if field is FieldKind.TITLE:
        valid = len(nodes) == 1 and tag in {"h1", "h2", "title"}
        return HeadingFeatures(valid, tag, len(text), float(valid and context.page_kind is not PageKind.CHAPTER)) if valid else HeadingFeatures(False)
    if field is FieldKind.AUTHOR:
        semantic = float(len(nodes) == 1 and bool(_AUTHOR.search(text)))
        return HeadingFeatures(bool(semantic), tag, len(text), semantic) if semantic else HeadingFeatures(False)
    if field is FieldKind.CHAPTER_TITLE:
        semantic = float(len(nodes) == 1 and tag in {"h1", "h2", "title"} and bool(_CHAPTER.search(text)))
        return HeadingFeatures(bool(semantic), tag, len(text), semantic) if semantic else HeadingFeatures(False)
    if field is FieldKind.CHAPTER_LIST:
        if any(node.name != "a" for node in nodes):
            return ChapterListFeatures(False)
        chapter_flags = [bool(_CHAPTER.search(node.get_text(" ", strip=True))) for node in nodes]
        count = sum(chapter_flags)
        if count == 0:
            return ChapterListFeatures(False)
        precision = count / len(nodes)
        origins = [urlsplit(urljoin(context.snapshot.final_url, str(node.get("href", "")))).netloc for node in nodes]
        base = urlsplit(context.snapshot.final_url).netloc
        same = sum(origin == base for origin in origins) / len(origins)
        continuity = _continuity(nodes, chapter_flags)
        return ChapterListFeatures(True, count, continuity, same, precision)
    if field is FieldKind.CONTENT:
        if len(nodes) != 1 or tag not in {"article", "main", "section", "div"} or _is_noise(first):
            return ContentFeatures(False)
        total_text = first.get_text(" ", strip=True)
        noise_length = sum(len(node.get_text(" ", strip=True)) for node in first.find_all(_is_noise))
        clean_length = max(0, len(total_text) - noise_length)
        paragraphs = sum(1 for node in first.find_all("p") if not any(_is_noise(parent) for parent in node.parents if isinstance(parent, Tag)))
        if clean_length < 45 or paragraphs == 0:
            return ContentFeatures(False)
        link_length = sum(len(node.get_text(" ", strip=True)) for node in first.find_all("a"))
        density = link_length / max(1, len(total_text))
        clean_ratio = clean_length / max(1, len(total_text))
        return ContentFeatures(True, clean_length, paragraphs, density, clean_ratio)
    if field in _NAV_TEXT:
        if len(nodes) != 1 or tag != "a":
            return NavigationFeatures(False)
        rels = {str(value).casefold() for value in first.get("rel", [])}
        expected = field.value.removesuffix("_link")
        rel = float(expected in rels)
        semantic = float(bool(_NAV_TEXT[field].fullmatch(text)))
        if not rel and not semantic:
            return NavigationFeatures(False)
        anchors = soup.find_all("a")
        order = float(first in anchors and (field is FieldKind.INDEX_LINK or len(anchors) > 1))
        return NavigationFeatures(True, semantic, rel, order)
    semantic = float(any(_is_noise(node) for node in nodes))
    return CleanFeatures(bool(semantic), semantic)


def _is_noise(node: Tag) -> bool:
    marker = " ".join([node.name, str(node.get("id", "")), *[str(value) for value in node.get("class", [])]])
    return bool(_NOISE.search(marker))


def _continuity(nodes: list[Tag], flags: list[bool]) -> float:
    del flags
    if not nodes:
        return 0.0
    numbers = [_chapter_number(node) for node in nodes]
    if len(numbers) < 2:
        return 0.0
    # Decimal chapter labels and volume resets are intentionally neutral until
    # a future calibration can distinguish them without content leakage.
    adjacent = sum(left is not None and right is not None and right - left == 1 for left, right in zip(numbers, numbers[1:], strict=False))
    return adjacent / (len(numbers) - 1)


def _chapter_number(node: Tag) -> int | None:
    text = node.get_text(" ", strip=True)
    special = re.fullmatch(r"(?:prologue|epilogue|foreword|afterword|interlude|序章|楔子|番外(?:\s*\d+)?)", text, re.I)
    if special:
        return None
    marker = re.search(r"(?:chapter\s+|第\s*)(\d+)(?![\d.])", text, re.I)
    if marker:
        return int(marker.group(1))
    # Word-number and Chinese-number chapter labels may use a stable numeric
    # href only when there is no competing Book/Volume number in the label.
    if re.search(r"\b(?:book|volume)\s+\d+", text, re.I):
        return None
    href = re.search(r"\d+", str(node.get("href", "")))
    return int(href.group()) if href else None

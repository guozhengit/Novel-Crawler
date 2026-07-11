"""Trusted, deterministic heuristic scoring for extracted field candidates.

Scores are comparable only within a field.  ``confidence`` is retained as a
compatibility alias for the heuristic score; it is not a probability.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.acquisition.models import PageSnapshot

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


@dataclass(frozen=True)
class ScoringContext:
    page_kind: PageKind
    snapshot: PageSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.page_kind, PageKind) or not isinstance(self.snapshot, PageSnapshot):
            raise TypeError("ScoringContext requires PageKind and PageSnapshot")


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
class _Features:
    valid: bool
    tag: str = ""
    text_length: int = 0
    semantic: float = 0
    count: int = 0
    continuity: float = 0
    same_origin: float = 0
    precision: float = 0
    paragraphs: int = 0
    link_density: float = 1
    clean_ratio: float = 0
    rel: float = 0
    order: float = 0


@runtime_checkable
class ScoringRule(Protocol):
    @property
    def rule_id(self) -> str: ...

    @property
    def field(self) -> FieldKind: ...

    def components(self, candidate: Candidate, features: object, config: ScoringConfig) -> tuple[ScoreComponent, ...]: ...


@dataclass(frozen=True)
class _BuiltinRule:
    field: FieldKind

    @property
    def rule_id(self) -> str:
        return f"{self.field.value}.builtin"

    def components(self, candidate: Candidate, features: object, config: ScoringConfig) -> tuple[ScoreComponent, ...]:
        del candidate
        f = features if isinstance(features, _Features) else _Features(False)
        prefix = self.field.value
        if self.field is FieldKind.TITLE:
            return (_component(prefix, "semantic", f.semantic, 3), _component(prefix, "dom", _tag_score(f.tag, {"h1": 1, "title": 0.7, "h2": 0.5}), 2), _component(prefix, "length", _length_score(f.text_length, 2, 80), 1))
        if self.field is FieldKind.AUTHOR:
            return (_component(prefix, "semantic", f.semantic, 4), _component(prefix, "length", _length_score(f.text_length, 2, 64), 1))
        if self.field is FieldKind.CHAPTER_TITLE:
            return (_component(prefix, "semantic", f.semantic, 4), _component(prefix, "dom", _tag_score(f.tag, {"h1": 1, "h2": 0.6, "title": 0.5}), 2), _component(prefix, "length", _length_score(f.text_length, 2, 100), 1))
        if self.field is FieldKind.CHAPTER_LIST:
            return (_component(prefix, "count", min(1, f.count / config.target_chapter_links), 2), _component(prefix, "continuity", f.continuity, 3), _component(prefix, "same_origin", f.same_origin, 1), _component(prefix, "selector_precision", f.precision, 4))
        if self.field is FieldKind.CONTENT:
            return (_component(prefix, "length", min(1, f.text_length / config.target_content_chars), 2), _component(prefix, "paragraphs", min(1, f.paragraphs / config.target_paragraphs), 2), _component(prefix, "link_density", max(0, 1 - f.link_density * 3), 3), _component(prefix, "cleanliness", f.clean_ratio, 5))
        if self.field in _NAV_TEXT:
            return (_component(prefix, "rel", f.rel, 3), _component(prefix, "text", f.semantic, 2), _component(prefix, "order", f.order, 1))
        return (_component(prefix, "noise_marker", f.semantic, 1),)


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
        raw = rule.components(candidate, features, self.config)
        if not isinstance(raw, tuple) or not raw or not all(isinstance(value, ScoreComponent) for value in raw):
            raise ValueError("components must be a nonempty tuple of ScoreComponent")
        ids = [value.rule_id for value in raw]
        prefix = candidate.field.value + "."
        if len(ids) != len(set(ids)) or any(not value.startswith(prefix) for value in ids):
            raise ValueError("components must have unique field-prefixed rule IDs")
        total = sum(value.weight for value in raw)
        score = sum(value.score * value.weight for value in raw) / total
        return ScoredCandidate(candidate, score, raw, self.config.calibration_id, self.config.version)

    def rank(self, candidates: Sequence[Candidate], context: ScoringContext) -> tuple[ScoredCandidate, ...]:
        if len({candidate.field for candidate in candidates}) > 1:
            raise ValueError("different fields are not comparable")
        return tuple(sorted((self.score(candidate, context) for candidate in candidates), key=lambda value: (-value.score, value.candidate.selector)))

    @staticmethod
    def _derive(candidate: Candidate, context: ScoringContext) -> _Features:
        soup = BeautifulSoup(context.snapshot.html, "lxml")
        try:
            nodes = soup.select(candidate.selector)
        except Exception:
            return _Features(False)
        tags = [node for node in nodes if isinstance(node, Tag)]
        if not tags:
            return _Features(False)
        return _field_features(candidate.field, tags, context, soup)


def _field_features(field: FieldKind, nodes: list[Tag], context: ScoringContext, soup: BeautifulSoup) -> _Features:
    text = " ".join(node.get_text(" ", strip=True) for node in nodes).strip()
    first = nodes[0]
    tag = first.name
    if field is FieldKind.TITLE:
        valid = len(nodes) == 1 and tag in {"h1", "h2", "title"}
        return _Features(valid, tag, len(text), float(valid and context.page_kind is not PageKind.CHAPTER)) if valid else _Features(False)
    if field is FieldKind.AUTHOR:
        semantic = float(len(nodes) == 1 and bool(_AUTHOR.search(text)))
        return _Features(bool(semantic), tag, len(text), semantic) if semantic else _Features(False)
    if field is FieldKind.CHAPTER_TITLE:
        semantic = float(len(nodes) == 1 and tag in {"h1", "h2", "title"} and bool(_CHAPTER.search(text)))
        return _Features(bool(semantic), tag, len(text), semantic) if semantic else _Features(False)
    if field is FieldKind.CHAPTER_LIST:
        if any(node.name != "a" for node in nodes):
            return _Features(False)
        chapter_flags = [bool(_CHAPTER.search(node.get_text(" ", strip=True))) for node in nodes]
        count = sum(chapter_flags)
        if count == 0:
            return _Features(False)
        precision = count / len(nodes)
        origins = [urlsplit(urljoin(context.snapshot.final_url, str(node.get("href", "")))).netloc for node in nodes]
        base = urlsplit(context.snapshot.final_url).netloc
        same = sum(origin == base for origin in origins) / len(origins)
        continuity = _continuity(nodes, chapter_flags)
        return _Features(True, tag, len(text), precision, count, continuity, same, precision)
    if field is FieldKind.CONTENT:
        if len(nodes) != 1 or tag not in {"article", "main", "section", "div"} or _is_noise(first):
            return _Features(False)
        total_text = first.get_text(" ", strip=True)
        noise_length = sum(len(node.get_text(" ", strip=True)) for node in first.find_all(_is_noise))
        clean_length = max(0, len(total_text) - noise_length)
        paragraphs = sum(1 for node in first.find_all("p") if not any(_is_noise(parent) for parent in node.parents if isinstance(parent, Tag)))
        if clean_length < 45 or paragraphs == 0:
            return _Features(False)
        link_length = sum(len(node.get_text(" ", strip=True)) for node in first.find_all("a"))
        density = link_length / max(1, len(total_text))
        clean_ratio = clean_length / max(1, len(total_text))
        return _Features(True, tag, clean_length, 1, paragraphs=paragraphs, link_density=density, clean_ratio=clean_ratio)
    if field in _NAV_TEXT:
        if len(nodes) != 1 or tag != "a":
            return _Features(False)
        rels = {str(value).casefold() for value in first.get("rel", [])}
        expected = field.value.removesuffix("_link")
        rel = float(expected in rels)
        semantic = float(bool(_NAV_TEXT[field].fullmatch(text)))
        if not rel and not semantic:
            return _Features(False)
        anchors = soup.find_all("a")
        order = float(first in anchors and (field is FieldKind.INDEX_LINK or len(anchors) > 1))
        return _Features(True, tag, len(text), semantic, rel=rel, order=order)
    semantic = float(any(_is_noise(node) for node in nodes))
    return _Features(bool(semantic), tag, len(text), semantic)


def _is_noise(node: Tag) -> bool:
    marker = " ".join([node.name, str(node.get("id", "")), *[str(value) for value in node.get("class", [])]])
    return bool(_NOISE.search(marker))


def _continuity(nodes: list[Tag], flags: list[bool]) -> float:
    if not nodes:
        return 0.0
    if not all(flags):
        return sum(flags) / len(flags)
    numbers: list[int] = []
    for node in nodes:
        match = re.search(r"\d+", node.get_text(" ", strip=True)) or re.search(r"\d+", str(node.get("href", "")))
        if match:
            numbers.append(int(match.group()))
    if len(numbers) < 2:
        return 1.0
    adjacent = sum(right > left for left, right in zip(numbers, numbers[1:], strict=False))
    return adjacent / (len(numbers) - 1)

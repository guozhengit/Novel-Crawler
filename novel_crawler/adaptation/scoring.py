"""Deterministic field-local normalization of adaptation candidates."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, cast, runtime_checkable

from novel_crawler.acquisition.classifier import PageKind

from .models import Candidate, FieldKind, MetadataValue

_SAFE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_STRUCTURAL_KEYS = re.compile(r"(?:count|ratio|density|precision|position|depth|role|bucket|match|index|order|siblings?)(?:_|$)", re.I)
_SENSITIVE_KEY = re.compile(r"(?:html|body|text|title|author|content|url|href|hash|digest|selector|value|preview)", re.I)
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.+-]{0,80}")


@dataclass(frozen=True)
class ScoringContext:
    """Page classification and a small, content-free structural feature map."""

    page_kind: PageKind
    snapshot: Mapping[str, MetadataValue]

    def __post_init__(self) -> None:
        if not isinstance(self.page_kind, PageKind):
            raise TypeError("page_kind must be PageKind")
        clean: dict[str, MetadataValue] = {}
        for key, value in self.snapshot.items():
            if not _SAFE_ID.fullmatch(key) or _SENSITIVE_KEY.search(key) or not _STRUCTURAL_KEYS.search(key):
                raise ValueError("snapshot accepts structural feature keys only")
            if not isinstance(value, str | int | float | bool) or isinstance(value, float) and not math.isfinite(value):
                raise ValueError("snapshot values must be finite scalars")
            if isinstance(value, str) and not _SAFE_TOKEN.fullmatch(value):
                raise ValueError("snapshot strings must be stable tokens")
            clean[key] = value
        object.__setattr__(self, "snapshot", MappingProxyType(clean))


@dataclass(frozen=True)
class ScoreComponent:
    rule_id: str
    value: float
    weight: float

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.rule_id):
            raise ValueError("rule_id must be a stable identifier")
        if not math.isfinite(self.value) or not 0 <= self.value <= 1:
            raise ValueError("component value must be finite and between zero and one")
        if not math.isfinite(self.weight) or self.weight <= 0:
            raise ValueError("component weight must be finite and positive")


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    confidence: float
    components: tuple[ScoreComponent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Candidate):
            raise TypeError("candidate must be Candidate")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be finite and between zero and one")
        components = tuple(self.components)
        if not components or not all(isinstance(item, ScoreComponent) for item in components):
            raise ValueError("components must contain ScoreComponent values")
        object.__setattr__(self, "components", components)


@dataclass(frozen=True)
class ScorerConfig:
    min_chapter_links: int = 3
    target_chapter_links: int = 30
    target_paragraphs: int = 10
    max_link_density: float = 0.35
    version: str = "score-v1"

    def __post_init__(self) -> None:
        if self.min_chapter_links < 1 or self.target_chapter_links < self.min_chapter_links:
            raise ValueError("chapter link thresholds must be positive and ordered")
        if self.target_paragraphs < 1 or not math.isfinite(self.max_link_density) or not 0 < self.max_link_density <= 1:
            raise ValueError("content thresholds are invalid")
        if not _SAFE_ID.fullmatch(self.version):
            raise ValueError("version must be a stable identifier")


@runtime_checkable
class ScoringRule(Protocol):
    rule_id: str
    fields: frozenset[FieldKind]

    def components(self, item: Candidate, context: ScoringContext, config: ScorerConfig) -> tuple[ScoreComponent, ...]: ...


def _number(item: Candidate, key: str) -> float:
    value = item.metadata.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        return 0.0
    return float(value)


def _ratio(item: Candidate, key: str) -> float:
    return max(0.0, min(1.0, _number(item, key)))


def _token(item: Candidate, key: str) -> str:
    value = item.metadata.get(key, "")
    return value.casefold() if isinstance(value, str) else ""


def _flag(item: Candidate, key: str) -> float:
    return 1.0 if item.metadata.get(key) is True else 0.0


def _bucket(value: str, scores: Mapping[str, float]) -> float:
    return scores.get(value, 0.0)


@dataclass(frozen=True)
class _TitleRule:
    rule_id: str = "title"
    fields: frozenset[FieldKind] = frozenset({FieldKind.TITLE})

    def components(self, item: Candidate, context: ScoringContext, config: ScorerConfig) -> tuple[ScoreComponent, ...]:
        del context, config
        semantic = 1.0 if _token(item, "semantic_role") in {"book_title", "title", "book_title_zh"} else 0.0
        dom = {"h1": 1.0, "title": 0.75, "h2": 0.5}.get(_token(item, "dom_role"), 0.0)
        length = _bucket(_token(item, "length_bucket"), {"1-16": 0.75, "17-64": 1.0, "65+": 0.25})
        return (ScoreComponent("title.semantic", semantic, 3), ScoreComponent("title.dom", dom, 2), ScoreComponent("title.length", length, 1))


@dataclass(frozen=True)
class _AuthorRule:
    rule_id: str = "author"
    fields: frozenset[FieldKind] = frozenset({FieldKind.AUTHOR})

    def components(self, item: Candidate, context: ScoringContext, config: ScorerConfig) -> tuple[ScoreComponent, ...]:
        del context, config
        semantic = 1.0 if _token(item, "semantic_role") in {"author", "author_label", "author_label_zh"} else 0.0
        length = _bucket(_token(item, "length_bucket"), {"1-16": 1.0, "17-64": 0.65, "65+": 0.1})
        return (ScoreComponent("author.semantic", semantic, 4), ScoreComponent("author.length", length, 1))


@dataclass(frozen=True)
class _ChapterTitleRule:
    rule_id: str = "chapter_title"
    fields: frozenset[FieldKind] = frozenset({FieldKind.CHAPTER_TITLE})

    def components(self, item: Candidate, context: ScoringContext, config: ScorerConfig) -> tuple[ScoreComponent, ...]:
        del config
        semantic = 1.0 if _token(item, "semantic_role") in {"chapter_title", "chapter_title_zh"} else 0.0
        if context.page_kind is not PageKind.CHAPTER:
            semantic *= 0.5
        dom = {"h1": 1.0, "h2": 0.65, "title": 0.6}.get(_token(item, "dom_role"), 0.0)
        length = _bucket(_token(item, "length_bucket"), {"1-16": 1.0, "17-64": 0.9, "65+": 0.2})
        return (ScoreComponent("chapter_title.semantic", semantic, 4), ScoreComponent("chapter_title.dom", dom, 2), ScoreComponent("chapter_title.length", length, 1))


@dataclass(frozen=True)
class _ChapterListRule:
    rule_id: str = "chapter_list"
    fields: frozenset[FieldKind] = frozenset({FieldKind.CHAPTER_LIST})

    def components(self, item: Candidate, context: ScoringContext, config: ScorerConfig) -> tuple[ScoreComponent, ...]:
        del context
        count = max(0.0, _number(item, "link_count"))
        count_score = max(0.0, min(1.0, (count - config.min_chapter_links + 1) / (config.target_chapter_links - config.min_chapter_links + 1)))
        return (
            ScoreComponent("chapter_list.count", count_score, 2),
            ScoreComponent("chapter_list.continuity", _ratio(item, "continuity_ratio"), 3),
            ScoreComponent("chapter_list.same_origin", _ratio(item, "same_origin_ratio"), 1),
            ScoreComponent("chapter_list.selector_precision", _ratio(item, "selector_precision"), 4),
        )


@dataclass(frozen=True)
class _ContentRule:
    rule_id: str = "content"
    fields: frozenset[FieldKind] = frozenset({FieldKind.CONTENT})

    def components(self, item: Candidate, context: ScoringContext, config: ScorerConfig) -> tuple[ScoreComponent, ...]:
        del context
        length = _bucket(_token(item, "length_bucket"), {"1-16": 0.0, "17-64": 0.15, "65+": 0.45, "65-256": 0.45, "257-1000": 0.85, "1000+": 1.0})
        paragraphs = max(0.0, min(1.0, _number(item, "paragraph_count") / config.target_paragraphs))
        link_density = max(0.0, 1.0 - max(0.0, _number(item, "link_density")) / config.max_link_density)
        noise = 1.0 - _ratio(item, "noise_ratio")
        return (ScoreComponent("content.length", length, 2), ScoreComponent("content.paragraphs", paragraphs, 2), ScoreComponent("content.link_density", link_density, 3), ScoreComponent("content.noise", noise, 5))


@dataclass(frozen=True)
class _NavigationRule:
    rule_id: str = "navigation"
    fields: frozenset[FieldKind] = frozenset({FieldKind.PREV_LINK, FieldKind.NEXT_LINK, FieldKind.INDEX_LINK})

    def components(self, item: Candidate, context: ScoringContext, config: ScorerConfig) -> tuple[ScoreComponent, ...]:
        del context, config
        return (ScoreComponent("navigation.rel", _flag(item, "rel_match"), 3), ScoreComponent("navigation.text", _flag(item, "text_match"), 2), ScoreComponent("navigation.order", _flag(item, "order_match"), 1))


@dataclass(frozen=True)
class _CleanRule:
    rule_id: str = "clean_selector"
    fields: frozenset[FieldKind] = frozenset({FieldKind.CLEAN_SELECTOR})

    def components(self, item: Candidate, context: ScoringContext, config: ScorerConfig) -> tuple[ScoreComponent, ...]:
        del context, config
        return (ScoreComponent("clean_selector.noise_marker", _flag(item, "noise_marker"), 1),)


class CandidateScorer:
    """Score candidates within their field; it intentionally has no decision threshold."""

    def __init__(self, rules: Sequence[ScoringRule] | None = None, config: ScorerConfig | None = None) -> None:
        self.config = config or ScorerConfig()
        builtins = (_TitleRule(), _AuthorRule(), _ChapterTitleRule(), _ChapterListRule(), _ContentRule(), _NavigationRule(), _CleanRule())
        self.rules: tuple[ScoringRule, ...] = tuple(rules) if rules is not None else cast(tuple[ScoringRule, ...], builtins)
        if not self.rules or not all(isinstance(rule, ScoringRule) for rule in self.rules):
            raise TypeError("rules must implement ScoringRule")

    def score(self, item: Candidate, context: ScoringContext) -> ScoredCandidate:
        components = tuple(component for rule in self.rules if item.field in rule.fields for component in rule.components(item, context, self.config))
        if not components:
            raise ValueError(f"no scoring rule for field {item.field.value}")
        total_weight = sum(component.weight for component in components)
        confidence = sum(component.value * component.weight for component in components) / total_weight
        return ScoredCandidate(item, max(0.0, min(1.0, confidence)), components)

    def rank(self, items: Sequence[Candidate], context: ScoringContext) -> tuple[ScoredCandidate, ...]:
        fields = {item.field for item in items}
        if len(fields) > 1:
            raise ValueError("candidates must have the same field; different fields are not comparable")
        scored = (self.score(item, context) for item in items)
        return tuple(sorted(scored, key=lambda item: (-item.confidence, item.candidate.selector)))

"""Immutable, privacy-safe candidate evidence models."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

MetadataValue: TypeAlias = str | int | float | bool
_SAFE_DETAIL = re.compile(r"[a-z][a-z0-9_]*=[A-Za-z0-9_.-]+(?:;[a-z][a-z0-9_]*=[A-Za-z0-9_.-]+)*")


class FieldKind(Enum):
    TITLE = "title"
    AUTHOR = "author"
    CHAPTER_LIST = "chapter_list"
    CHAPTER_TITLE = "chapter_title"
    CONTENT = "content"
    PREV_LINK = "prev_link"
    NEXT_LINK = "next_link"
    INDEX_LINK = "index_link"
    CLEAN_SELECTOR = "clean_selector"


@dataclass(frozen=True)
class Evidence:
    rule_id: str
    weight: float
    detail: str

    def __post_init__(self) -> None:
        if len(self.detail) > 80:
            raise ValueError("evidence detail must be at most 80 characters")
        if not _SAFE_DETAIL.fullmatch(self.detail):
            raise ValueError("evidence detail must contain only structured, redacted features")


@dataclass(frozen=True)
class Candidate:
    field: FieldKind
    selector: str
    value_preview: str
    raw_score: float
    confidence: float
    evidence: tuple[Evidence, ...]
    metadata: Mapping[str, MetadataValue]

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class ExtractionResult:
    candidates: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))

    def for_field(self, field: FieldKind) -> tuple[Candidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.field is field)

    def __iter__(self) -> Iterator[Candidate]:
        return iter(self.candidates)

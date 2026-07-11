"""Immutable, privacy-safe candidate evidence models."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

MetadataValue: TypeAlias = str | int | float | bool
_SAFE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_SAFE_STRUCTURED = re.compile(r"[a-z][a-z0-9_]*=[A-Za-z0-9_.-]+(?:;[a-z][a-z0-9_]*=[A-Za-z0-9_.-]+)*")
_SELECTOR_SECRET = re.compile(r"(?:https?://|[?&]|token|secret|session|password|href\s*[*^$]?=)", re.I)


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
        if not _SAFE_ID.fullmatch(self.rule_id):
            raise ValueError("rule_id must be a safe stable identifier")
        if not math.isfinite(self.weight):
            raise ValueError("evidence weight must be finite")
        if len(self.detail) > 80 or not _SAFE_STRUCTURED.fullmatch(self.detail):
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
        if not isinstance(self.field, FieldKind):
            raise TypeError("field must be FieldKind")
        if not self.selector or len(self.selector) > 512 or _SELECTOR_SECRET.search(self.selector):
            raise ValueError("selector must be bounded and must not contain URLs or query-like secrets")
        if len(self.value_preview) > 80 or not _SAFE_STRUCTURED.fullmatch(self.value_preview):
            raise ValueError("value_preview must be a structured redacted summary")
        if not math.isfinite(self.raw_score):
            raise ValueError("raw_score must be finite")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be finite and between 0 and 1")
        frozen_evidence = tuple(self.evidence)
        if not all(isinstance(item, Evidence) for item in frozen_evidence):
            raise TypeError("evidence must contain Evidence values")
        clean_metadata: dict[str, MetadataValue] = {}
        for key, value in self.metadata.items():
            if not _SAFE_ID.fullmatch(key):
                raise ValueError("metadata keys must be safe identifiers")
            if not isinstance(value, str | int | float | bool) or isinstance(value, float) and not math.isfinite(value):
                raise ValueError("metadata values must be finite scalars")
            if isinstance(value, str) and not re.fullmatch(r"[A-Za-z0-9_.-]{0,80}", value):
                raise ValueError("metadata strings must be short redacted tokens")
            clean_metadata[key] = value
        object.__setattr__(self, "evidence", frozen_evidence)
        object.__setattr__(self, "metadata", MappingProxyType(clean_metadata))


@dataclass(frozen=True)
class ExtractionResult:
    candidates: tuple[Candidate, ...]
    provenance: str = "builtin"
    version: str = "v2"

    def __post_init__(self) -> None:
        values = tuple(self.candidates)
        if not all(isinstance(item, Candidate) for item in values):
            raise TypeError("candidates must contain Candidate values")
        if not _SAFE_ID.fullmatch(self.provenance) or not _SAFE_ID.fullmatch(self.version):
            raise ValueError("provenance and version must be safe identifiers")
        object.__setattr__(self, "candidates", values)

    def for_field(self, field: FieldKind) -> tuple[Candidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.field is field)

    def __iter__(self) -> Iterator[Candidate]:
        return iter(self.candidates)

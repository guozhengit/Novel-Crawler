"""Immutable, privacy-safe candidate evidence models."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError, dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

MetadataValue: TypeAlias = str | int | float | bool
_SAFE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_SAFE_STRUCTURED = re.compile(r"[a-z][a-z0-9_]*=[A-Za-z0-9_.+-]+(?:;[a-z][a-z0-9_]*=[A-Za-z0-9_.+-]+)*")
_SELECTOR_SECRET = re.compile(r"(?:https?://|[?&]|token|secret|session|password)", re.I)


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


class Candidate:
    __slots__ = ("_confidence", "_evidence", "_field", "_metadata", "_raw_score", "_selector", "_value_preview")

    def __init__(self, field: FieldKind, selector: str, value_preview: str, raw_score: float, confidence: float, evidence: tuple[Evidence, ...], metadata: Mapping[str, MetadataValue]) -> None:
        if not isinstance(field, FieldKind):
            raise TypeError("field must be FieldKind")
        if not selector or len(selector) > 512 or _SELECTOR_SECRET.search(selector):
            raise ValueError("selector must be bounded and must not contain URLs or query-like secrets")
        if len(value_preview) > 80 or not _SAFE_STRUCTURED.fullmatch(value_preview):
            raise ValueError("value_preview must be a structured redacted summary")
        if not math.isfinite(raw_score):
            raise ValueError("raw_score must be finite")
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be finite and between 0 and 1")
        frozen_evidence = tuple(evidence)
        if not all(isinstance(item, Evidence) for item in frozen_evidence):
            raise TypeError("evidence must contain Evidence values")
        clean_metadata: dict[str, MetadataValue] = {}
        for key, value in metadata.items():
            if not _SAFE_ID.fullmatch(key):
                raise ValueError("metadata keys must be safe identifiers")
            if not isinstance(value, str | int | float | bool) or isinstance(value, float) and not math.isfinite(value):
                raise ValueError("metadata values must be finite scalars")
            if isinstance(value, str):
                safe_string = len(value) <= 512 and not _SELECTOR_SECRET.search(value) if key.endswith("selector") else bool(re.fullmatch(r"[A-Za-z0-9_.+-]{0,80}", value))
                if not safe_string:
                    raise ValueError("metadata strings must be short redacted tokens or safe selectors")
            clean_metadata[key] = value
        object.__setattr__(self, "_field", field)
        object.__setattr__(self, "_selector", selector)
        object.__setattr__(self, "_value_preview", value_preview)
        object.__setattr__(self, "_raw_score", raw_score)
        object.__setattr__(self, "_confidence", confidence)
        object.__setattr__(self, "_evidence", frozen_evidence)
        object.__setattr__(self, "_metadata", MappingProxyType(clean_metadata))

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError(f"cannot assign to field '{name}'")

    field = property(lambda self: self._field)
    selector = property(lambda self: self._selector)
    value_preview = property(lambda self: self._value_preview)
    raw_score = property(lambda self: self._raw_score)
    confidence = property(lambda self: self._confidence)
    evidence = property(lambda self: self._evidence)
    metadata = property(lambda self: self._metadata)

    def __repr__(self) -> str:
        return f"Candidate(field={self.field.value!r}, selector_present=True, value_preview={self.value_preview!r}, raw_score={self.raw_score!r}, confidence={self.confidence!r}, evidence={self.evidence!r}, metadata={dict(self.metadata)!r})"


class ExtractionResult:
    __slots__ = ("_candidates", "_provenance", "_version")

    def __init__(self, candidates: tuple[Candidate, ...], provenance: str = "builtin", version: str = "v2") -> None:
        values = tuple(candidates)
        if not all(isinstance(item, Candidate) for item in values):
            raise TypeError("candidates must contain Candidate values")
        if not _SAFE_ID.fullmatch(provenance) or not _SAFE_ID.fullmatch(version):
            raise ValueError("provenance and version must be safe identifiers")
        object.__setattr__(self, "_candidates", values)
        object.__setattr__(self, "_provenance", provenance)
        object.__setattr__(self, "_version", version)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError(f"cannot assign to field '{name}'")

    candidates = property(lambda self: self._candidates)
    provenance = property(lambda self: self._provenance)
    version = property(lambda self: self._version)

    def __repr__(self) -> str:
        return f"ExtractionResult(candidates={self.candidates!r}, provenance={self.provenance!r}, version={self.version!r})"

    def for_field(self, field: FieldKind) -> tuple[Candidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.field is field)

    def __iter__(self) -> Iterator[Candidate]:
        return iter(self.candidates)

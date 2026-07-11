"""Deterministic single-page policy for field-local adaptation scores."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from novel_crawler.acquisition.classifier import Classification, PageKind

from .diagnostics import Diagnostic, DiagnosticCode, safe_origin
from .models import FieldKind
from .scoring import ScoredCandidate

_SAFE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_REQUIRED = {
    PageKind.BOOK_INDEX: (FieldKind.TITLE, FieldKind.CHAPTER_LIST),
    PageKind.CHAPTER: (FieldKind.CHAPTER_TITLE, FieldKind.CONTENT),
}


class DecisionKind(Enum):
    AUTO_ACCEPT = "auto_accept"
    REQUIRE_CONFIRMATION = "require_confirmation"
    REJECT = "reject"


@dataclass(frozen=True)
class DecisionConfig:
    high: float = 0.85
    medium: float = 0.60
    ambiguity_margin: float = 0.03
    version: str = "decision-v2"

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.medium, self.high, self.ambiguity_margin)):
            raise ValueError("thresholds must be finite")
        if not 0 <= self.medium < self.high <= 1 or not 0 <= self.ambiguity_margin <= 1:
            raise ValueError("invalid decision thresholds")
        if not _SAFE_ID.fullmatch(self.version):
            raise ValueError("version must be a stable identifier")


@dataclass(frozen=True, repr=False)
class ScoredPageBatch:
    sample_id: str
    safe_origin: str
    page_kind: PageKind
    candidates: tuple[ScoredCandidate, ...]

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.sample_id):
            raise ValueError("sample_id must be a safe structural identifier")
        if self.safe_origin != "redacted" and safe_origin(self.safe_origin) != self.safe_origin:
            raise ValueError("safe_origin must contain only an origin")
        if not isinstance(self.page_kind, PageKind):
            raise TypeError("page_kind must be PageKind")
        values = tuple(self.candidates)
        if not all(isinstance(item, ScoredCandidate) for item in values):
            raise TypeError("candidates must contain ScoredCandidate values")
        if any(item.sample_id != self.sample_id for item in values):
            raise ValueError("every candidate sample_id must match the batch sample_id")
        if any(item.origin_key != self.safe_origin for item in values):
            raise ValueError("every candidate origin must match the batch safe_origin")
        if any(item.page_kind is not self.page_kind for item in values):
            raise ValueError("every candidate page_kind must match the batch page_kind")
        object.__setattr__(self, "candidates", values)

    def __repr__(self) -> str:
        return f"ScoredPageBatch(sample_id={self.sample_id!r}, safe_origin={self.safe_origin!r}, page_kind={self.page_kind.value!r}, candidate_count={len(self.candidates)})"


class FieldDecision:
    __slots__ = ("_best_selector", "_field", "_reason_ids", "_score", "_status", "_threshold")

    def __init__(self, field: FieldKind, best_selector: str | None, score: float, threshold: float, status: DecisionKind, reason_ids: tuple[str, ...] = ()) -> None:
        if not isinstance(field, FieldKind) or best_selector is not None and not isinstance(best_selector, str):
            raise TypeError("invalid field decision")
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (score, threshold)):
            raise ValueError("scores and thresholds must be finite and bounded")
        reasons = tuple(reason_ids)
        if not isinstance(status, DecisionKind) or not all(_SAFE_ID.fullmatch(item) for item in reasons):
            raise ValueError("invalid status or reason_ids")
        object.__setattr__(self, "_field", field)
        object.__setattr__(self, "_best_selector", best_selector)
        object.__setattr__(self, "_score", score)
        object.__setattr__(self, "_threshold", threshold)
        object.__setattr__(self, "_status", status)
        object.__setattr__(self, "_reason_ids", reasons)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("FieldDecision is immutable")

    field = property(lambda self: self._field)
    best_selector = property(lambda self: self._best_selector)
    score = property(lambda self: self._score)
    threshold = property(lambda self: self._threshold)
    status = property(lambda self: self._status)
    reason_ids = property(lambda self: self._reason_ids)

    def __repr__(self) -> str:
        return f"FieldDecision(field={self.field.value!r}, score={self.score!r}, threshold={self.threshold!r}, status={self.status.value!r}, selector_present={self.best_selector is not None!r}, reason_ids={self.reason_ids!r})"

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field.value, "score": self.score, "threshold": self.threshold, "status": self.status.value, "selector_present": self.best_selector is not None, "reason_ids": list(self.reason_ids)}


class AdaptationDecision:
    __slots__ = ("_config_version", "_diagnostic", "_fields", "_kind", "_overall_score")

    def __init__(self, kind: DecisionKind, overall_score: float, fields: Sequence[FieldDecision], config_version: str, diagnostic: Diagnostic) -> None:
        if not isinstance(kind, DecisionKind):
            raise TypeError("kind must be DecisionKind")
        if not math.isfinite(overall_score) or not 0 <= overall_score <= 1:
            raise ValueError("overall_score must be finite and bounded")
        if not isinstance(diagnostic, Diagnostic):
            raise TypeError("diagnostic must be Diagnostic")
        values = tuple(fields)
        terminal_codes = {DiagnosticCode.AUTH_REQUIRED, DiagnosticCode.ERROR_PAGE, DiagnosticCode.UNSUPPORTED_PAGE}
        if not values:
            if kind is not DecisionKind.REJECT or not diagnostic.codes or not set(diagnostic.codes) <= terminal_codes:
                raise ValueError("empty fields are allowed only for terminal reject diagnostics")
        elif not all(isinstance(item, FieldDecision) for item in values):
            raise TypeError("fields must contain FieldDecision values")
        if len({item.field for item in values}) != len(values):
            raise ValueError("fields must have unique field kinds")
        expected_kind = DecisionKind.REJECT if any(item.status is DecisionKind.REJECT for item in values) else DecisionKind.REQUIRE_CONFIRMATION if any(item.status is DecisionKind.REQUIRE_CONFIRMATION for item in values) else DecisionKind.AUTO_ACCEPT
        if values and kind is not expected_kind:
            raise ValueError("kind must agree with field statuses")
        if not _SAFE_ID.fullmatch(config_version):
            raise ValueError("config_version must be a stable identifier")
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_overall_score", overall_score)
        object.__setattr__(self, "_fields", values)
        object.__setattr__(self, "_config_version", config_version)
        object.__setattr__(self, "_diagnostic", diagnostic)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("AdaptationDecision is immutable")

    kind = property(lambda self: self._kind)
    overall_score = property(lambda self: self._overall_score)
    fields = property(lambda self: self._fields)
    config_version = property(lambda self: self._config_version)
    diagnostic = property(lambda self: self._diagnostic)

    def __repr__(self) -> str:
        return f"AdaptationDecision(kind={self.kind.value!r}, overall_score={self.overall_score!r}, fields={self.fields!r}, config_version={self.config_version!r}, diagnostic={self.diagnostic!r})"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "overall_score": self.overall_score, "fields": [item.to_dict() for item in self.fields], "config_version": self.config_version, "diagnostic": self.diagnostic.to_dict()}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


class DecisionPolicy:
    def __init__(self, config: DecisionConfig | None = None) -> None:
        if config is not None and not isinstance(config, DecisionConfig):
            raise TypeError("config must be DecisionConfig")
        self.config = config or DecisionConfig()

    def decide(self, classification: Classification, batch: ScoredPageBatch) -> AdaptationDecision:
        if not isinstance(classification, Classification):
            raise TypeError("classification must be Classification")
        if not isinstance(batch, ScoredPageBatch):
            raise TypeError("batch must be exactly one ScoredPageBatch")
        if classification.kind is not batch.page_kind:
            raise ValueError("classification page_kind must match batch page_kind")
        values = batch.candidates
        terminal = {PageKind.AUTH_OR_CHALLENGE: DiagnosticCode.AUTH_REQUIRED, PageKind.ERROR: DiagnosticCode.ERROR_PAGE, PageKind.UNKNOWN: DiagnosticCode.UNSUPPORTED_PAGE, PageKind.SEARCH_OR_LIST: DiagnosticCode.UNSUPPORTED_PAGE}
        if classification.kind in terminal:
            diagnostic = Diagnostic((terminal[classification.kind],), (), batch.safe_origin, {"candidate_count": len(values)})
            return AdaptationDecision(DecisionKind.REJECT, 0.0, (), self.config.version, diagnostic)
        required = _REQUIRED.get(classification.kind)
        if required is None:
            diagnostic = Diagnostic((DiagnosticCode.UNSUPPORTED_PAGE,), (), batch.safe_origin, {"candidate_count": len(values)})
            return AdaptationDecision(DecisionKind.REJECT, 0.0, (), self.config.version, diagnostic)
        fields: list[FieldDecision] = []
        codes: list[DiagnosticCode] = []
        for field in required:
            ranked = sorted((item for item in values if item.candidate.field is field), key=lambda item: (-item.score, item.candidate.selector))
            reasons: tuple[str, ...]
            if not ranked:
                fields.append(FieldDecision(field, None, 0.0, self.config.medium, DecisionKind.REJECT, ("missing_field",)))
                codes.append(DiagnosticCode.MISSING_FIELD)
                continue
            best = ranked[0]
            ambiguous = len(ranked) > 1 and best.score - ranked[1].score < self.config.ambiguity_margin
            if best.score < self.config.medium:
                status, threshold, reasons = DecisionKind.REJECT, self.config.medium, ("low_confidence",)
                codes.append(DiagnosticCode.LOW_CONFIDENCE)
            elif ambiguous:
                status, threshold, reasons = DecisionKind.REQUIRE_CONFIRMATION, self.config.high, ("ambiguous_candidates",)
                codes.append(DiagnosticCode.AMBIGUOUS_CANDIDATES)
            elif best.score < self.config.high:
                status, threshold, reasons = DecisionKind.REQUIRE_CONFIRMATION, self.config.high, ("low_confidence",)
                codes.append(DiagnosticCode.LOW_CONFIDENCE)
            else:
                status, threshold, reasons = DecisionKind.AUTO_ACCEPT, self.config.high, ()
            fields.append(FieldDecision(field, best.candidate.selector, best.score, threshold, status, reasons))
        kind = DecisionKind.REJECT if any(item.status is DecisionKind.REJECT for item in fields) else DecisionKind.REQUIRE_CONFIRMATION if any(item.status is DecisionKind.REQUIRE_CONFIRMATION for item in fields) else DecisionKind.AUTO_ACCEPT
        overall = sum(item.score for item in fields) / len(fields)
        unique_codes = tuple(dict.fromkeys(codes))
        diagnostic = Diagnostic(unique_codes, tuple(code.value for code in unique_codes), batch.safe_origin, {"candidate_count": len(values), "required_field_count": len(required)})
        return AdaptationDecision(kind, overall, tuple(fields), self.config.version, diagnostic)

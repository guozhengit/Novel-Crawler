"""Deterministic policy for turning field-local scores into a decision."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

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
class FieldDecision:
    field: FieldKind
    best_selector: str | None
    score: float
    threshold: float
    status: DecisionKind
    reason_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.field, FieldKind) or self.best_selector is not None and not isinstance(self.best_selector, str):
            raise TypeError("invalid field decision")
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (self.score, self.threshold)):
            raise ValueError("scores and thresholds must be finite and bounded")
        if not isinstance(self.status, DecisionKind) or not all(_SAFE_ID.fullmatch(item) for item in self.reason_ids):
            raise ValueError("invalid status or reason_ids")
        object.__setattr__(self, "reason_ids", tuple(self.reason_ids))


@dataclass(frozen=True)
class AdaptationDecision:
    kind: DecisionKind
    overall_score: float
    fields: tuple[FieldDecision, ...]
    config_version: str
    diagnostic: Diagnostic


@dataclass(frozen=True)
class DecisionPolicy:
    high: float = 0.85
    medium: float = 0.60
    config_version: str = "decision-v1"

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.medium, self.high)) or not 0 <= self.medium < self.high <= 1:
            raise ValueError("thresholds must satisfy 0 <= medium < high <= 1")
        if not _SAFE_ID.fullmatch(self.config_version):
            raise ValueError("config_version must be a stable identifier")

    def decide(
        self,
        classification: Classification,
        candidates: Sequence[ScoredCandidate],
        source_url: str | None = None,
    ) -> AdaptationDecision:
        if not isinstance(classification, Classification):
            raise TypeError("classification must be Classification")
        values = tuple(candidates)
        if not all(isinstance(item, ScoredCandidate) for item in values):
            raise TypeError("candidates must contain ScoredCandidate values")

        terminal = {
            PageKind.AUTH_OR_CHALLENGE: DiagnosticCode.AUTH_REQUIRED,
            PageKind.ERROR: DiagnosticCode.ERROR_PAGE,
            PageKind.UNKNOWN: DiagnosticCode.UNSUPPORTED_PAGE,
            PageKind.SEARCH_OR_LIST: DiagnosticCode.UNSUPPORTED_PAGE,
        }
        if classification.kind in terminal:
            diagnostic = Diagnostic((terminal[classification.kind],), (), safe_origin(source_url), {"candidate_count": len(values)})
            return AdaptationDecision(DecisionKind.REJECT, 0.0, (), self.config_version, diagnostic)

        required = _REQUIRED.get(classification.kind)
        if required is None:
            diagnostic = Diagnostic((DiagnosticCode.UNSUPPORTED_PAGE,), (), safe_origin(source_url), {"candidate_count": len(values)})
            return AdaptationDecision(DecisionKind.REJECT, 0.0, (), self.config_version, diagnostic)

        fields: list[FieldDecision] = []
        codes: list[DiagnosticCode] = []
        for field in required:
            ranked = sorted((item for item in values if item.candidate.field is field), key=lambda item: (-item.score, item.candidate.selector))
            reasons: tuple[str, ...]
            if not ranked:
                fields.append(FieldDecision(field, None, 0.0, self.medium, DecisionKind.REJECT, ("missing_field",)))
                codes.append(DiagnosticCode.MISSING_FIELD)
                continue
            best = ranked[0]
            tied = len(ranked) > 1 and ranked[1].score == best.score
            if best.score < self.medium:
                status, threshold, reasons = DecisionKind.REJECT, self.medium, ("low_confidence",)
                codes.append(DiagnosticCode.LOW_CONFIDENCE)
            elif tied:
                status, threshold, reasons = DecisionKind.REQUIRE_CONFIRMATION, self.high, ("ambiguous_candidates",)
                codes.append(DiagnosticCode.AMBIGUOUS_CANDIDATES)
            elif best.score < self.high:
                status, threshold, reasons = DecisionKind.REQUIRE_CONFIRMATION, self.high, ("low_confidence",)
                codes.append(DiagnosticCode.LOW_CONFIDENCE)
            else:
                status, threshold, reasons = DecisionKind.AUTO_ACCEPT, self.high, ()
            fields.append(FieldDecision(field, best.candidate.selector, best.score, threshold, status, reasons))

        kind = DecisionKind.AUTO_ACCEPT
        if any(item.status is DecisionKind.REJECT for item in fields):
            kind = DecisionKind.REJECT
        elif any(item.status is DecisionKind.REQUIRE_CONFIRMATION for item in fields):
            kind = DecisionKind.REQUIRE_CONFIRMATION
        overall = sum(item.score for item in fields) / len(fields)
        unique_codes = tuple(dict.fromkeys(codes))
        diagnostic = Diagnostic(unique_codes, tuple(code.value for code in unique_codes), safe_origin(source_url), {"candidate_count": len(values), "required_field_count": len(required)})
        return AdaptationDecision(kind, overall, tuple(fields), self.config_version, diagnostic)

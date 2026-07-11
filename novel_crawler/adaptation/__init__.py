"""Candidate extraction primitives for scored site adaptation."""

from .decision import AdaptationDecision, DecisionKind, DecisionPolicy, FieldDecision
from .diagnostics import Diagnostic, DiagnosticCode
from .extractor import CandidateExtractor, ExtractionRule, ExtractorConfig
from .models import Candidate, Evidence, ExtractionResult, FieldKind
from .scoring import (
    CandidateIdentity,
    CandidateScorer,
    FieldFeatures,
    ScoreComponent,
    ScoredCandidate,
    ScorerConfig,
    ScoringConfig,
    ScoringContext,
    ScoringRule,
)

__all__ = [
    "AdaptationDecision",
    "Candidate",
    "DecisionKind",
    "DecisionPolicy",
    "Diagnostic",
    "DiagnosticCode",
    "CandidateExtractor",
    "CandidateIdentity",
    "CandidateScorer",
    "Evidence",
    "ExtractionResult",
    "ExtractionRule",
    "ExtractorConfig",
    "FieldDecision",
    "FieldKind",
    "FieldFeatures",
    "ScoreComponent",
    "ScoredCandidate",
    "ScorerConfig",
    "ScoringConfig",
    "ScoringContext",
    "ScoringRule",
]

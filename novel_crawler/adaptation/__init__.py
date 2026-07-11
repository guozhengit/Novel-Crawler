"""Candidate extraction primitives for scored site adaptation."""

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
    "Candidate",
    "CandidateExtractor",
    "CandidateIdentity",
    "CandidateScorer",
    "Evidence",
    "ExtractionResult",
    "ExtractionRule",
    "ExtractorConfig",
    "FieldKind",
    "FieldFeatures",
    "ScoreComponent",
    "ScoredCandidate",
    "ScorerConfig",
    "ScoringConfig",
    "ScoringContext",
    "ScoringRule",
]

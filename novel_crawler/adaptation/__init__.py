"""Candidate extraction primitives for scored site adaptation."""

from .extractor import CandidateExtractor, ExtractionRule, ExtractorConfig
from .models import Candidate, Evidence, ExtractionResult, FieldKind
from .scoring import CandidateScorer, ScoreComponent, ScoredCandidate, ScorerConfig, ScoringContext, ScoringRule

__all__ = [
    "Candidate",
    "CandidateExtractor",
    "CandidateScorer",
    "Evidence",
    "ExtractionResult",
    "ExtractionRule",
    "ExtractorConfig",
    "FieldKind",
    "ScoreComponent",
    "ScoredCandidate",
    "ScorerConfig",
    "ScoringContext",
    "ScoringRule",
]

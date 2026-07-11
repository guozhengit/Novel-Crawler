"""Candidate extraction primitives for scored site adaptation."""

from .decision import AdaptationDecision, DecisionConfig, DecisionKind, DecisionPolicy, FieldDecision, ScoredPageBatch
from .diagnostics import Diagnostic, DiagnosticCode
from .extractor import CandidateExtractor, ExtractionRule, ExtractorConfig
from .models import Candidate, Evidence, ExtractionResult, FieldKind
from .registry import (
    ConfigConflictError,
    ConfigRegistry,
    ConfigStatus,
    RegistryEntry,
    RegistryError,
    RegistryLimitError,
    RegistryLockTimeout,
)
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
from .service import ProbeService
from .validation import ConfigDraft, MultiPageValidator, PageValidation, ValidationResult

__all__ = [
    "AdaptationDecision",
    "Candidate",
    "DecisionKind",
    "DecisionConfig",
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
    "ScoredPageBatch",
    "ProbeService",
    "ConfigDraft",
    "ConfigConflictError",
    "ConfigRegistry",
    "ConfigStatus",
    "MultiPageValidator",
    "PageValidation",
    "RegistryEntry",
    "RegistryError",
    "RegistryLimitError",
    "RegistryLockTimeout",
    "ValidationResult",
]

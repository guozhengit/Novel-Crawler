"""Candidate extraction primitives for scored site adaptation."""

from .config_manager import ConfigManager, ConfigResolution, ResolutionKind
from .config_schema import DEFAULT_SCHEMA_REGISTRY, SchemaVersionRegistry, SiteConfig, parse_config
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
from .revalidation import ConfigRevalidator, RevalidationResult, RevalidationStatus
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
from .static_service import StaticAdaptiveService
from .validation import ConfigDraft, MultiPageValidator, PageValidation, ValidationResult

__all__ = [
    "AdaptationDecision",
    "ConfigManager",
    "ConfigResolution",
    "DEFAULT_SCHEMA_REGISTRY",
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
    "StaticAdaptiveService",
    "ConfigDraft",
    "ConfigConflictError",
    "ConfigRegistry",
    "ConfigRevalidator",
    "ConfigStatus",
    "MultiPageValidator",
    "PageValidation",
    "RegistryEntry",
    "RegistryError",
    "RegistryLimitError",
    "RegistryLockTimeout",
    "RevalidationResult",
    "RevalidationStatus",
    "ResolutionKind",
    "SchemaVersionRegistry",
    "SiteConfig",
    "ValidationResult",
    "parse_config",
]

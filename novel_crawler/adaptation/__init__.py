"""Candidate extraction primitives for scored site adaptation."""

from .extractor import CandidateExtractor
from .models import Candidate, Evidence, ExtractionResult, FieldKind

__all__ = ["Candidate", "CandidateExtractor", "Evidence", "ExtractionResult", "FieldKind"]

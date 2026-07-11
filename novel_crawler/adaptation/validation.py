"""Privacy-safe validation of an adaptation across adjacent pages."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from novel_crawler.acquisition.classifier import PageKind

from .decision import DecisionKind
from .fingerprint import StructureFingerprint
from .url_paths import canonical_path

_SAFE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}")


class ConfigDraft:
    """Draft configuration whose selectors remain process-private."""

    __slots__ = ("_domain", "_fingerprint_salt", "_fingerprints", "_navigation_paths", "_scores", "_selectors", "_version")
    _selectors: Mapping[str, str]
    _fingerprints: Mapping[str, StructureFingerprint]
    _fingerprint_salt: bytes | None
    _navigation_paths: tuple[str, ...]

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("ConfigDraft is immutable")

    def __init__(
        self,
        version: str,
        domain: str,
        scores: Mapping[str, float],
        selectors: Mapping[str, str],
        *,
        fingerprints: Mapping[str, StructureFingerprint] | None = None,
        fingerprint_salt: bytes | None = None,
        navigation_paths: tuple[str, ...] = (),
    ) -> None:
        if not _SAFE_ID.fullmatch(version) or not domain or "/" in domain:
            raise ValueError("invalid draft identity")
        if not all(_SAFE_ID.fullmatch(key) and math.isfinite(value) and 0 <= value <= 1 for key, value in scores.items()):
            raise ValueError("invalid field scores")
        object.__setattr__(self, "_version", version)
        object.__setattr__(self, "_domain", domain)
        object.__setattr__(self, "_scores", MappingProxyType(dict(scores)))
        object.__setattr__(self, "_selectors", MappingProxyType(dict(selectors)))
        values = dict(fingerprints or {})
        if values and (
            set(values) != {"book", "chapter_first", "chapter_second"}
            or values["book"].page_kind != "book"
            or values["chapter_first"].page_kind != "chapter"
            or values["chapter_second"].page_kind != "chapter"
        ):
            raise ValueError("fingerprints must contain the complete three-page baseline")
        if fingerprint_salt is not None and (not isinstance(fingerprint_salt, bytes) or len(fingerprint_salt) != 32):
            raise ValueError("fingerprint_salt must be exactly 32 bytes")
        if bool(values) != (fingerprint_salt is not None):
            raise ValueError("fingerprints and fingerprint_salt must be supplied together")
        object.__setattr__(self, "_fingerprints", MappingProxyType(values))
        object.__setattr__(self, "_fingerprint_salt", fingerprint_salt)
        paths = tuple(dict.fromkeys(canonical_path(path) for path in navigation_paths))
        if paths and len(paths) != 3:
            raise ValueError("navigation_paths must contain three unique canonical paths")
        object.__setattr__(self, "_navigation_paths", paths)

    version = property(lambda self: self._version)
    domain = property(lambda self: self._domain)
    scores = property(lambda self: self._scores)

    def selector(self, field: str) -> str | None:
        return self._selectors.get(field)

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "domain_present": True, "field_scores": dict(self.scores)}

    def to_config(self) -> dict[str, Any]:
        """Explicit sensitive export for a caller that intends to persist selectors."""
        return {
            **self.to_dict(),
            "domain": self.domain,
            "selectors": dict(self._selectors),
            "fingerprints": dict(self._fingerprints),
            "fingerprint_salt": self._fingerprint_salt,
            "navigation_paths": self._navigation_paths,
        }


@dataclass(frozen=True, repr=False)
class PageValidation:
    page_id: str
    kind: PageKind
    decision: DecisionKind
    book_identity_matches: bool
    content_selector: str
    content_length: int
    paragraph_count: int
    next_matches_adjacent: bool
    auth_or_error: bool = False
    content_fingerprint: str = ""

    def __repr__(self) -> str:
        return f"PageValidation(kind={self.kind.value!r}, decision={self.decision.value!r})"


class ValidationResult:
    __slots__ = ("confidence", "config_draft", "diagnostic", "ok", "outcome", "page_decisions", "reason_ids")

    def __init__(self, ok: bool, confidence: float, reason_ids: tuple[str, ...], page_decisions: tuple[DecisionKind, ...], diagnostic: Mapping[str, int], config_draft: ConfigDraft | None) -> None:
        self.ok = ok
        self.confidence = confidence
        self.reason_ids = reason_ids
        self.page_decisions = page_decisions
        self.diagnostic = MappingProxyType(dict(diagnostic))
        self.config_draft = config_draft
        self.outcome = DecisionKind.REJECT if not ok else DecisionKind.AUTO_ACCEPT if confidence >= 0.85 else DecisionKind.REQUIRE_CONFIRMATION

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "confidence": self.confidence, "reason_ids": list(self.reason_ids), "page_decisions": [item.value for item in self.page_decisions], "outcome": self.outcome.value, "diagnostic": dict(self.diagnostic), "config_draft": self.config_draft.to_dict() if self.config_draft else None}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


class MultiPageValidator:
    def validate(self, first: PageValidation, second: PageValidation, draft: ConfigDraft, *, catalog_order_ok: bool = True, index_decision: DecisionKind = DecisionKind.AUTO_ACCEPT) -> ValidationResult:
        reasons: list[str] = []
        if not catalog_order_ok:
            reasons.append("catalog_order_invalid")
        if not first.next_matches_adjacent:
            reasons.append("next_link_mismatch")
        if first.page_id == second.page_id:
            reasons.append("url_duplicate")
        if first.auth_or_error or second.auth_or_error:
            reasons.append("auth_or_error")
        if not first.content_fingerprint or first.content_fingerprint != second.content_fingerprint:
            reasons.append("content_structure_mismatch")
        if not first.book_identity_matches or not second.book_identity_matches:
            reasons.append("book_title_mismatch")
        if not self._reasonable(first, second):
            reasons.append("content_shape_invalid")
        if DecisionKind.REJECT in (index_decision, first.decision, second.decision):
            reasons.append("page_rejected")
        confidence = min((*draft.scores.values(), 1.0))
        if DecisionKind.REQUIRE_CONFIRMATION in (index_decision, first.decision, second.decision):
            confidence = min(confidence, 0.84)
        unique = tuple(dict.fromkeys(reasons))
        return ValidationResult(not unique, confidence if not unique else 0.0, unique, (index_decision, first.decision, second.decision), {"pages": 3, "failures": len(unique)}, draft if not unique else None)

    @staticmethod
    def _reasonable(first: PageValidation, second: PageValidation) -> bool:
        if min(first.content_length, second.content_length) < 45 or min(first.paragraph_count, second.paragraph_count) < 1:
            return False
        ratio = max(first.content_length, second.content_length) / max(1, min(first.content_length, second.content_length))
        return ratio <= 8

"""Stable, serializable and privacy-safe adaptation diagnostics."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

_SAFE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_SAFE_COUNT = re.compile(r"[a-z][a-z0-9_]{0,39}")


class DiagnosticCode(Enum):
    LOW_CONFIDENCE = "low_confidence"
    MISSING_FIELD = "missing_field"
    AMBIGUOUS_CANDIDATES = "ambiguous_candidates"
    UNSUPPORTED_PAGE = "unsupported_page"
    AUTH_REQUIRED = "auth_required"
    ERROR_PAGE = "error_page"


def safe_origin(url: str | None) -> str:
    """Return only scheme, hostname and port; malformed values are redacted."""
    if not url:
        return "redacted"
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "redacted"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    except ValueError:
        return "redacted"


@dataclass(frozen=True)
class Diagnostic:
    codes: tuple[DiagnosticCode, ...]
    evidence_ids: tuple[str, ...] = ()
    origin: str = "redacted"
    counts: dict[str, int] | None = None

    def __post_init__(self) -> None:
        codes = tuple(self.codes)
        evidence = tuple(self.evidence_ids)
        if not all(isinstance(code, DiagnosticCode) for code in codes):
            raise TypeError("codes must contain DiagnosticCode values")
        if not all(_SAFE_ID.fullmatch(item) for item in evidence):
            raise ValueError("evidence_ids must be stable identifiers")
        if self.origin != "redacted" and safe_origin(self.origin) != self.origin:
            raise ValueError("origin must contain only a safe origin")
        counts = dict(self.counts or {})
        if not all(_SAFE_COUNT.fullmatch(key) and isinstance(value, int) and not isinstance(value, bool) and value >= 0 for key, value in counts.items()):
            raise ValueError("counts must be non-negative integers with safe keys")
        object.__setattr__(self, "codes", codes)
        object.__setattr__(self, "evidence_ids", evidence)
        object.__setattr__(self, "counts", counts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "codes": [code.value for code in self.codes],
            "evidence_ids": list(self.evidence_ids),
            "origin": self.origin,
            "counts": dict(self.counts or {}),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

"""Versioned, immutable and privacy-safe site configuration schema."""

from __future__ import annotations

import json
import math
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import FrozenInstanceError, dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from soupsieve.util import SelectorSyntaxError

CURRENT_SCHEMA_VERSION = 1
_V1_FIELDS = frozenset({"schema_version", "config_id", "site", "domain", "url_patterns", "selectors", "request_policy", "generated_at", "last_validated", "field_scores", "validation_samples", "fingerprint_salt"})
_FIELDS = _V1_FIELDS
_CONFIG_ID = re.compile(r"cfg_[A-Za-z0-9_-]{16,80}")
_SCORE_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_SECRET = re.compile(r"(?:token|secret|password|passwd|api[_-]?key|session|authorization)", re.I)
_SAMPLE_FIELDS = frozenset({"page_kind", "matched_fields", "node_count_bucket", "selector_match_counts", "success", "fingerprint"})
_LDH_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_PATH_LITERAL = re.compile(r"[A-Za-z0-9._~%-]*")
_PERCENT = re.compile(r"%(?:[0-9A-Fa-f]{2})")
_UNSAFE_SELECTOR = re.compile(r"(?::has\s*\(|:contains\s*\(|:-soup-contains(?:-own)?\s*\(|https?://|@|[?&]|token|secret|password|session)", re.I)


def _utc(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be an ISO UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO UTC string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{field} must use UTC")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _domain(value: object) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "/@?#:"):
        raise ValueError("domain must be a bare hostname")
    try:
        normalized = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("domain is not valid IDNA") from exc
    if len(normalized) > 253 or "." not in normalized or any(not _LDH_LABEL.fullmatch(part) for part in normalized.split(".")):
        raise ValueError("domain is not a valid hostname")
    return normalized


@dataclass(frozen=True)
class SafeUrlPattern:
    template: str
    _regex: re.Pattern[str] = field(repr=False, compare=False)

    @classmethod
    def parse(cls, value: object, domain: str) -> SafeUrlPattern:
        if not isinstance(value, str) or not value or len(value) > 512 or _SECRET.search(value):
            raise ValueError("URL template is unsafe")
        parsed = urlsplit(value)
        prefix = ""
        if parsed.scheme:
            if parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("absolute URL templates must use https without secrets")
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("URL template port is invalid") from exc
            host = _domain(parsed.hostname)
            if host != domain:
                raise ValueError("absolute URL template host must match configured domain")
            prefix = rf"https://{re.escape(host)}" + (rf":{port}" if port is not None else "")
        elif parsed.netloc or parsed.query or parsed.fragment or not value.startswith("/"):
            raise ValueError("URL template must be absolute https or relative")
        parts = parsed.path.split("/")[1:]
        compiled: list[str] = []
        for index, part in enumerate(parts):
            if part == "**":
                if index != len(parts) - 1:
                    raise ValueError("** is allowed only as the final path segment")
                compiled.append(".*")
            elif part == "*":
                compiled.append("[^/]+")
            elif part == "{int}":
                compiled.append("[0-9]+")
            elif part == "{slug}":
                compiled.append("[A-Za-z0-9][A-Za-z0-9._~-]*")
            else:
                if not _PATH_LITERAL.fullmatch(part) or re.sub(_PERCENT, "", part).find("%") >= 0:
                    raise ValueError("URL template contains unsafe path syntax")
                compiled.append(re.escape(part))
        return cls(value, re.compile(rf"^{prefix}/{'/'.join(compiled)}$"))

    def matches(self, url: str) -> bool:
        return bool(self._regex.fullmatch(url))


def _pattern(value: object, domain: str) -> SafeUrlPattern:
    return SafeUrlPattern.parse(value, domain)


def _selector(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or _UNSAFE_SELECTOR.search(value):
        raise ValueError("selector is unsafe")
    if len(re.findall(r"\s+|[>+~]", value)) > 3:
        raise ValueError("selector nesting is too deep")
    try:
        BeautifulSoup("", "html.parser").select(value)
    except SelectorSyntaxError as exc:
        raise ValueError("selector syntax is invalid") from exc
    return value


def validate_selector_collection(value: Sequence[object]) -> tuple[str, ...]:
    if len(value) > 20:
        raise ValueError("at most 20 selectors are allowed")
    if sum(len(selector) for selector in value if isinstance(selector, str)) > 4096:
        raise ValueError("selectors exceed total length limit of 4096")
    return tuple(_selector(selector) for selector in value)


def validate_candidate_selectors(value: Mapping[str, str]) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        raise TypeError("candidate selectors must be a mapping")
    if not all(isinstance(key, str) and _SCORE_KEY.fullmatch(key) and isinstance(selector, str) for key, selector in value.items()):
        raise TypeError("candidate selectors must map safe names to selectors")
    return validate_selector_collection(tuple(value.values()))


def _selectors(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"clean", "book", "chapter"}:
        raise ValueError("selectors must contain exactly clean, book and chapter")
    clean_raw = value["clean"]
    if not isinstance(clean_raw, Sequence) or isinstance(clean_raw, str):
        raise TypeError("clean selectors must be a sequence")
    raw_groups: dict[str, Mapping[str, object]] = {}
    for kind in ("book", "chapter"):
        raw = value[kind]
        if not isinstance(raw, Mapping) or not all(isinstance(key, str) and _SCORE_KEY.fullmatch(key) for key in raw):
            raise TypeError(f"{kind} selectors must be a field mapping")
        raw_groups[kind] = raw
    all_values = tuple(clean_raw) + tuple(raw_groups["book"].values()) + tuple(raw_groups["chapter"].values())
    validated = iter(validate_selector_collection(all_values))
    clean = tuple(next(validated) for _ in clean_raw)
    result: dict[str, object] = {"clean": clean}
    for kind in ("book", "chapter"):
        result[kind] = MappingProxyType({key: next(validated) for key in raw_groups[kind]})
    return MappingProxyType(result)


def _request_policy(value: object) -> Mapping[str, int | float]:
    if not isinstance(value, Mapping) or set(value) != {"timeout_seconds", "max_retries", "rate_limit_seconds"}:
        raise ValueError("request_policy has invalid fields")
    timeout, retries, rate = value["timeout_seconds"], value["max_retries"], value["rate_limit_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("timeout_seconds is invalid")
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 10:
        raise ValueError("max_retries is invalid")
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not math.isfinite(rate) or not 0 <= rate <= 60:
        raise ValueError("rate_limit_seconds is invalid")
    return MappingProxyType({"timeout_seconds": float(timeout), "max_retries": retries, "rate_limit_seconds": float(rate)})


def _scores(value: object) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        raise TypeError("field_scores must be a mapping")
    result: dict[str, float] = {}
    for key, score in value.items():
        if not isinstance(key, str) or not _SCORE_KEY.fullmatch(key) or isinstance(score, bool) or not isinstance(score, int | float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("field_scores must contain safe names and finite values from 0 to 1")
        result[key] = float(score)
    return MappingProxyType(result)


def _samples(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise TypeError("validation_samples must be a sequence")
    output = []
    for sample in value:
        if not isinstance(sample, Mapping) or not set(sample) <= _SAMPLE_FIELDS or "page_kind" not in sample:
            raise ValueError("validation samples may contain structural summaries only")
        if sample["page_kind"] not in {"book", "chapter", "catalog"}:
            raise ValueError("invalid sample page_kind")
        clean: dict[str, object] = {}
        for key, item in sample.items():
            if key == "selector_match_counts":
                if not isinstance(item, Mapping) or not all(isinstance(k, str) and _SCORE_KEY.fullmatch(k) and isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1_000_000 for k, v in item.items()):
                    raise ValueError("invalid selector match summary")
                clean[key] = MappingProxyType(dict(item))
            elif key == "fingerprint" and (not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)):
                raise ValueError("invalid structural fingerprint")
            elif key == "matched_fields" and (not isinstance(item, int) or isinstance(item, bool) or item < 0):
                raise ValueError("invalid matched_fields")
            elif key == "node_count_bucket" and (not isinstance(item, str) or not re.fullmatch(r"(?:0|[0-9]+-[0-9]+|[0-9]+\+)", item)):
                raise ValueError("invalid node_count_bucket")
            elif key == "success" and not isinstance(item, bool):
                raise ValueError("invalid success flag")
            else:
                clean[key] = item
        output.append(MappingProxyType(clean))
    return tuple(output)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class SiteConfig:
    __slots__ = tuple(f"_{name}" for name in _FIELDS)

    def __init__(self, **values: object) -> None:
        if set(values) != _FIELDS:
            missing, unknown = _FIELDS - set(values), set(values) - _FIELDS
            raise ValueError(f"invalid config fields; missing={sorted(missing)!r}, unknown={sorted(unknown)!r}")
        if values["schema_version"] != CURRENT_SCHEMA_VERSION or isinstance(values["schema_version"], bool):
            raise ValueError(f"unsupported schema_version: {values['schema_version']!r}")
        config_id = values["config_id"]
        site = values["site"]
        if not isinstance(config_id, str) or not _CONFIG_ID.fullmatch(config_id):
            raise ValueError("config_id is invalid")
        if not isinstance(site, str) or not site.strip() or len(site) > 120 or any(ord(c) < 32 for c in site):
            raise ValueError("site is invalid")
        domain = _domain(values["domain"])
        patterns_raw = values["url_patterns"]
        if not isinstance(patterns_raw, Sequence) or isinstance(patterns_raw, str) or not patterns_raw:
            raise TypeError("url_patterns must be a non-empty sequence")
        normalized = {
            "schema_version": CURRENT_SCHEMA_VERSION, "config_id": config_id, "site": site.strip(), "domain": domain,
            "url_patterns": tuple(_pattern(item, domain) for item in patterns_raw), "selectors": _selectors(values["selectors"]),
            "request_policy": _request_policy(values["request_policy"]), "generated_at": _utc(values["generated_at"], "generated_at"),
            "last_validated": _utc(values["last_validated"], "last_validated"), "field_scores": _scores(values["field_scores"]),
            "validation_samples": _samples(values["validation_samples"]),
            "fingerprint_salt": _salt(values["fingerprint_salt"]),
        }
        for name, value in normalized.items():
            object.__setattr__(self, f"_{name}", value)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError(f"cannot assign to field '{name}'")

    schema_version = property(lambda self: self._schema_version)
    config_id = property(lambda self: self._config_id)
    site = property(lambda self: self._site)
    domain = property(lambda self: self._domain)
    url_patterns = property(lambda self: self._url_patterns)
    selectors = property(lambda self: self._selectors)
    request_policy = property(lambda self: self._request_policy)
    generated_at = property(lambda self: self._generated_at)
    last_validated = property(lambda self: self._last_validated)
    field_scores = property(lambda self: self._field_scores)
    validation_samples = property(lambda self: self._validation_samples)
    fingerprint_salt = property(lambda self: self._fingerprint_salt)

    def _selector_count(self) -> int:
        clean = self.selectors["clean"]
        book = self.selectors["book"]
        chapter = self.selectors["chapter"]
        assert isinstance(clean, tuple) and isinstance(book, Mapping) and isinstance(chapter, Mapping)
        return len(clean) + len(book) + len(chapter)

    def __repr__(self) -> str:
        return f"SiteConfig(schema_version={self.schema_version}, config_id_present=True, site_present=True, domain_present=True, url_pattern_count={len(self.url_patterns)}, selector_count={self._selector_count()}, fingerprint_salt_present=True)"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SiteConfig) and self.to_dict(include_sensitive=True) == other.to_dict(include_sensitive=True)

    def to_dict(self, *, include_sensitive: bool = False) -> dict[str, object]:
        data = {name: _thaw(getattr(self, name)) for name in _FIELDS if name not in {"domain", "url_patterns", "selectors", "fingerprint_salt"}}
        if include_sensitive:
            data.update(domain=self.domain, url_patterns=[item.template for item in self.url_patterns], selectors=_thaw(self.selectors), fingerprint_salt=self.fingerprint_salt.hex())
        return data

    def to_json(self, *, include_sensitive: bool = False) -> str:
        return json.dumps(self.to_dict(include_sensitive=include_sensitive), ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def safe_summary(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "config_id_present": True, "site_present": True, "domain_present": True, "url_pattern_count": len(self.url_patterns), "selector_count": self._selector_count(), "generated_at": self.generated_at, "last_validated": self.last_validated, "fingerprint_salt_present": True}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SiteConfig:
        if not isinstance(value, Mapping):
            raise TypeError("config must be a mapping")
        return parse_config(value)

    @classmethod
    def from_json(cls, value: str) -> SiteConfig:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("config JSON is invalid") from exc
        return cls.from_dict(decoded)

    @classmethod
    def new(cls, *, site: str, domain: str, url_patterns: Sequence[str], selectors: Mapping[str, object], request_policy: Mapping[str, object] | None = None, generated_at: str | None = None) -> SiteConfig:
        now = generated_at or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        return cls(schema_version=CURRENT_SCHEMA_VERSION, config_id=f"cfg_{secrets.token_urlsafe(18)}", site=site, domain=domain, url_patterns=url_patterns, selectors=selectors, request_policy=request_policy or {"timeout_seconds": 15.0, "max_retries": 2, "rate_limit_seconds": 0.5}, generated_at=now, last_validated=now, field_scores={}, validation_samples=[], fingerprint_salt=secrets.token_bytes(32))


def _salt(value: object) -> bytes:
    if isinstance(value, bytes) and len(value) == 32:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return bytes.fromhex(value)
    raise ValueError("fingerprint_salt must encode exactly 32 bytes")


def _parse_v1(value: Mapping[str, object]) -> dict[str, object]:
    if set(value) != _V1_FIELDS:
        missing, unknown = _V1_FIELDS - set(value), set(value) - _V1_FIELDS
        raise ValueError(f"invalid v1 config fields; missing={sorted(missing)!r}, unknown={sorted(unknown)!r}")
    return dict(value)


_VERSION_PARSERS = {1: _parse_v1}
Migration = Callable[[dict[str, object]], dict[str, object]]
MIGRATIONS: dict[int, Migration] = {}


def parse_config(value: Mapping[str, object], *, migrations: Mapping[int, Migration] | None = None) -> SiteConfig:
    if not isinstance(value, Mapping):
        raise TypeError("config must be a mapping")
    raw_version = value.get("schema_version")
    if not isinstance(raw_version, int) or isinstance(raw_version, bool):
        raise ValueError(f"unsupported schema_version: {raw_version!r}")
    parser = _VERSION_PARSERS.get(raw_version)
    if parser is None or raw_version > CURRENT_SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {raw_version!r}")
    payload = parser(value)
    registry = MIGRATIONS if migrations is None else migrations
    version = raw_version
    while version < CURRENT_SCHEMA_VERSION:
        migration = registry.get(version)
        if migration is None:
            raise ValueError(f"missing migration from schema_version {version}")
        payload = migration(dict(payload))
        expected = version + 1
        if payload.get("schema_version") != expected:
            raise ValueError(f"migration from schema_version {version} must produce {expected}")
        version = expected
    return SiteConfig(**payload)


def migrate_v1_to_current(value: Mapping[str, object], *, migrations: Mapping[int, Migration] | None = None) -> SiteConfig:
    """Parse v1 independently; future migrations can chain from this boundary."""
    if value.get("schema_version") != 1:
        raise ValueError("migrate_v1_to_current requires schema_version 1")
    return parse_config(value, migrations=migrations)

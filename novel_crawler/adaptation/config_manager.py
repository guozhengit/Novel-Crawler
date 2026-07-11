"""Privacy-safe orchestration of config reuse, probing and registration."""

from __future__ import annotations

import json
import re
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from .config_schema import SafeUrlPattern, SiteConfig, validate_candidate_selectors
from .decision import DecisionKind
from .fingerprint import StructureFingerprint
from .registry import ConfigConflictError, ConfigRegistry, ConfigStatus, RegistryEntry
from .revalidation import ConfigRevalidator, RevalidationStatus
from .service import ProbeService
from .url_paths import canonical_path, path_template
from .validation import ConfigDraft, ValidationResult

_SAFE_REASON = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_RESOLUTION_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_RESOLUTION_LOCKS_GUARD = threading.Lock()
_COLLABORATOR_LOCKS: dict[int, threading.RLock] = {}
_COLLABORATOR_LOCKS_GUARD = threading.Lock()


class ResolutionKind(StrEnum):
    REUSED = "reused"
    REGISTERED = "registered"
    CONFIRMATION_REQUIRED = "confirmation_required"
    REJECTED = "rejected"
    TRANSIENT_FAILURE = "transient_failure"


class ConfigResolution:
    """Immutable result; sensitive handles require explicit property access."""

    __slots__ = ("_config", "_confirmation_token", "_kind", "_reason_ids")

    def __init__(
        self,
        kind: ResolutionKind,
        *,
        config: SiteConfig | None = None,
        confirmation_token: str | None = None,
        reason_ids: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(kind, ResolutionKind):
            raise TypeError("kind must be ResolutionKind")
        reasons = tuple(dict.fromkeys(reason_ids))
        if not all(isinstance(item, str) and _SAFE_REASON.fullmatch(item) for item in reasons):
            raise ValueError("reason_ids must be safe identifiers")
        if kind in {ResolutionKind.REUSED, ResolutionKind.REGISTERED}:
            if config is None or confirmation_token is not None:
                raise ValueError("reused and registered resolutions require only a config")
        elif kind is ResolutionKind.CONFIRMATION_REQUIRED:
            if not confirmation_token or config is not None:
                raise ValueError("confirmation-required resolutions require only a token")
        elif config is not None or confirmation_token is not None:
            raise ValueError("rejected and transient resolutions cannot expose sensitive handles")
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "_confirmation_token", confirmation_token)
        object.__setattr__(self, "_reason_ids", reasons)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("ConfigResolution is immutable")

    kind = property(lambda self: self._kind)
    config = property(lambda self: self._config)
    confirmation_token = property(lambda self: self._confirmation_token)
    reason_ids = property(lambda self: self._reason_ids)

    def __repr__(self) -> str:
        return f"ConfigResolution(kind={self.kind.value!r}, config_present={self.config is not None!r}, confirmation_required={self.confirmation_token is not None!r}, reason_ids={self.reason_ids!r})"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "config_present": self.config is not None,
            "confirmation_required": self.confirmation_token is not None,
            "reason_ids": list(self.reason_ids),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


class _Registry(Protocol):
    def lookup(self, url: str) -> RegistryEntry | None: ...
    def load(self, entry_or_id: RegistryEntry | str, *, version: int | None = None) -> SiteConfig: ...
    def register(self, config: SiteConfig) -> RegistryEntry: ...


class _Revalidator(Protocol):
    def revalidate(self, entry: RegistryEntry, url: str) -> Any: ...


class _Probe(Protocol):
    def probe(self, url: str, *, overrides: Mapping[str, str] | None = None) -> ValidationResult: ...


@dataclass(frozen=True)
class _PendingConfig:
    url: str
    draft: ConfigDraft
    config: SiteConfig
    expires_at: datetime
    overrides: tuple[tuple[str, str], ...] | None = None


class ConfigManager:
    def __init__(
        self,
        registry: ConfigRegistry | _Registry,
        revalidator: ConfigRevalidator | _Revalidator,
        probe: ProbeService | _Probe,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.registry = registry
        self.revalidator = revalidator
        self.probe = probe
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pending: dict[str, _PendingConfig] = {}
        self._pending_lock = threading.RLock()
        self._token_locks: dict[str, threading.RLock] = {}

    def resolve(self, url: str) -> ConfigResolution:
        try:
            domain = self._url_parts(url)[0]
        except ValueError:
            return ConfigResolution(ResolutionKind.REJECTED, reason_ids=("input_url_invalid",))
        try:
            guard = self._resolution_guard(domain)
            with guard:
                for _attempt in range(3):
                    try:
                        return self._resolve_locked(url)
                    except ConfigConflictError:
                        continue
                return ConfigResolution(ResolutionKind.TRANSIENT_FAILURE, reason_ids=("concurrent_revision",))
        except Exception:
            return ConfigResolution(ResolutionKind.TRANSIENT_FAILURE, reason_ids=("registry_unavailable",))

    def _resolve_locked(self, url: str) -> ConfigResolution:
        try:
            entry = self.registry.lookup(url)
        except Exception:
            return ConfigResolution(ResolutionKind.TRANSIENT_FAILURE, reason_ids=("registry_unavailable",))
        if entry is not None:
            try:
                with self._collaborator_lock(self.revalidator):
                    checked = self.revalidator.revalidate(entry, url)
            except Exception:
                return ConfigResolution(ResolutionKind.TRANSIENT_FAILURE, reason_ids=("revalidation_unavailable",))
            if "concurrent_revision" in checked.reason_ids:
                raise ConfigConflictError("revalidation revision changed")
            if checked.status is RevalidationStatus.VALID:
                try:
                    if "concurrent_revision" in checked.reason_ids or checked.entry is None:
                        raise ConfigConflictError("revalidation did not produce an exact revision")
                    if checked.entry.config_id != entry.config_id or checked.entry.domain != entry.domain:
                        raise ConfigConflictError("revalidation identity changed")
                    load_exact = getattr(self.registry, "load_exact", None)
                    if not callable(load_exact):
                        raise ConfigConflictError("registry cannot guarantee exact revision loading")
                    config = load_exact(checked.entry.config_id, checked.entry.version, ConfigStatus.ACTIVE)
                    return ConfigResolution(ResolutionKind.REUSED, config=config)
                except ConfigConflictError:
                    raise
                except Exception:
                    return ConfigResolution(ResolutionKind.TRANSIENT_FAILURE, reason_ids=("registry_unavailable",))
            if checked.status is RevalidationStatus.TRANSIENT_FAILURE:
                return ConfigResolution(ResolutionKind.TRANSIENT_FAILURE, reason_ids=tuple(checked.reason_ids))
        return self._from_probe(url)

    def confirm(self, token: str, selector_overrides: Mapping[str, str] | None = None) -> ConfigResolution:
        with self._pending_lock:
            pending = self._pending.get(token)
            if pending is None or pending.expires_at <= self._now():
                self._pending.pop(token, None)
                self._token_locks.pop(token, None)
                raise KeyError("unknown or expired confirmation token")
            pending_url = pending.url
            token_lock = self._token_locks[token]
        domain = self._url_parts(pending_url)[0]
        with token_lock, self._resolution_guard(domain):
            with self._pending_lock:
                pending = self._pending.get(token)
                if pending is None or pending.expires_at <= self._now():
                    self._pending.pop(token, None)
                    self._token_locks.pop(token, None)
                    raise KeyError("unknown or expired confirmation token")
                config = pending.config
                normalized_overrides = tuple(sorted(selector_overrides.items())) if selector_overrides else None
                if pending.overrides is not None and normalized_overrides not in {None, pending.overrides}:
                    raise ValueError("confirmation overrides cannot change after registration starts")
                needs_probe = normalized_overrides is not None and pending.overrides is None
            if needs_probe:
                validated = dict(selector_overrides or {})
                validate_candidate_selectors(validated)
                result = self.probe.probe(pending_url, overrides=validated)
                if result.outcome is DecisionKind.REJECT or result.config_draft is None:
                    raise ValueError("selector overrides failed three-page validation")
                draft = result.config_draft
                rematerialized = self._materialize(pending_url, draft)
                if rematerialized is None:
                    raise ValueError("pending config is incomplete")
                with self._pending_lock:
                    current = self._pending.get(token)
                    if current is not pending or current.expires_at <= self._now():
                        raise KeyError("confirmation token was cancelled or expired")
                    pending = _PendingConfig(pending_url, draft, rematerialized, current.expires_at, normalized_overrides)
                    self._pending[token] = pending
                    config = rematerialized
            try:
                resolution = self._register(config, pending_url)
            except ConfigConflictError:
                resolution = None
            if resolution is not None:
                with self._pending_lock:
                    if self._pending.get(token) is not pending:
                        raise KeyError("confirmation token was cancelled")
                    del self._pending[token]
                    self._token_locks.pop(token, None)
        if resolution is None:
            resolution = self.resolve(pending_url)
            with self._pending_lock:
                self._pending.pop(token, None)
                self._token_locks.pop(token, None)
        return resolution

    def cancel(self, token: str) -> bool:
        with self._pending_lock:
            pending = self._pending.get(token)
            if pending is None or pending.expires_at <= self._now():
                self._pending.pop(token, None)
                self._token_locks.pop(token, None)
                return False
            token_lock = self._token_locks[token]
        with token_lock, self._pending_lock:
            if token not in self._pending:
                return False
            del self._pending[token]
            self._token_locks.pop(token, None)
        return True

    def _from_probe(self, url: str) -> ConfigResolution:
        try:
            with self._collaborator_lock(self.probe):
                result = self.probe.probe(url)
        except Exception:
            return ConfigResolution(ResolutionKind.TRANSIENT_FAILURE, reason_ids=("probe_unavailable",))
        if result.outcome is DecisionKind.REJECT or result.config_draft is None:
            return ConfigResolution(ResolutionKind.REJECTED, reason_ids=tuple(result.reason_ids) or ("probe_rejected",))
        if result.outcome is DecisionKind.REQUIRE_CONFIRMATION:
            try:
                config = self._materialize(url, result.config_draft)
            except (TypeError, ValueError):
                return ConfigResolution(ResolutionKind.REJECTED, reason_ids=("config_materialization_invalid",))
            if config is None:
                return ConfigResolution(ResolutionKind.REJECTED, reason_ids=("fingerprint_baseline_missing",))
            token = secrets.token_urlsafe(32)
            with self._pending_lock:
                self._purge_expired()
                self._pending[token] = _PendingConfig(url, result.config_draft, config, self._now() + timedelta(minutes=10))
                self._token_locks[token] = threading.RLock()
            return ConfigResolution(ResolutionKind.CONFIRMATION_REQUIRED, confirmation_token=token)
        try:
            config = self._materialize(url, result.config_draft)
        except (TypeError, ValueError):
            return ConfigResolution(ResolutionKind.REJECTED, reason_ids=("config_materialization_invalid",))
        if config is None:
            return ConfigResolution(ResolutionKind.REJECTED, reason_ids=("fingerprint_baseline_missing",))
        try:
            return self._register(config, url)
        except ConfigConflictError:
            raise
        except Exception:
            return ConfigResolution(ResolutionKind.TRANSIENT_FAILURE, reason_ids=("registry_unavailable",))

    def _materialize(self, url: str, draft: ConfigDraft) -> SiteConfig | None:
        sensitive = draft.to_config()
        salt = sensitive.get("fingerprint_salt")
        fingerprints = sensitive.get("fingerprints")
        selectors = sensitive.get("selectors")
        if not isinstance(salt, bytes) or not isinstance(fingerprints, Mapping) or not isinstance(selectors, Mapping):
            return None
        if set(fingerprints) != {"book", "chapter_first", "chapter_second"}:
            return None
        domain = self._url_parts(url)[0]
        if draft.domain.rstrip(".").encode("idna").decode("ascii").lower() != domain:
            return None
        book_fields = {"title", "author", "chapter_list"}
        book = {key: value for key, value in selectors.items() if key in book_fields}
        chapter = {key: value for key, value in selectors.items() if key not in book_fields and key != "clean_selector"}
        clean_selector = selectors.get("clean_selector")
        clean = (clean_selector,) if isinstance(clean_selector, str) else ()
        raw_paths = sensitive.get("navigation_paths")
        if not isinstance(raw_paths, tuple) or len(raw_paths) != 3:
            return None
        canonical_paths = tuple(dict.fromkeys(canonical_path(path) for path in raw_paths))
        if len(canonical_paths) != 3:
            return None
        patterns = tuple(dict.fromkeys(path_template(path) for path in canonical_paths))
        if not all(any(SafeUrlPattern.parse(template, domain).matches(path) for template in patterns) for path in canonical_paths):
            return None
        samples = []
        for label in ("book", "chapter_first", "chapter_second"):
            fingerprint = fingerprints[label]
            if not isinstance(fingerprint, StructureFingerprint):
                return None
            samples.append({"page_kind": label, "fingerprint": fingerprint.to_dict()})
        return SiteConfig.create(
            site=domain,
            domain=domain,
            url_patterns=patterns,
            selectors={"clean": clean, "book": book, "chapter": chapter},
            validation_samples=samples,
            fingerprint_salt=salt,
            field_scores=draft.scores,
        )

    @staticmethod
    def _url_parts(url: str) -> tuple[str, str]:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
                raise ValueError("invalid URL")
            domain = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
            _port = parsed.port
        except (UnicodeError, ValueError):
            raise ValueError("invalid URL") from None
        path = canonical_path(parsed.path)
        return domain, path_template(path)

    def _domain_lock(self, domain: str) -> threading.RLock:
        root = getattr(self.registry, "root", None)
        registry_key = str(root.resolve(strict=False)) if root is not None else f"instance:{id(self.registry)}"
        with _RESOLUTION_LOCKS_GUARD:
            return _RESOLUTION_LOCKS.setdefault((registry_key, domain), threading.RLock())

    @contextmanager
    def _resolution_guard(self, domain: str) -> Iterator[None]:
        lock = self._domain_lock(domain)
        with lock:
            registry_guard = getattr(self.registry, "resolution_lock", None)
            if callable(registry_guard):
                with registry_guard(domain):
                    yield
            else:
                yield

    @staticmethod
    def _collaborator_lock(collaborator: object) -> threading.RLock:
        with _COLLABORATOR_LOCKS_GUARD:
            return _COLLABORATOR_LOCKS.setdefault(id(collaborator), threading.RLock())

    def _register(self, config: SiteConfig, url: str) -> ConfigResolution:
        existing = self.registry.lookup(url)
        if existing is not None and existing.config_id != config.config_id:
            raise ConfigConflictError("matching config requires exact revalidation")
        entry = self.registry.register(config)
        status = getattr(entry, "status", ConfigStatus.ACTIVE)
        if status is not ConfigStatus.ACTIVE:
            raise ConfigConflictError("registered config is not active")
        return ConfigResolution(ResolutionKind.REGISTERED, config=config)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("clock must return timezone-aware values")
        return value.astimezone(UTC)

    def _purge_expired(self) -> None:
        now = self._now()
        for token in [key for key, pending in self._pending.items() if pending.expires_at <= now]:
            del self._pending[token]
            self._token_locks.pop(token, None)

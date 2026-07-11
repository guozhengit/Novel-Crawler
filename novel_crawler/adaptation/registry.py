"""Crash-safe, privacy-aware persistence for versioned site configurations."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config_schema import SiteConfig

_REGISTRY_SCHEMA_VERSION = 1
_REVISION_NAME = re.compile(r"rev-([0-9]{6})\.json")
_HASH_NAME = re.compile(r"[0-9a-f]{64}")
_REASON_ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class RegistryError(RuntimeError):
    """Base class for registry failures whose messages are safe to expose."""


class ConfigConflictError(RegistryError):
    """A config id was reused for different content."""


class RegistryLimitError(RegistryError):
    """A bounded recovery or write limit was exceeded."""


class RegistryLockTimeout(RegistryError):
    """A registry lock could not be acquired within its bounded timeout."""


class ConfigStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    INVALID = "invalid"
    REVOKED = "revoked"


@dataclass(frozen=True)
class RegistryEntry:
    """Non-sensitive registry metadata; use ``ConfigRegistry.load`` for content."""

    config_id: str
    domain: str
    status: ConfigStatus
    version: int
    created: str
    validated: str
    invalid_reason_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Record:
    entry: RegistryEntry
    path: Path
    digest: str


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _reject_symlink_components(path: Path) -> None:
    current = path.absolute()
    for component in (current, *current.parents):
        if _is_link_or_reparse_point(component):
            raise RegistryError("registry path components must not be symlinks or reparse points")


def _is_link_or_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _private_directory(path: Path) -> None:
    _reject_symlink_components(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlink_components(path)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _private_file(path: Path) -> None:
    _reject_symlink_components(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    _private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _private_file(temporary)
        os.replace(temporary, path)
        _private_file(path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _thread_lock(name: str) -> threading.RLock:
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(name, threading.RLock())


@contextmanager
def _bounded_thread_lock(lock: threading.RLock, timeout: float) -> Iterator[None]:
    if not lock.acquire(timeout=timeout):
        raise RegistryLockTimeout("registry lock acquisition timed out")
    try:
        yield
    finally:
        lock.release()


class _FileLock:
    def __init__(self, path: Path, timeout: float) -> None:
        self._path = path
        self._timeout = timeout
        self._stream: Any = None

    def __enter__(self) -> _FileLock:
        _private_directory(self._path.parent)
        _reject_symlink_components(self._path)
        self._stream = self._path.open("a+b")
        _private_file(self._path)
        self._stream.seek(0, os.SEEK_END)
        if self._stream.tell() == 0:
            self._stream.write(b"\0")
            self._stream.flush()
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                self._try_lock()
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._stream.close()
                    self._stream = None
                    raise RegistryLockTimeout("registry lock acquisition timed out") from None
                time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))

    def _try_lock(self) -> None:
        assert self._stream is not None
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl: Any = importlib.import_module("fcntl")
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl: Any = importlib.import_module("fcntl")
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None


class ConfigRegistry:
    """Store immutable config revisions and expose only safe metadata by default."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        lock_timeout: float = 5.0,
        max_files: int = 10_000,
        max_config_bytes: int = 1_048_576,
    ) -> None:
        if lock_timeout <= 0:
            raise ValueError("lock_timeout must be positive")
        if max_files < 0 or max_config_bytes <= 0:
            raise ValueError("registry limits are invalid")
        self.root = Path(root).absolute()
        self._lock_timeout = float(lock_timeout)
        self._max_files = max_files
        self._max_config_bytes = max_config_bytes
        self._max_manifest_bytes = max(4096, max_files * 6144)
        self._configs = self.root / "configs"
        self._locks = self.root / "locks"
        self._quarantine = self.root / "quarantine"
        self._manifest = self.root / "manifest.json"
        for directory in (self.root, self._configs, self._locks, self._quarantine):
            _private_directory(directory)
        self._history: dict[str, list[_Record]] = {}
        with self._global_lock():
            self._recover()

    @contextmanager
    def _global_lock(self) -> Iterator[None]:
        key = str(self.root.resolve(strict=False))
        with _bounded_thread_lock(_thread_lock(key), self._lock_timeout):
            with _FileLock(self._locks / "registry.lock", self._lock_timeout):
                yield

    @contextmanager
    def _config_lock(self, config_id: str) -> Iterator[None]:
        name = _hash(config_id)
        key = f"{self.root.resolve(strict=False)}:{name}"
        with _bounded_thread_lock(_thread_lock(key), self._lock_timeout):
            with _FileLock(self._locks / f"{name}.lock", self._lock_timeout):
                yield

    def register(self, config: SiteConfig) -> RegistryEntry:
        if not isinstance(config, SiteConfig):
            raise TypeError("config must be a SiteConfig")
        config_payload = config.to_dict(include_sensitive=True)
        config_bytes = _canonical_json(config_payload)
        if len(config_bytes) > self._max_config_bytes:
            raise RegistryLimitError("config exceeds maximum bytes")
        digest = hashlib.sha256(config_bytes).hexdigest()
        with self._config_lock(config.config_id):
            with self._global_lock():
                self._recover()
                records = self._history.get(config.config_id, [])
                if records:
                    if records[0].digest != digest:
                        raise ConfigConflictError(f"config id {config.config_id} conflicts with stored content")
                    return records[-1].entry
                if sum(len(items) for items in self._history.values()) >= self._max_files:
                    raise RegistryLimitError("registry exceeds maximum files")
                entry = RegistryEntry(
                    config_id=config.config_id,
                    domain=config.domain,
                    status=ConfigStatus.ACTIVE,
                    version=1,
                    created=config.generated_at,
                    validated=config.last_validated,
                )
                record = self._write_revision(entry, config_payload, digest)
                self._history[config.config_id] = [record]
                self._write_manifest()
                return entry

    def lookup(self, url: str) -> RegistryEntry | None:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
                return None
            domain = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
            port = parsed.port
        except (UnicodeError, ValueError):
            return None
        relative = parsed.path or "/"
        absolute = f"{parsed.scheme}://{domain}" + (f":{port}" if port is not None else "") + relative
        with self._global_lock():
            self._recover()
            matches: list[RegistryEntry] = []
            for records in self._history.values():
                current = records[-1]
                if current.entry.status is not ConfigStatus.ACTIVE or current.entry.domain != domain:
                    continue
                config = self._load_record(current)
                if config is None:
                    continue
                if any(pattern.matches(relative) or pattern.matches(absolute) for pattern in config.url_patterns):
                    matches.append(current.entry)
            if not matches:
                return None
            return max(matches, key=lambda item: (item.validated, item.version, item.config_id))

    def load(self, entry_or_id: RegistryEntry | str, *, version: int | None = None) -> SiteConfig:
        selected_version: int | None
        if isinstance(entry_or_id, RegistryEntry):
            config_id = entry_or_id.config_id
            selected_version = entry_or_id.version if version is None else version
        elif isinstance(entry_or_id, str):
            config_id = entry_or_id
            selected_version = version
        else:
            raise TypeError("entry_or_id must be a RegistryEntry or config id")
        with self._global_lock():
            self._recover()
            records = self._history.get(config_id)
            if not records:
                raise KeyError(config_id)
            record = records[-1] if selected_version is None else next(
                (item for item in records if item.entry.version == selected_version), None
            )
            if record is None:
                raise KeyError(f"unknown config revision {selected_version}")
            loaded = self._load_record(record)
            if loaded is None:
                raise RegistryError("stored config revision is unavailable")
            return loaded

    def list(
        self,
        *,
        domain: str | None = None,
        status: ConfigStatus | None = None,
        include_history: bool = False,
    ) -> tuple[RegistryEntry, ...]:
        with self._global_lock():
            self._recover()
            entries = [
                record.entry
                for records in self._history.values()
                for record in (records if include_history else records[-1:])
            ]
        if domain is not None:
            entries = [entry for entry in entries if entry.domain == domain]
        if status is not None:
            entries = [entry for entry in entries if entry.status is status]
        return tuple(sorted(entries, key=lambda item: (item.config_id, item.version)))

    def mark_stale(self, config_id: str) -> RegistryEntry:
        return self._transition(config_id, ConfigStatus.STALE, ())

    def mark_invalid(self, config_id: str, invalid_reason_ids: Sequence[str]) -> RegistryEntry:
        reasons = tuple(sorted(set(invalid_reason_ids)))
        if len(reasons) > 64:
            raise ValueError("at most 64 invalid reason ids are allowed")
        if not reasons or any(not isinstance(reason, str) or not _REASON_ID.fullmatch(reason) for reason in reasons):
            raise ValueError("invalid reason ids must be non-empty safe identifiers")
        return self._transition(config_id, ConfigStatus.INVALID, reasons)

    def mark_revoked(self, config_id: str) -> RegistryEntry:
        return self._transition(config_id, ConfigStatus.REVOKED, ())

    def _transition(
        self, config_id: str, status: ConfigStatus, invalid_reason_ids: tuple[str, ...]
    ) -> RegistryEntry:
        with self._config_lock(config_id):
            with self._global_lock():
                self._recover()
                records = self._history.get(config_id)
                if not records:
                    raise KeyError(config_id)
                current = records[-1]
                if sum(len(items) for items in self._history.values()) >= self._max_files:
                    raise RegistryLimitError("registry exceeds maximum files")
                config = self._read_envelope(current.path).get("config")
                if not isinstance(config, dict):
                    raise RegistryError("stored config revision is unavailable")
                entry = replace(
                    current.entry,
                    status=status,
                    version=current.entry.version + 1,
                    invalid_reason_ids=invalid_reason_ids,
                )
                record = self._write_revision(entry, config, current.digest)
                records.append(record)
                self._write_manifest()
                return entry

    def _revision_path(self, entry: RegistryEntry) -> Path:
        return self._configs / _hash(entry.domain) / _hash(entry.config_id) / f"rev-{entry.version:06d}.json"

    def _write_revision(self, entry: RegistryEntry, config: dict[str, object], digest: str) -> _Record:
        path = self._revision_path(entry)
        entry_payload = self._entry_dict(entry)
        envelope = {
            "registry_schema_version": _REGISTRY_SCHEMA_VERSION,
            "entry": entry_payload,
            "content_sha256": digest,
            "revision_sha256": hashlib.sha256(_canonical_json({"entry": entry_payload, "config": config})).hexdigest(),
            "config": config,
        }
        payload = _canonical_json(envelope)
        if len(payload) > self._max_config_bytes:
            raise RegistryLimitError("config revision exceeds maximum bytes")
        if path.exists():
            raise RegistryError("config revision already exists")
        _atomic_write(path, payload)
        return _Record(entry, path, digest)

    @staticmethod
    def _entry_dict(entry: RegistryEntry) -> dict[str, object]:
        return {
            "config_id": entry.config_id,
            "domain": entry.domain,
            "status": entry.status.value,
            "version": entry.version,
            "created": entry.created,
            "validated": entry.validated,
            "invalid_reason_ids": list(entry.invalid_reason_ids),
        }

    @staticmethod
    def _parse_entry(value: object) -> RegistryEntry:
        if not isinstance(value, dict) or set(value) != {
            "config_id", "domain", "status", "version", "created", "validated", "invalid_reason_ids"
        }:
            raise ValueError("invalid entry metadata")
        reasons = value["invalid_reason_ids"]
        if (
            not isinstance(reasons, list)
            or len(reasons) > 64
            or any(not isinstance(item, str) or not _REASON_ID.fullmatch(item) for item in reasons)
        ):
            raise ValueError("invalid entry metadata")
        version = value["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("invalid entry metadata")
        strings = (value["config_id"], value["domain"], value["created"], value["validated"])
        if any(not isinstance(item, str) for item in strings):
            raise ValueError("invalid entry metadata")
        status = ConfigStatus(value["status"])
        if (status is ConfigStatus.INVALID) != bool(reasons):
            raise ValueError("invalid entry metadata")
        return RegistryEntry(
            config_id=value["config_id"],
            domain=value["domain"],
            status=status,
            version=version,
            created=value["created"],
            validated=value["validated"],
            invalid_reason_ids=tuple(reasons),
        )

    def _read_envelope(self, path: Path) -> dict[str, object]:
        if path.is_symlink() or not path.is_file():
            raise ValueError("unsafe revision path")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(self._configs.resolve(strict=True))
        except ValueError as exc:
            raise ValueError("unsafe revision path") from exc
        size = path.stat().st_size
        if size > self._max_config_bytes:
            raise RegistryLimitError("config revision exceeds maximum bytes")
        raw = path.read_bytes()
        decoded = json.loads(raw)
        if not isinstance(decoded, dict) or decoded.get("registry_schema_version") != _REGISTRY_SCHEMA_VERSION:
            raise ValueError("unsupported registry schema")
        if set(decoded) != {"registry_schema_version", "entry", "content_sha256", "revision_sha256", "config"}:
            raise ValueError("invalid revision envelope")
        revision_digest = decoded["revision_sha256"]
        if not isinstance(revision_digest, str) or not _HASH_NAME.fullmatch(revision_digest):
            raise ValueError("invalid revision digest")
        revision_payload = {"entry": decoded["entry"], "config": decoded["config"]}
        if hashlib.sha256(_canonical_json(revision_payload)).hexdigest() != revision_digest:
            raise ValueError("revision digest mismatch")
        return decoded

    def _load_record(self, record: _Record) -> SiteConfig | None:
        try:
            envelope = self._read_envelope(record.path)
            config = envelope["config"]
            if not isinstance(config, dict):
                raise ValueError("invalid config payload")
            canonical = _canonical_json(config)
            if hashlib.sha256(canonical).hexdigest() != record.digest:
                raise ValueError("config digest mismatch")
            return SiteConfig.from_dict(config)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._quarantine_path(record.path, "invalid_revision")
            return None

    def _recover(self) -> None:
        self._validate_or_quarantine_manifest()
        history: dict[str, list[_Record]] = {}
        for path in self._scan_revision_files():
            try:
                relative = path.relative_to(self._configs)
                if len(relative.parts) != 3:
                    raise ValueError("unexpected revision location")
                domain_hash, config_hash, filename = relative.parts
                match = _REVISION_NAME.fullmatch(filename)
                if not _HASH_NAME.fullmatch(domain_hash) or not _HASH_NAME.fullmatch(config_hash) or match is None:
                    raise ValueError("unexpected revision location")
                envelope = self._read_envelope(path)
                entry = self._parse_entry(envelope["entry"])
                if entry.version != int(match.group(1)):
                    raise ValueError("revision version mismatch")
                if _hash(entry.domain) != domain_hash or _hash(entry.config_id) != config_hash:
                    raise ValueError("revision path mismatch")
                digest = envelope["content_sha256"]
                config_payload = envelope["config"]
                if not isinstance(digest, str) or not _HASH_NAME.fullmatch(digest) or not isinstance(config_payload, dict):
                    raise ValueError("invalid revision content")
                canonical = _canonical_json(config_payload)
                if hashlib.sha256(canonical).hexdigest() != digest:
                    raise ValueError("config digest mismatch")
                parsed = SiteConfig.from_dict(config_payload)
                if (
                    parsed.config_id != entry.config_id
                    or parsed.domain != entry.domain
                    or parsed.generated_at != entry.created
                    or parsed.last_validated != entry.validated
                ):
                    raise ValueError("entry and config mismatch")
                history.setdefault(entry.config_id, []).append(_Record(entry, path, digest))
            except RegistryLimitError:
                raise
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                self._quarantine_path(path, "invalid_revision")
        for config_id, records in list(history.items()):
            records.sort(key=lambda item: item.entry.version)
            valid: list[_Record] = []
            for expected, record in enumerate(records, 1):
                if record.entry.version != expected or (valid and record.digest != valid[0].digest):
                    self._quarantine_path(record.path, "invalid_history")
                else:
                    valid.append(record)
            if valid:
                history[config_id] = valid
            else:
                del history[config_id]
        self._history = history
        self._write_manifest()

    def _scan_revision_files(self) -> Iterator[Path]:
        scan_entries = 0
        config_files = 0
        scan_limit = max(64, self._max_files * 4 + 16)
        for directory, dirnames, filenames in os.walk(self._configs, followlinks=False):
            dirnames.sort()
            filenames.sort()
            for name in tuple(dirnames):
                scan_entries += 1
                if scan_entries > scan_limit:
                    raise RegistryLimitError("registry exceeds maximum scan entries")
                candidate = Path(directory) / name
                if _is_link_or_reparse_point(candidate):
                    dirnames.remove(name)
                    self._quarantine_path(candidate, "unsafe_directory")
            for name in filenames:
                scan_entries += 1
                if scan_entries > scan_limit:
                    raise RegistryLimitError("registry exceeds maximum scan entries")
                if ".tmp" in name:
                    continue
                candidate = Path(directory) / name
                if candidate.suffix != ".json":
                    self._quarantine_path(candidate, "unknown_file")
                    continue
                config_files += 1
                if config_files > self._max_files:
                    raise RegistryLimitError("registry exceeds maximum files")
                yield candidate

    def _validate_or_quarantine_manifest(self) -> None:
        if not self._manifest.exists() and not self._manifest.is_symlink():
            return
        try:
            if self._manifest.is_symlink() or not self._manifest.is_file():
                raise ValueError("unsafe manifest")
            if self._manifest.stat().st_size > self._max_manifest_bytes:
                raise ValueError("oversized manifest")
            decoded = json.loads(self._manifest.read_bytes())
            if (
                not isinstance(decoded, dict)
                or set(decoded) != {"registry_schema_version", "entries"}
                or decoded.get("registry_schema_version") != _REGISTRY_SCHEMA_VERSION
                or not isinstance(decoded.get("entries"), list)
            ):
                raise ValueError("invalid manifest")
            for item in decoded["entries"]:
                self._parse_entry(item)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._quarantine_path(self._manifest, "invalid_manifest")

    def _quarantine_path(self, path: Path, reason: str) -> None:
        token = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        reason_path = self._quarantine / f"{token}.reason.json"
        _atomic_write(
            reason_path,
            _canonical_json({"registry_schema_version": _REGISTRY_SCHEMA_VERSION, "reason_id": reason, "source_hash": token}),
        )
        if path.is_symlink():
            return
        try:
            if path.exists() and path.is_file():
                destination = self._quarantine / f"{token}.bad"
                if not destination.exists():
                    os.replace(path, destination)
                    _private_file(destination)
        except OSError:
            pass

    def _write_manifest(self) -> None:
        entries = [record.entry for records in self._history.values() for record in records]
        payload = {
            "registry_schema_version": _REGISTRY_SCHEMA_VERSION,
            "entries": [self._entry_dict(entry) for entry in sorted(entries, key=lambda item: (item.config_id, item.version))],
        }
        encoded = _canonical_json(payload)
        if len(encoded) > self._max_manifest_bytes:
            raise RegistryLimitError("manifest exceeds maximum bytes")
        _atomic_write(self._manifest, encoded)


__all__ = [
    "ConfigConflictError",
    "ConfigRegistry",
    "ConfigStatus",
    "RegistryEntry",
    "RegistryError",
    "RegistryLimitError",
    "RegistryLockTimeout",
]

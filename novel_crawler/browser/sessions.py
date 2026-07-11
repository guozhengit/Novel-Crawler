"""Crash-safe, privacy-preserving storage for persistent browser profiles."""

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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from novel_crawler.adaptation.registry_io import RegistryIO, RegistryIOError, default_registry_io

_SCHEMA_VERSION = 1
_HASH_NAME = re.compile(r"[0-9a-f]{64}")
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class BrowserSessionError(RuntimeError):
    """Base class with messages safe to expose to callers."""


class SessionLockTimeout(BrowserSessionError):
    """A domain is already leased beyond the requested timeout."""


class SessionConflictError(BrowserSessionError):
    """A session state or identity precondition was not satisfied."""


class SessionConfirmationError(BrowserSessionError):
    """Destructive deletion was not explicitly confirmed."""


class SessionLimitError(BrowserSessionError):
    """A bounded storage or scan limit was exceeded."""


class BrowserSessionStatus(StrEnum):
    AVAILABLE = "available"
    IN_USE = "in_use"
    STALE = "stale"
    REVOKED = "revoked"


@dataclass(frozen=True)
class BrowserSessionInfo:
    """Safe metadata. Browser profile contents are intentionally absent."""

    session_id: str
    domain: str
    created: str
    last_used: str
    status: BrowserSessionStatus
    size_bucket: str


def _canonical_domain(domain: str) -> str:
    if not isinstance(domain, str):
        raise TypeError("domain must be a string")
    value = domain.rstrip(".")
    if not value or any(character in value for character in "/@?#:\\"):
        raise ValueError("domain is invalid")
    try:
        canonical = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("domain is invalid") from None
    if len(canonical) > 253:
        raise ValueError("domain is invalid")
    labels = canonical.split(".")
    if any(not label or len(label) > 63 or label[0] == "-" or label[-1] == "-" for label in labels):
        raise ValueError("domain is invalid")
    if any(not re.fullmatch(r"[a-z0-9-]+", label) for label in labels):
        raise ValueError("domain is invalid")
    return canonical


def _domain_key(domain: str) -> str:
    return hashlib.sha256(domain.encode("ascii")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _process_lock(key: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


class _DomainLock:
    def __init__(self, path: Path, timeout: float, io: RegistryIO) -> None:
        self._path = path
        self._timeout = timeout
        self._io = io
        self._stream: Any = None
        self._thread_lock = _process_lock(str(path.absolute()))
        self._thread_locked = False
        self._os_locked = False

    def acquire(self) -> None:
        deadline = time.monotonic() + self._timeout
        if not self._thread_lock.acquire(timeout=self._timeout):
            raise SessionLockTimeout("browser session lock acquisition timed out")
        self._thread_locked = True
        try:
            self._stream = self._io.open_lock(self._path)
            self._stream.seek(0, os.SEEK_END)
            if self._stream.tell() == 0:
                self._stream.write(b"\0")
                self._stream.flush()
                os.fsync(self._stream.fileno())
            while True:
                try:
                    self._try_lock()
                    self._os_locked = True
                    return
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise SessionLockTimeout("browser session lock acquisition timed out") from None
                    time.sleep(min(0.025, max(0.001, deadline - time.monotonic())))
        except Exception:
            self.release()
            raise

    def _try_lock(self) -> None:
        assert self._stream is not None
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl: Any = importlib.import_module("fcntl")
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self._stream is not None:
            try:
                if self._os_locked:
                    self._stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl: Any = importlib.import_module("fcntl")
                        fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            finally:
                self._os_locked = False
                self._stream.close()
                self._stream = None
        if self._thread_locked:
            self._thread_locked = False
            self._thread_lock.release()


class BrowserSessionLease:
    """An exclusive domain lease. Only ``profile_path`` reveals the private path."""

    __slots__ = ("_closed", "_info", "_lock", "_profile_path", "_store")

    def __init__(
        self,
        store: BrowserSessionStore,
        info: BrowserSessionInfo,
        profile_path: Path,
        lock: _DomainLock,
    ) -> None:
        self._store = store
        self._info = info
        self._profile_path = profile_path
        self._lock = lock
        self._closed = False

    @property
    def info(self) -> BrowserSessionInfo:
        return self._info

    @property
    def profile_path(self) -> Path:
        return self._profile_path

    def __enter__(self) -> BrowserSessionLease:
        if self._closed:
            raise BrowserSessionError("browser session lease is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._store._release(self._info)
        finally:
            self._lock.release()

    def __repr__(self) -> str:
        return f"BrowserSessionLease(info={self.info!r}, closed={self._closed!r})"


class BrowserSessionStore:
    """Persist and exclusively lease one browser profile per canonical domain."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        lock_timeout: float = 5.0,
        max_sessions: int = 256,
        max_profile_bytes: int = 1_073_741_824,
        max_scan_entries: int = 100_000,
        max_delete_entries: int = 100_000,
        _io: RegistryIO | None = None,
    ) -> None:
        if lock_timeout <= 0 or max_sessions <= 0 or max_profile_bytes <= 0:
            raise ValueError("browser session limits must be positive")
        if max_scan_entries <= 0 or max_delete_entries <= 0:
            raise ValueError("browser session scan limits must be positive")
        self.root = Path(root).absolute()
        self._io = _io or default_registry_io()
        self._lock_timeout = float(lock_timeout)
        self._max_sessions = max_sessions
        self._max_profile_bytes = max_profile_bytes
        self._max_scan_entries = max_scan_entries
        self._max_delete_entries = max_delete_entries
        self._profiles = self.root / "profiles"
        self._metadata = self.root / "metadata"
        self._locks = self.root / "locks"
        self._quarantine = self.root / "quarantine"
        for path in (self.root, self._profiles, self._metadata, self._locks, self._quarantine):
            self._io.ensure_directory(path)
            self._io.verify_private(path)

    def _paths(self, domain: str) -> tuple[str, Path, Path, Path]:
        key = _domain_key(domain)
        return key, self._profiles / key, self._metadata / f"{key}.json", self._locks / f"{key}.lock"

    def _lock(self, domain: str, timeout: float | None = None) -> _DomainLock:
        _, _, _, path = self._paths(domain)
        lock = _DomainLock(path, self._lock_timeout if timeout is None else timeout, self._io)
        lock.acquire()
        return lock

    def acquire(self, domain: str, timeout: float | None = None) -> BrowserSessionLease:
        canonical = _canonical_domain(domain)
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        lock = self._lock(canonical, timeout)
        allocation: _DomainLock | None = None
        try:
            _, profile, metadata, _ = self._paths(canonical)
            info = self._read_info(metadata, quarantine=True)
            if info is not None and info.domain != canonical:
                self._quarantine_metadata(metadata)
                info = None
            if info is not None and info.status is BrowserSessionStatus.REVOKED:
                raise SessionConflictError("browser session is revoked")
            if info is not None and info.status is BrowserSessionStatus.STALE:
                self._safe_delete_profile(profile)
                info = None
            if info is None:
                allocation = _DomainLock(self._locks / "allocation.lock", self._lock_timeout, self._io)
                allocation.acquire()
                self._enforce_session_limit(excluding=metadata)
                if profile.exists():
                    self._safe_delete_profile(profile)
                self._io.ensure_directory(profile)
                self._io.verify_private(profile)
                timestamp = _now()
                info = BrowserSessionInfo(
                    uuid.uuid4().hex,
                    canonical,
                    timestamp,
                    timestamp,
                    BrowserSessionStatus.AVAILABLE,
                    "empty",
                )
            try:
                self._verify_profile(profile)
                size = self._profile_size(profile)
                timestamp = _now()
                in_use = replace(
                    info,
                    last_used=timestamp,
                    status=BrowserSessionStatus.IN_USE,
                    size_bucket=self._size_bucket(size),
                )
                self._write_info(metadata, in_use)
                return BrowserSessionLease(self, in_use, profile, lock)
            finally:
                if allocation is not None:
                    allocation.release()
        except Exception:
            if allocation is not None:
                allocation.release()
            lock.release()
            raise

    def _release(self, leased: BrowserSessionInfo) -> None:
        _, profile, metadata, _ = self._paths(leased.domain)
        current = self._read_info(metadata, quarantine=False)
        if current is None or current.session_id != leased.session_id:
            return
        if current.status is BrowserSessionStatus.IN_USE:
            size = self._profile_size(profile)
            self._write_info(metadata, replace(current, status=BrowserSessionStatus.AVAILABLE, size_bucket=self._size_bucket(size)))

    def get(self, domain: str) -> BrowserSessionInfo | None:
        canonical = _canonical_domain(domain)
        lock = self._lock(canonical)
        try:
            _, _, metadata, _ = self._paths(canonical)
            return self._read_info(metadata, quarantine=True)
        finally:
            lock.release()

    def list_sessions(self) -> tuple[BrowserSessionInfo, ...]:
        results: list[BrowserSessionInfo] = []
        for count, path in enumerate(self._metadata.iterdir(), start=1):
            if count > self._max_scan_entries:
                raise SessionLimitError("browser session metadata scan limit exceeded")
            if not path.is_file() or not _HASH_NAME.fullmatch(path.stem) or path.suffix != ".json":
                continue
            info = self._read_info(path, quarantine=True)
            if info is not None:
                results.append(info)
        return tuple(sorted(results, key=lambda item: item.domain))

    def mark_stale(self, domain: str) -> BrowserSessionInfo:
        return self._set_status(domain, BrowserSessionStatus.STALE)

    def revoke(self, domain: str) -> BrowserSessionInfo:
        return self._set_status(domain, BrowserSessionStatus.REVOKED)

    def _set_status(self, domain: str, status: BrowserSessionStatus) -> BrowserSessionInfo:
        canonical = _canonical_domain(domain)
        lock = self._lock(canonical)
        try:
            _, _, metadata, _ = self._paths(canonical)
            info = self._read_info(metadata, quarantine=True)
            if info is None:
                raise SessionConflictError("browser session does not exist")
            updated = replace(info, status=status)
            self._write_info(metadata, updated)
            return updated
        finally:
            lock.release()

    def clear(self, domain: str, expected_session_id: str, confirmation: bool = True) -> bool:
        if confirmation is not True:
            raise SessionConfirmationError("browser session deletion requires confirmation")
        canonical = _canonical_domain(domain)
        lock = self._lock(canonical)
        try:
            _, profile, metadata, _ = self._paths(canonical)
            info = self._read_info(metadata, quarantine=True)
            if info is None:
                return False
            if info.session_id != expected_session_id:
                raise SessionConflictError("browser session identity does not match")
            self._safe_delete_profile(profile)
            self._delete_metadata(metadata)
            return True
        finally:
            lock.release()

    def _write_info(self, path: Path, info: BrowserSessionInfo) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": info.session_id,
            "domain": info.domain,
            "created": info.created,
            "last_used": info.last_used,
            "status": info.status.value,
            "size_bucket": info.size_bucket,
        }
        self._io.atomic_write(path, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii"))

    def _read_info(self, path: Path, *, quarantine: bool) -> BrowserSessionInfo | None:
        try:
            payload = self._io.read_bounded(path, 4096)
        except FileNotFoundError:
            return None
        except RegistryIOError as exc:
            if not path.exists():
                return None
            if quarantine:
                self._quarantine_metadata(path)
                return None
            raise BrowserSessionError("browser session metadata cannot be read safely") from exc
        try:
            value = json.loads(payload)
            if not isinstance(value, dict) or value.get("schema_version") != _SCHEMA_VERSION:
                raise ValueError
            info = BrowserSessionInfo(
                session_id=value["session_id"], domain=_canonical_domain(value["domain"]),
                created=value["created"], last_used=value["last_used"],
                status=BrowserSessionStatus(value["status"]), size_bucket=value["size_bucket"],
            )
            if not re.fullmatch(r"[0-9a-f]{32}", info.session_id):
                raise ValueError
            if info.size_bucket not in {"empty", "small", "medium", "large"}:
                raise ValueError
            datetime.fromisoformat(info.created)
            datetime.fromisoformat(info.last_used)
            return info
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            if quarantine:
                self._quarantine_metadata(path)
                return None
            raise BrowserSessionError("browser session metadata is corrupt") from None

    def _quarantine_metadata(self, path: Path) -> None:
        if not path.exists():
            return
        destination = self._quarantine / f"{path.stem}.{uuid.uuid4().hex}.bad"
        self._io.durable_move(path, destination)

    def _delete_metadata(self, path: Path) -> None:
        self._io.reject_link(path)
        if not path.exists():
            return
        tombstone = self._quarantine / f"{path.stem}.{uuid.uuid4().hex}.deleted"
        self._io.durable_move(path, tombstone)
        try:
            self._io.reject_link(tombstone)
            tombstone.unlink()
        except FileNotFoundError:
            return
        self._sync_directory(tombstone.parent)

    def _verify_profile(self, profile: Path) -> None:
        self._io.reject_link(profile)
        self._io.verify_private(profile)
        if not profile.is_dir():
            raise BrowserSessionError("browser profile is not a private directory")
        try:
            profile.relative_to(self._profiles)
        except ValueError:
            raise BrowserSessionError("browser profile escapes the session root") from None

    def _profile_size(self, profile: Path) -> int:
        total = 0
        count = 0
        stack = [profile]
        while stack:
            directory = stack.pop()
            self._io.reject_link(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > self._max_scan_entries:
                        raise SessionLimitError("browser profile scan limit exceeded")
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode) or self._is_reparse(metadata):
                        raise BrowserSessionError("browser profile contains a link or reparse point")
                    if stat.S_ISDIR(metadata.st_mode):
                        stack.append(Path(entry.path))
                    elif stat.S_ISREG(metadata.st_mode):
                        total += metadata.st_size
                        if total > self._max_profile_bytes:
                            raise SessionLimitError("browser profile exceeds maximum bytes")
                    else:
                        raise BrowserSessionError("browser profile contains an unsafe filesystem object")
        return total

    def _safe_delete_profile(self, profile: Path) -> None:
        if not profile.exists():
            return
        self._io.reject_link(profile)
        self._verify_profile(profile)
        tombstone = self._quarantine / f"profile.{uuid.uuid4().hex}.trash"
        self._io.durable_move(profile, tombstone)
        count = 0
        stack: list[tuple[Path, bool]] = [(tombstone, False)]
        while stack:
            directory, visited = stack.pop()
            self._io.reject_link(directory)
            if visited:
                directory.rmdir()
                continue
            stack.append((directory, True))
            with os.scandir(directory) as entries:
                children: list[Path] = []
                for entry in entries:
                    count += 1
                    if count > self._max_delete_entries:
                        raise SessionLimitError("browser profile deletion limit exceeded")
                    child = directory / entry.name
                    renamed = directory / f".delete.{uuid.uuid4().hex}"
                    os.replace(child, renamed)
                    children.append(renamed)
            for child in children:
                metadata = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or self._is_reparse(metadata):
                    raise BrowserSessionError("browser profile deletion rejected a link or reparse point")
                if stat.S_ISDIR(metadata.st_mode):
                    stack.append((child, False))
                elif stat.S_ISREG(metadata.st_mode):
                    child.unlink()
                else:
                    raise BrowserSessionError("browser profile deletion rejected an unsafe object")
        self._sync_directory(tombstone.parent)

    @staticmethod
    def _is_reparse(metadata: os.stat_result) -> bool:
        return bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _enforce_session_limit(self, *, excluding: Path) -> None:
        sessions = 0
        for count, path in enumerate(self._metadata.iterdir(), start=1):
            if count > self._max_scan_entries:
                raise SessionLimitError("browser session metadata scan limit exceeded")
            if path != excluding and path.suffix == ".json":
                sessions += 1
                if sessions >= self._max_sessions:
                    raise SessionLimitError("maximum browser session count reached")

    @staticmethod
    def _size_bucket(size: int) -> str:
        if size == 0:
            return "empty"
        if size < 10 * 1024 * 1024:
            return "small"
        if size < 100 * 1024 * 1024:
            return "medium"
        return "large"


__all__ = [
    "BrowserSessionError", "BrowserSessionInfo", "BrowserSessionLease", "BrowserSessionStatus",
    "BrowserSessionStore", "SessionConfirmationError", "SessionConflictError", "SessionLimitError",
    "SessionLockTimeout",
]

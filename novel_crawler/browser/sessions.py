"""Crash-safe, privacy-preserving storage for persistent browser profiles."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import sys
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from novel_crawler.adaptation.registry_io import RegistryIO, RegistryIOError, RegistryIOSizeError, default_registry_io
from novel_crawler.core.domains import canonical_domain

_SCHEMA_VERSION = 1
_HASH_NAME = re.compile(r"[0-9a-f]{64}")
_NORMAL_METADATA_FIELDS = frozenset({
    "binding", "created", "domain", "last_used", "profile_key", "schema_version",
    "session_id", "size_bucket", "status",
})
_DELETING_METADATA_FIELDS = _NORMAL_METADATA_FIELDS | {"tombstone_id"}
_PROCESS_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_PROCESS_LOCKS_GUARD = threading.Lock()


class BrowserSessionError(RuntimeError):
    """Base class with messages safe to expose to callers."""

    def __init__(self, code: str, domain: str | None = None) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("browser session error code is invalid")
        self.code = code
        self.domain = domain
        super().__init__(code if domain is None else f"{code} ({domain})")


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


def _domain_key(domain: str) -> str:
    return hashlib.sha256(domain.encode("ascii")).hexdigest()


def _binding(session_id: str, profile_key: str) -> str:
    return hashlib.sha256(f"{session_id}:{profile_key}".encode("ascii")).hexdigest()


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
        self._identity: tuple[int, int] | None = None

    def acquire(self) -> None:
        deadline = time.monotonic() + self._timeout
        if not self._thread_lock.acquire(timeout=self._timeout):
            raise SessionLockTimeout("lock_timeout")
        self._thread_locked = True
        try:
            self._open_stream()
            while True:
                try:
                    self._try_lock()
                    self._os_locked = True
                    if not self.identity_matches():  # pragma: no cover - POSIX ABA regression
                        self.release()  # pragma: no cover - POSIX ABA regression
                        if time.monotonic() >= deadline:  # pragma: no cover - POSIX ABA regression
                            raise SessionLockTimeout("lock_generation_changed") from None
                        if not self._thread_lock.acquire(  # pragma: no cover - POSIX ABA regression
                            timeout=max(0.001, deadline - time.monotonic())
                        ):
                            raise SessionLockTimeout("lock_timeout") from None
                        self._thread_locked = True  # pragma: no cover - POSIX ABA regression
                        self._open_stream()  # pragma: no cover - POSIX ABA regression
                        continue  # pragma: no cover - POSIX ABA regression
                    return
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise SessionLockTimeout("lock_timeout") from None
                    time.sleep(min(0.025, max(0.001, deadline - time.monotonic())))
        except Exception:
            self.release()
            raise

    def _open_stream(self) -> None:
        self._stream = self._io.open_lock(self._path)
        self._stream.seek(0, os.SEEK_END)
        if self._stream.tell() == 0:
            self._stream.write(b"\0")
            self._stream.flush()
            os.fsync(self._stream.fileno())

    def _try_lock(self) -> None:
        assert self._stream is not None
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - exercised by POSIX CI
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
                    else:  # pragma: no cover - exercised by POSIX CI
                        fcntl: Any = importlib.import_module("fcntl")
                        fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            finally:
                self._os_locked = False
                self._stream.close()
            self._stream = None
            self._identity = None
        if self._thread_locked:
            self._thread_locked = False
            self._thread_lock.release()

    def identity_matches(self) -> bool:
        if self._stream is None:  # pragma: no cover - defensive internal call
            return False  # pragma: no cover
        held = os.fstat(self._stream.fileno())
        self._identity = self._identity or (held.st_dev, held.st_ino)
        try:
            current = os.stat(self._path, follow_symlinks=False)
        except OSError:  # pragma: no cover - POSIX unlink fence
            return False  # pragma: no cover
        return self._identity == (current.st_dev, current.st_ino) and stat.S_ISREG(current.st_mode)

    @property
    def identity(self) -> tuple[int, int] | None:
        return self._identity


class BrowserSessionLease:
    """An exclusive domain lease.

    Context-manager exit persists release metadata. Any storage failure is
    reported as the privacy-safe ``release_failed`` error after unlocking.
    Only ``profile_path`` reveals the private path.
    """

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
            raise BrowserSessionError("lease_closed", self.info.domain)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failed = False
        try:
            self._store._release(self._info)
        except (BrowserSessionError, RegistryIOError, OSError):
            failed = True
        try:
            self._lock.release()
        except (RegistryIOError, OSError):
            failed = True
        if failed:
            raise BrowserSessionError("release_failed", self.info.domain) from None

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
        self._trash = self.root / "trash"
        self._tombstones = self.root / "tombstones"
        for path in (
            self.root, self._profiles, self._metadata, self._locks, self._quarantine,
            self._trash, self._tombstones,
        ):
            self._io.ensure_directory(path)
            self._io.verify_private(path)
        recovery = _DomainLock(self._locks / "allocation.lock", self._lock_timeout, self._io)
        try:
            recovery.acquire()
            self._recover_deletions()
        except (RegistryIOError, OSError):
            raise BrowserSessionError("recovery_io") from None
        finally:
            recovery.release()

    def _paths(self, domain: str) -> tuple[str, Path, Path, Path]:
        key = _domain_key(domain)
        return key, self._profiles / key, self._metadata / f"{key}.json", self._locks / f"{key}.lock"

    def _lock(self, domain: str, timeout: float | None = None) -> _DomainLock:
        _, _, _, path = self._paths(domain)
        lock = _DomainLock(path, self._lock_timeout if timeout is None else timeout, self._io)
        try:
            lock.acquire()
        except BrowserSessionError as exc:
            raise type(exc)(exc.code, domain) from None
        except (RegistryIOError, OSError):
            raise BrowserSessionError("lock_io", domain) from None
        return lock

    def acquire(self, domain: str, timeout: float | None = None) -> BrowserSessionLease:
        canonical = canonical_domain(domain)
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        _, _, metadata_path, _ = self._paths(canonical)
        preallocation = _DomainLock(self._locks / "allocation.lock", self._lock_timeout, self._io)
        allocation: _DomainLock | None = preallocation
        try:
            preallocation.acquire()
        except (RegistryIOError, OSError):
            raise BrowserSessionError("allocation_io", canonical) from None
        try:
            preview = self._read_info(metadata_path, quarantine=False)
        except BrowserSessionError as exc:
            if exc.code not in {"metadata_corrupt", "metadata_io"}:
                preallocation.release()
                raise
            preview = None
        if preview is not None and preview.status is not BrowserSessionStatus.STALE:
            preallocation.release()
            allocation = None
        lock = self._lock(canonical, timeout)
        try:
            _, profile, metadata, _ = self._paths(canonical)
            info = self._read_info(metadata, quarantine=allocation is not None)
            if info is not None and info.domain != canonical:
                self._quarantine_metadata(metadata)
                info = None
            if info is not None and info.status is BrowserSessionStatus.REVOKED:
                raise SessionConflictError("revoked", canonical)
            if info is not None and info.status is BrowserSessionStatus.STALE:
                tombstone_id = uuid.uuid4().hex
                self._write_deleting(metadata, info, tombstone_id)
                self._write_tombstone(info, tombstone_id)
                self._complete_deletion(metadata, info, tombstone_id)
                info = None
            if info is None:
                if allocation is None:
                    lock.release()
                    return self.acquire(canonical, timeout)
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
                committed = self._read_info(metadata, quarantine=False)
                if (
                    not lock.identity_matches()
                    or committed is None
                    or committed.session_id != in_use.session_id
                ):
                    lock.release()
                    return self.acquire(canonical, timeout)
                return BrowserSessionLease(self, in_use, profile, lock)
            finally:
                if allocation is not None:
                    allocation.release()
        except (RegistryIOError, OSError):  # pragma: no cover - injected cleanup failure
            identity = lock.identity
            lock.release()
            if allocation is not None:
                allocation.release()
            self._cleanup_failed_lock(canonical, identity)
            raise BrowserSessionError("storage_io", canonical) from None  # pragma: no cover
        except Exception:
            identity = lock.identity
            lock.release()
            if allocation is not None:
                allocation.release()
            self._cleanup_failed_lock(canonical, identity)
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
        canonical = canonical_domain(domain)
        _, _, metadata_path, _ = self._paths(canonical)
        allocation = _DomainLock(self._locks / "allocation.lock", self._lock_timeout, self._io)
        try:
            allocation.acquire()
            if not metadata_path.exists():
                return None
        except (BrowserSessionError, RegistryIOError, OSError):
            raise BrowserSessionError("storage_io", canonical) from None
        finally:
            try:
                allocation.release()
            except (RegistryIOError, OSError):
                raise BrowserSessionError("storage_io", canonical) from None
        lock = self._lock(canonical)
        try:
            _, _, metadata, _ = self._paths(canonical)
            return self._read_info(metadata, quarantine=False)
        except (RegistryIOError, OSError):
            raise BrowserSessionError("storage_io", canonical) from None
        finally:
            lock.release()

    def list_sessions(self) -> tuple[BrowserSessionInfo, ...]:
        results: list[BrowserSessionInfo] = []
        try:
            for count, path in enumerate(self._metadata.iterdir(), start=1):
                if count > self._max_scan_entries:
                    raise SessionLimitError("metadata_scan_limit")
                if not path.is_file() or not _HASH_NAME.fullmatch(path.stem) or path.suffix != ".json":
                    continue
                info = self._read_info(path, quarantine=False)
                if info is not None:
                    results.append(info)
        except (RegistryIOError, OSError):
            raise BrowserSessionError("storage_io") from None
        return tuple(sorted(results, key=lambda item: item.domain))

    def mark_stale(self, domain: str) -> BrowserSessionInfo:
        return self._set_status(domain, BrowserSessionStatus.STALE)

    def revoke(self, domain: str) -> BrowserSessionInfo:
        return self._set_status(domain, BrowserSessionStatus.REVOKED)

    def _set_status(self, domain: str, status: BrowserSessionStatus) -> BrowserSessionInfo:
        canonical = canonical_domain(domain)
        lock = self._lock(canonical)
        try:
            _, _, metadata, _ = self._paths(canonical)
            info = self._read_info(metadata, quarantine=True)
            if info is None:
                raise SessionConflictError("not_found", canonical)
            updated = replace(info, status=status)
            self._write_info(metadata, updated)
            return updated
        except (RegistryIOError, OSError):
            raise BrowserSessionError("storage_io", canonical) from None
        finally:
            lock.release()

    def clear(self, domain: str, expected_session_id: str, *, confirmation: bool = False) -> bool:
        canonical = canonical_domain(domain)
        if confirmation is not True:
            raise SessionConfirmationError("confirmation_required", canonical)
        allocation = _DomainLock(self._locks / "allocation.lock", self._lock_timeout, self._io)
        try:
            allocation.acquire()
            allocation.release()
        except (BrowserSessionError, RegistryIOError, OSError):
            raise BrowserSessionError("storage_io", canonical) from None
        lock = self._lock(canonical)
        try:
            _, profile, metadata, _ = self._paths(canonical)
            info = self._read_info(metadata, quarantine=True)
            if info is None:
                return False
            if info.session_id != expected_session_id:
                raise SessionConflictError("identity_mismatch", canonical)
            tombstone_id = uuid.uuid4().hex
            self._write_deleting(metadata, info, tombstone_id)
            self._write_tombstone(info, tombstone_id)
            self._complete_deletion(metadata, info, tombstone_id)
            return True
        except RegistryIOSizeError:
            raise SessionLimitError("profile_delete_limit", canonical) from None
        except (RegistryIOError, OSError):
            raise BrowserSessionError("deletion_io", canonical) from None
        finally:
            primary_active = sys.exc_info()[0] is not None
            try:
                lock.release()
                cleanup = _DomainLock(self._locks / "allocation.lock", self._lock_timeout, self._io)
                cleanup.acquire()
                try:
                    _, profile, metadata, lock_path = self._paths(canonical)
                    if not profile.exists() and not metadata.exists():
                        self._delete_lock_file(lock_path)
                finally:
                    cleanup.release()
            except (BrowserSessionError, RegistryIOError, OSError):
                if not primary_active:
                    raise BrowserSessionError("clear_cleanup_failed", canonical) from None

    def _write_info(self, path: Path, info: BrowserSessionInfo) -> None:
        profile_key = _domain_key(info.domain)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": info.session_id,
            "domain": info.domain,
            "created": info.created,
            "last_used": info.last_used,
            "status": info.status.value,
            "size_bucket": info.size_bucket,
            "profile_key": profile_key,
            "binding": _binding(info.session_id, profile_key),
        }
        self._io.atomic_write(path, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii"))

    def _deleting_payload(self, info: BrowserSessionInfo, tombstone_id: str) -> dict[str, object]:
        profile_key = _domain_key(info.domain)
        return {
            "schema_version": _SCHEMA_VERSION,
            "session_id": info.session_id,
            "domain": info.domain,
            "created": info.created,
            "last_used": info.last_used,
            "status": "deleting",
            "size_bucket": info.size_bucket,
            "profile_key": profile_key,
            "binding": _binding(info.session_id, profile_key),
            "tombstone_id": tombstone_id,
        }

    def _write_deleting(self, path: Path, info: BrowserSessionInfo, tombstone_id: str) -> None:
        payload = self._deleting_payload(info, tombstone_id)
        self._io.atomic_write(path, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii"))

    def _write_tombstone(self, info: BrowserSessionInfo, tombstone_id: str) -> None:
        payload = self._deleting_payload(info, tombstone_id)
        self._io.atomic_write(
            self._tombstones / f"{tombstone_id}.json",
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii"),
        )

    def _read_info(self, path: Path, *, quarantine: bool) -> BrowserSessionInfo | None:
        try:
            payload = self._io.read_bounded(path, 4096)
        except FileNotFoundError:
            return None
        except RegistryIOError:
            if not path.exists():
                return None
            if quarantine:
                self._quarantine_metadata(path)
                return None
            raise BrowserSessionError("metadata_io") from None
        try:
            value = json.loads(payload)
            if not isinstance(value, dict) or value.get("schema_version") != _SCHEMA_VERSION:
                raise ValueError
            expected_fields = _DELETING_METADATA_FIELDS if value.get("status") == "deleting" else _NORMAL_METADATA_FIELDS
            if set(value) != expected_fields:
                raise ValueError
            if value.get("status") == "deleting":
                info, tombstone_id = self._validate_deleting(path, value)
                if quarantine:
                    self._complete_deletion(path, info, tombstone_id)
                return None
            info = BrowserSessionInfo(
                session_id=value["session_id"], domain=canonical_domain(value["domain"]),
                created=value["created"], last_used=value["last_used"],
                status=BrowserSessionStatus(value["status"]), size_bucket=value["size_bucket"],
            )
            if not re.fullmatch(r"[0-9a-f]{32}", info.session_id):
                raise ValueError
            profile_key = _domain_key(info.domain)
            if path.stem != profile_key or value.get("profile_key") != profile_key:
                raise ValueError
            if value.get("binding") != _binding(info.session_id, profile_key):
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
            raise BrowserSessionError("metadata_corrupt") from None

    def _validate_deleting(self, path: Path, value: dict[str, object]) -> tuple[BrowserSessionInfo, str]:
        info = BrowserSessionInfo(
            session_id=str(value["session_id"]),
            domain=canonical_domain(str(value["domain"])),
            created=str(value["created"]),
            last_used=str(value["last_used"]),
            status=BrowserSessionStatus.REVOKED,
            size_bucket=str(value["size_bucket"]),
        )
        tombstone_id = str(value["tombstone_id"])
        profile_key = _domain_key(info.domain)
        if (
            path.stem != profile_key
            or value.get("profile_key") != profile_key
            or value.get("binding") != _binding(info.session_id, profile_key)
            or not re.fullmatch(r"[0-9a-f]{32}", info.session_id)
            or not re.fullmatch(r"[0-9a-f]{32}", tombstone_id)
        ):
            raise ValueError("invalid deletion metadata")
        return info, tombstone_id

    def _complete_deletion(self, metadata: Path, info: BrowserSessionInfo, tombstone_id: str) -> None:
        _, profile, _, _ = self._paths(info.domain)
        trash = self._trash / tombstone_id
        if profile.exists() and not trash.exists():
            self._io.reject_link(profile)
            self._verify_profile(profile)
            self._io.durable_move(profile, trash)
        if trash.exists():
            self._io.secure_remove_tree(trash, self._max_delete_entries)
        self._delete_metadata(metadata)
        self._delete_metadata(self._tombstones / f"{tombstone_id}.json")

    def _recover_deletions(self) -> None:
        seen = 0
        for path in self._metadata.iterdir():
            seen += 1
            if seen > self._max_scan_entries:
                raise SessionLimitError("metadata_scan_limit")
            try:
                value = json.loads(self._io.read_bounded(path, 4096))
                if isinstance(value, dict) and value.get("status") == "deleting":
                    info, tombstone_id = self._validate_deleting(path, value)
                    domain_lock = self._lock(info.domain)
                    try:
                        self._complete_deletion(path, info, tombstone_id)
                    finally:
                        domain_lock.release()
            except (RegistryIOError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
        for tombstone in self._tombstones.glob("*.json"):
            seen += 1
            if seen > self._max_scan_entries:
                raise SessionLimitError("recovery_scan_limit")
            try:
                value = json.loads(self._io.read_bounded(tombstone, 4096))
                if not isinstance(value, dict) or value.get("status") != "deleting":
                    raise ValueError
                domain = canonical_domain(str(value["domain"]))
                _, _, metadata, _ = self._paths(domain)
                info, tombstone_id = self._validate_deleting(metadata, value)
                if tombstone.stem != tombstone_id:
                    raise ValueError
                domain_lock = self._lock(info.domain)
                try:
                    self._complete_deletion(metadata, info, tombstone_id)
                finally:
                    domain_lock.release()
            except (RegistryIOError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                self._quarantine_metadata(tombstone)
        metadata_keys = {path.stem for path in self._metadata.glob("*.json")}
        for profile in self._profiles.iterdir():
            seen += 1
            if seen > self._max_scan_entries:
                raise SessionLimitError("recovery_scan_limit")
            if profile.name not in metadata_keys:
                tombstone_id = uuid.uuid4().hex
                trash = self._trash / tombstone_id
                domain_lock = _DomainLock(self._locks / f"{profile.name}.lock", self._lock_timeout, self._io)
                domain_lock.acquire()
                try:
                    self._io.durable_move(profile, trash)
                    self._io.secure_remove_tree(trash, self._max_delete_entries)
                finally:
                    domain_lock.release()
        for trash in self._trash.iterdir():
            seen += 1
            if seen > self._max_scan_entries:
                raise SessionLimitError("recovery_scan_limit")
            self._io.secure_remove_tree(trash, self._max_delete_entries)
        tracked = {path.stem for path in self._metadata.glob("*.json")} | {
            path.name for path in self._profiles.iterdir()
        }
        for lock_path in self._locks.glob("*.lock"):
            seen += 1
            if seen > self._max_scan_entries:
                raise SessionLimitError("recovery_scan_limit")
            if lock_path.name != "allocation.lock" and lock_path.stem not in tracked:
                self._delete_lock_file(lock_path)

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

    def _delete_lock_file(self, path: Path) -> None:
        self._io.reject_link(path)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except PermissionError:
            # An already-open waiter owns this generation. Its identity fence
            # prevents it from crossing into a later recreated lock.
            return
        self._sync_directory(path.parent)

    def _cleanup_failed_lock(self, domain: str, expected: tuple[int, int] | None) -> None:
        cleanup = _DomainLock(self._locks / "allocation.lock", self._lock_timeout, self._io)
        try:
            cleanup.acquire()
            _, profile, metadata, lock_path = self._paths(domain)
            if not profile.exists() and not metadata.exists() and expected is not None:
                current = os.stat(lock_path, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == expected:
                    self._delete_lock_file(lock_path)
        except (BrowserSessionError, RegistryIOError, OSError):  # pragma: no cover - best-effort cleanup
            pass  # pragma: no cover
        finally:
            cleanup.release()

    def _verify_profile(self, profile: Path) -> None:
        try:
            profile.relative_to(self._profiles)
        except ValueError:
            raise BrowserSessionError("profile_escape") from None
        self._io.reject_link(profile)
        self._io.verify_private(profile)
        if not profile.is_dir():
            raise BrowserSessionError("profile_not_private")

    def _profile_size(self, profile: Path) -> int:
        try:
            return self._io.secure_tree_size(profile, self._max_scan_entries, self._max_profile_bytes)
        except RegistryIOError as exc:
            if "limit" in str(exc):
                raise SessionLimitError("profile_size_limit") from None
            raise BrowserSessionError("profile_measure_unsafe") from None

    def _safe_delete_profile(self, profile: Path) -> None:
        if not profile.exists():
            return
        self._io.reject_link(profile)
        self._verify_profile(profile)
        tombstone = self._trash / uuid.uuid4().hex
        self._io.durable_move(profile, tombstone)
        try:
            self._io.secure_remove_tree(tombstone, self._max_delete_entries)
        except RegistryIOError as exc:
            if "limit" in str(exc):
                raise SessionLimitError("profile_delete_limit") from None
            raise BrowserSessionError("profile_remove_unsafe") from None

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))  # pragma: no cover - POSIX
        try:  # pragma: no cover - POSIX
            os.fsync(descriptor)
        finally:  # pragma: no cover - POSIX
            os.close(descriptor)

    def _enforce_session_limit(self, *, excluding: Path) -> None:
        reservations: set[str] = set()
        count = 0
        for directory, prefix in (
            (self._metadata, "domain:"),
            (self._profiles, "domain:"),
            (self._tombstones, "tombstone:"),
            (self._trash, "trash:"),
            (self._quarantine, "quarantine:"),
        ):
            for path in directory.iterdir():
                count += 1
                if count > self._max_scan_entries:
                    raise SessionLimitError("metadata_scan_limit")
                key = path.stem if directory in {self._metadata, self._tombstones} else path.name
                reservations.add(prefix + key)
        reservations.discard("domain:" + excluding.stem)
        if len(reservations) >= self._max_sessions:
            raise SessionLimitError("session_count_limit")

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

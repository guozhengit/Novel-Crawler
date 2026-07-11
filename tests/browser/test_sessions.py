from __future__ import annotations

import json
import multiprocessing
import os
import stat
import sys
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from novel_crawler.browser.sessions import (
    BrowserSessionStatus,
    BrowserSessionStore,
    SessionConfirmationError,
    SessionConflictError,
    SessionLimitError,
    SessionLockTimeout,
)


def _crash_with_lease(root: str, ready: multiprocessing.synchronize.Event) -> None:
    store = BrowserSessionStore(root)
    store.acquire("crash.example")
    ready.set()
    os._exit(23)


def _hold_process_lease(
    root: str, ready: multiprocessing.synchronize.Event, release: multiprocessing.synchronize.Event
) -> None:
    store = BrowserSessionStore(root)
    with store.acquire("held.example"):
        ready.set()
        release.wait(10)


def test_session_lifecycle_is_private_and_idna_canonical(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")

    with store.acquire("B\u00dcCHER.Example.") as lease:
        info = lease.info
        assert info.domain == "xn--bcher-kva.example"
        assert info.status is BrowserSessionStatus.IN_USE
        assert lease.profile_path.is_dir()
        assert str(lease.profile_path) not in repr(lease)
        assert str(lease.profile_path) not in repr(info)

    available = store.get("b\u00fccher.example")
    assert available is not None
    assert available.status is BrowserSessionStatus.AVAILABLE
    assert available.session_id == info.session_id
    with pytest.raises(FrozenInstanceError):
        available.domain = "other.example"  # type: ignore[misc]


def test_clear_requires_confirmation_and_matching_identity(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("example.com") as lease:
        session_id = lease.info.session_id

    with pytest.raises(SessionConfirmationError):
        store.clear("example.com", session_id, confirmation=False)
    with pytest.raises(SessionConflictError):
        store.clear("example.com", "wrong-id", confirmation=True)
    assert store.clear("example.com", session_id, confirmation=True)
    assert store.get("example.com") is None

    with store.acquire("example.com") as replacement:
        assert replacement.info.session_id != session_id


def test_stale_and_revoked_sessions_are_not_reused(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("one.example") as lease:
        first = lease.info.session_id
    stale = store.mark_stale("one.example")
    assert stale.status is BrowserSessionStatus.STALE
    with store.acquire("one.example") as replacement:
        assert replacement.info.session_id != first

    store.revoke("one.example")
    with pytest.raises(SessionConflictError, match="revoked"):
        store.acquire("one.example")


@pytest.mark.parametrize("domain", ["", ".", "a/b.example", "user@example.com", "example.com:443"])
def test_invalid_domains_are_rejected(tmp_path: Path, domain: str) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with pytest.raises(ValueError, match="domain"):
        store.acquire(domain)


def test_same_domain_is_exclusive_but_different_domains_are_concurrent(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions", lock_timeout=0.1)
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with store.acquire("one.example"):
            entered.set()
            release.wait(2)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(2)
    with pytest.raises(SessionLockTimeout):
        store.acquire("one.example")
    with store.acquire("two.example", timeout=0.5):
        pass
    release.set()
    worker.join(2)
    assert not worker.is_alive()


def test_process_crash_releases_os_lock_and_recovers_in_use_metadata(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_crash_with_lease, args=(str(root), ready))
    process.start()
    assert ready.wait(10)
    process.join(10)
    assert process.exitcode == 23

    store = BrowserSessionStore(root)
    crashed = store.get("crash.example")
    assert crashed is not None and crashed.status is BrowserSessionStatus.IN_USE
    with store.acquire("crash.example", timeout=2) as recovered:
        assert recovered.info.session_id == crashed.session_id


def test_cross_process_contention_times_out(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_hold_process_lease, args=(str(root), ready, release))
    process.start()
    assert ready.wait(10)
    try:
        with pytest.raises(SessionLockTimeout):
            BrowserSessionStore(root).acquire("held.example", timeout=0.1)
    finally:
        release.set()
        process.join(10)
    assert process.exitcode == 0


def test_corrupt_metadata_is_quarantined_and_recreated(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("broken.example") as lease:
        old_id = lease.info.session_id
    metadata = next((store.root / "metadata").glob("*.json"))
    metadata.write_bytes(b'{"cookie":"secret","profile_path":"C:/secret"}')

    assert store.get("broken.example") is None
    quarantined = list((store.root / "quarantine").glob("*.bad"))
    assert len(quarantined) == 1
    with store.acquire("broken.example") as replacement:
        assert replacement.info.session_id != old_id


def test_metadata_serializer_contains_only_safe_fields(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("safe.example"):
        pass
    payload = json.loads(next((store.root / "metadata").glob("*.json")).read_bytes())
    assert set(payload) == {
        "schema_version", "session_id", "domain", "created", "last_used", "status", "size_bucket"
    }
    assert "cookie" not in repr(store.list_sessions()).lower()
    assert str(store.root) not in repr(store.list_sessions())


def test_session_count_and_profile_size_are_bounded(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions", max_sessions=1, max_profile_bytes=3)
    with store.acquire("one.example") as lease:
        (lease.profile_path / "data").write_bytes(b"1234")
        with pytest.raises(SessionLimitError, match="bytes"):
            lease.close()
    with pytest.raises(SessionLimitError, match="count"):
        store.acquire("two.example")


def test_symlink_in_profile_is_never_followed_or_deleted(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    lease = store.acquire("linked.example")
    link = lease.profile_path / "link"
    try:
        link.symlink_to(outside)
    except OSError:
        lease.close()
        pytest.skip("symlink creation unavailable")
    with pytest.raises(Exception, match="link|reparse"):
        lease.close()
    assert outside.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_posix_profile_and_metadata_permissions_are_private(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("private.example") as lease:
        assert stat.S_IMODE(lease.profile_path.stat().st_mode) == 0o700
    metadata = next((store.root / "metadata").glob("*.json"))
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o600


def test_validation_missing_states_and_closed_lease(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BrowserSessionStore(tmp_path / "bad", lock_timeout=0)
    with pytest.raises(ValueError):
        BrowserSessionStore(tmp_path / "bad", max_scan_entries=0)
    store = BrowserSessionStore(tmp_path / "sessions")
    with pytest.raises(TypeError):
        store.acquire(1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.acquire("a" * 64 + ".example")
    with pytest.raises(ValueError):
        store.acquire("example.com", timeout=0)
    assert store.clear("absent.example", "id") is False
    with pytest.raises(SessionConflictError, match="does not exist"):
        store.mark_stale("absent.example")
    lease = store.acquire("closed.example")
    lease.close()
    lease.close()
    with pytest.raises(Exception, match="closed"):
        lease.__enter__()


def test_nested_profile_is_deleted_and_size_buckets_are_coarse(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("nested.example") as lease:
        nested = lease.profile_path / "a" / "b"
        nested.mkdir(parents=True)
        (nested / "state.bin").write_bytes(b"state")
        session_id = lease.info.session_id
    info = store.get("nested.example")
    assert info is not None and info.size_bucket == "small"
    assert store.clear("nested.example", session_id)
    assert store._size_bucket(10 * 1024 * 1024) == "medium"
    assert store._size_bucket(100 * 1024 * 1024) == "large"


def test_scan_and_delete_entry_limits_fail_closed(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions", max_scan_entries=1)
    (store.root / "metadata" / "ignored").write_text("x", encoding="utf-8")
    (store.root / "metadata" / "also-ignored").write_text("x", encoding="utf-8")
    with pytest.raises(SessionLimitError, match="scan"):
        store.list_sessions()

    deleting = BrowserSessionStore(tmp_path / "deleting", max_delete_entries=1)
    with deleting.acquire("delete.example") as lease:
        (lease.profile_path / "one").write_text("1", encoding="utf-8")
        (lease.profile_path / "two").write_text("2", encoding="utf-8")
        session_id = lease.info.session_id
    with pytest.raises(SessionLimitError, match="deletion"):
        deleting.clear("delete.example", session_id)


def test_unknown_metadata_files_are_not_deserialized(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    (store.root / "metadata" / "notes.txt").write_text("cookie=secret", encoding="utf-8")
    (store.root / "metadata" / "not-a-hash.json").write_text("{}", encoding="utf-8")
    assert store.list_sessions() == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [("session_id", "predictable"), ("size_bucket", "exactly-123-bytes")],
)
def test_invalid_safe_metadata_fields_are_quarantined(tmp_path: Path, field: str, value: str) -> None:
    store = BrowserSessionStore(tmp_path / field)
    with store.acquire("invalid.example"):
        pass
    metadata = next((store.root / "metadata").glob("*.json"))
    payload = json.loads(metadata.read_bytes())
    payload[field] = value
    metadata.write_text(json.dumps(payload), encoding="ascii")
    assert store.get("invalid.example") is None
    assert list((store.root / "quarantine").glob("*.bad"))


def test_domain_mismatch_is_quarantined_before_profile_reuse(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("one.example"):
        pass
    metadata = next((store.root / "metadata").glob("*.json"))
    payload = json.loads(metadata.read_bytes())
    payload["domain"] = "other.example"
    metadata.write_text(json.dumps(payload), encoding="ascii")
    with store.acquire("one.example") as replacement:
        assert replacement.info.domain == "one.example"
    assert list((store.root / "quarantine").glob("*.bad"))


def test_profile_and_metadata_scan_limits_are_independent(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "profiles", max_scan_entries=1)
    lease = store.acquire("scan.example")
    (lease.profile_path / "one").write_text("1", encoding="utf-8")
    (lease.profile_path / "two").write_text("2", encoding="utf-8")
    with pytest.raises(SessionLimitError, match="profile scan"):
        lease.close()

    metadata_store = BrowserSessionStore(tmp_path / "metadata", max_sessions=100, max_scan_entries=1)
    for name in ("a.json", "b.json"):
        (metadata_store.root / "metadata" / name).write_text("{}", encoding="ascii")
    with pytest.raises(SessionLimitError, match="metadata scan"):
        metadata_store.acquire("new.example")


def test_deep_profile_deletion_is_iterative(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions", max_delete_entries=200)
    with store.acquire("deep.example") as lease:
        cursor = lease.profile_path
        for _ in range(60):
            cursor = cursor / "d"
            cursor.mkdir()
        session_id = lease.info.session_id
    previous = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(50)
        assert store.clear("deep.example", session_id)
    finally:
        sys.setrecursionlimit(previous)


def test_directory_swap_during_deletion_cannot_escape_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with store.acquire("swap.example") as lease:
        child = lease.profile_path / "child"
        child.mkdir()
        session_id = lease.info.session_id
    original_scandir = os.scandir
    swapped = False

    def scandir(path: str | os.PathLike[str]) -> os.ScandirIterator[str]:
        nonlocal swapped
        candidate = Path(path)
        if candidate == child and not swapped:
            candidate.rmdir()
            try:
                candidate.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlink creation unavailable")
            swapped = True
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert store.clear("swap.example", session_id)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_global_session_limit_includes_unpublished_concurrent_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sessions"
    stores = [BrowserSessionStore(root, max_sessions=1) for _ in range(2)]
    first_writing = threading.Event()
    allow_first_write = threading.Event()
    original_write = BrowserSessionStore._write_info
    calls = 0
    calls_guard = threading.Lock()

    def delayed_write(self: BrowserSessionStore, path: Path, info: object) -> None:
        nonlocal calls
        with calls_guard:
            calls += 1
            call = calls
        if call == 1:
            first_writing.set()
            assert allow_first_write.wait(5)
        original_write(self, path, info)  # type: ignore[arg-type]

    monkeypatch.setattr(BrowserSessionStore, "_write_info", delayed_write)
    outcomes: list[object] = []

    def create(index: int) -> None:
        try:
            with stores[index].acquire(f"{index}.example") as lease:
                outcomes.append(lease.info.session_id)
        except Exception as exc:
            outcomes.append(exc)

    first = threading.Thread(target=create, args=(0,))
    second = threading.Thread(target=create, args=(1,))
    first.start()
    assert first_writing.wait(5)
    second.start()
    time.sleep(0.1)
    allow_first_write.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert sum(isinstance(item, str) for item in outcomes) == 1
    assert sum(isinstance(item, SessionLimitError) for item in outcomes) == 1

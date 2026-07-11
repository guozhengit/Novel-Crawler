from __future__ import annotations

import json
import multiprocessing
import os
import re
import stat
import sys
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from novel_crawler.adaptation.registry_io import RegistryIOError
from novel_crawler.browser.sessions import (
    BrowserSessionError,
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


def _allocate_limited_session(root: str, domain: str, start: object, results: object) -> None:
    start.wait(10)  # type: ignore[attr-defined]
    try:
        store = BrowserSessionStore(root, max_sessions=1)
        with store.acquire(domain):
            pass
        results.put("ok")  # type: ignore[attr-defined]
    except SessionLimitError:
        results.put("limit")  # type: ignore[attr-defined]


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
        store.clear("example.com", session_id)
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


def test_uts46_nontransitional_domains_do_not_alias_and_invalid_labels_fail(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("faß.de") as sharp_s:
        sharp_id = sharp_s.info.session_id
        assert sharp_s.info.domain == "xn--fa-hia.de"
    with store.acquire("fass.de") as ascii_s:
        assert ascii_s.info.session_id != sharp_id
    for invalid in ("a\u200db.example", "xn--invalid-.example", "xn--a.example"):
        with pytest.raises(ValueError, match="domain"):
            store.acquire(invalid)


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


def test_multiprocess_global_allocation_limit_is_strict(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    root = str(tmp_path / "sessions")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_allocate_limited_session, args=(root, f"{index}.example", start, results))
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    assert sorted(results.get(timeout=2) for _ in range(2)) == ["limit", "ok"]


def test_corrupt_metadata_is_quarantined_and_recreated(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("broken.example") as lease:
        old_id = lease.info.session_id
    metadata = next((store.root / "metadata").glob("*.json"))
    metadata.write_bytes(b'{"cookie":"secret","profile_path":"C:/secret"}')

    with pytest.raises(BrowserSessionError, match="metadata_corrupt"):
        store.get("broken.example")
    assert not list((store.root / "quarantine").glob("*.bad"))
    with store.acquire("broken.example") as replacement:
        assert replacement.info.session_id != old_id


def test_metadata_serializer_contains_only_safe_fields(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("safe.example"):
        pass
    payload = json.loads(next((store.root / "metadata").glob("*.json")).read_bytes())
    assert set(payload) == {
        "binding", "created", "domain", "last_used", "profile_key", "schema_version", "session_id", "size_bucket", "status"
    }
    assert "cookie" not in repr(store.list_sessions()).lower()
    assert str(store.root) not in repr(store.list_sessions())


def test_session_count_and_profile_size_are_bounded(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions", max_sessions=1, max_profile_bytes=3)
    with store.acquire("one.example") as lease:
        (lease.profile_path / "data").write_bytes(b"1234")
        with pytest.raises(BrowserSessionError, match="release_failed"):
            lease.close()
    with pytest.raises(SessionLimitError, match="session_count_limit"):
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
    with pytest.raises(Exception, match="release_failed"):
        lease.close()
    assert outside.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_posix_profile_and_metadata_permissions_are_private(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("private.example") as lease:
        assert stat.S_IMODE(lease.profile_path.stat().st_mode) == 0o700
    metadata = next((store.root / "metadata").glob("*.json"))
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o600
    for name in ("profiles", "metadata", "locks", "trash", "tombstones", "quarantine"):
        assert stat.S_IMODE((store.root / name).stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (store.root / "locks").iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL verification")
def test_windows_all_session_directories_and_files_have_private_acls(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("private.example"):
        pass
    for name in ("profiles", "metadata", "locks", "trash", "tombstones", "quarantine"):
        store._io.verify_private(store.root / name)
    metadata = next((store.root / "metadata").glob("*.json"))
    assert store._io.read_bounded(metadata, 4096)
    for lock in (store.root / "locks").iterdir():
        stream = store._io.open_lock(lock)
        stream.close()


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
    assert store.clear("absent.example", "id", confirmation=True) is False
    with pytest.raises(SessionConflictError, match="not_found"):
        store.mark_stale("absent.example")
    lease = store.acquire("closed.example")
    lease.close()
    lease.close()
    with pytest.raises(Exception, match="lease_closed"):
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
    assert store.clear("nested.example", session_id, confirmation=True)
    assert store._size_bucket(10 * 1024 * 1024) == "medium"
    assert store._size_bucket(100 * 1024 * 1024) == "large"


def test_scan_and_delete_entry_limits_fail_closed(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions", max_scan_entries=1)
    (store.root / "metadata" / "ignored").write_text("x", encoding="utf-8")
    (store.root / "metadata" / "also-ignored").write_text("x", encoding="utf-8")
    with pytest.raises(SessionLimitError, match="metadata_scan_limit"):
        store.list_sessions()

    deleting = BrowserSessionStore(tmp_path / "deleting", max_delete_entries=1)
    with deleting.acquire("delete.example") as lease:
        (lease.profile_path / "one").write_text("1", encoding="utf-8")
        (lease.profile_path / "two").write_text("2", encoding="utf-8")
        session_id = lease.info.session_id
    with pytest.raises(SessionLimitError, match="profile_delete_limit"):
        deleting.clear("delete.example", session_id, confirmation=True)


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
    with pytest.raises(BrowserSessionError, match="metadata_corrupt"):
        store.get("invalid.example")
    assert not list((store.root / "quarantine").glob("*.bad"))


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


def test_complete_metadata_swap_is_quarantined_by_filename_binding(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    for domain in ("one.example", "two.example"):
        with store.acquire(domain):
            pass
    metadata = sorted((store.root / "metadata").glob("*.json"))
    first = metadata[0].read_bytes()
    second = metadata[1].read_bytes()
    metadata[0].write_bytes(second)
    metadata[1].write_bytes(first)
    with pytest.raises(BrowserSessionError, match="metadata_corrupt"):
        store.list_sessions()
    assert not list((store.root / "quarantine").glob("*.bad"))


def test_profile_and_metadata_scan_limits_are_independent(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "profiles", max_scan_entries=1)
    lease = store.acquire("scan.example")
    (lease.profile_path / "one").write_text("1", encoding="utf-8")
    (lease.profile_path / "two").write_text("2", encoding="utf-8")
    with pytest.raises(BrowserSessionError, match="release_failed"):
        lease.close()

    metadata_store = BrowserSessionStore(tmp_path / "metadata", max_sessions=100, max_scan_entries=1)
    for name in ("a.json", "b.json"):
        (metadata_store.root / "metadata" / name).write_text("{}", encoding="ascii")
    with pytest.raises(SessionLimitError, match="metadata_scan_limit"):
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
        assert store.clear("deep.example", session_id, confirmation=True)
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

    def scandir(path: str | os.PathLike[str]) -> Any:
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
    assert store.clear("swap.example", session_id, confirmation=True)
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


def test_exact_domain_locks_are_bounded_by_tracked_sessions(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions", max_sessions=100)
    for index in range(70):
        with store.acquire(f"domain-{index}.example"):
            pass
    lock_names = {path.name for path in (store.root / "locks").iterdir()}
    assert len(lock_names - {"allocation.lock"}) == 70
    assert all(name == "allocation.lock" or re.fullmatch(r"[0-9a-f]{64}\.lock", name) for name in lock_names)
    for info in store.list_sessions():
        store.clear(info.domain, info.session_id, confirmation=True)
    assert {path.name for path in (store.root / "locks").iterdir()} <= {"allocation.lock"}


def test_interrupted_transactional_clear_is_completed_on_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sessions"
    store = BrowserSessionStore(root)
    with store.acquire("recover.example") as lease:
        session_id = lease.info.session_id
    original_remove = store._io.secure_remove_tree
    failed = False

    def fail_once(path: Path, max_entries: int) -> None:
        nonlocal failed
        if not failed and path.parent.name == "trash":
            failed = True
            raise RegistryIOError("injected deletion crash")
        original_remove(path, max_entries)

    monkeypatch.setattr(store._io, "secure_remove_tree", fail_once)
    with pytest.raises(BrowserSessionError) as caught:
        store.clear("recover.example", session_id, confirmation=True)
    assert caught.value.code == "deletion_io"
    assert str(root) not in str(caught.value)
    assert caught.value.__suppress_context__
    assert list((root / "trash").iterdir())
    assert list((root / "tombstones").iterdir())

    recovered = BrowserSessionStore(root)
    assert recovered.get("recover.example") is None
    assert not list((root / "trash").iterdir())
    assert not list((root / "tombstones").iterdir())


def test_orphan_profile_from_crash_before_metadata_is_recovered(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    store = BrowserSessionStore(root)
    orphan = store.root / "profiles" / ("a" * 64)
    store._io.ensure_directory(orphan)
    (orphan / "state").write_bytes(b"private")
    BrowserSessionStore(root)
    assert not orphan.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle swap defense")
def test_windows_child_swap_attack_executes_but_cannot_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("keep", encoding="utf-8")
    lease = store.acquire("attack.example")
    child = lease.profile_path / "child"
    child.mkdir()
    lease.close()
    api = store._io._api
    original_open = api.open_tree_handle
    attacked = False

    def attack(path: Path, *, directory: bool) -> object:
        nonlocal attacked
        if path == child and not attacked:
            child.rmdir()
            try:
                child.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlink creation unavailable")
            attacked = True
        return original_open(path, directory=directory)

    monkeypatch.setattr(api, "open_tree_handle", attack)
    with pytest.raises(RegistryIOError):
        store._io.secure_remove_tree(lease.profile_path, 100)
    assert attacked
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ownership")
def test_windows_secure_remove_closes_every_handle_after_mark_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    tree = store.root / "trash" / ("a" * 32)
    store._io.ensure_directory(tree)
    (tree / "state").write_text("x", encoding="ascii")
    api = store._io._api
    original_open = api.open_tree_handle
    original_close = api.close_tree_handle
    original_mark = api.mark_tree_handle_delete
    opened: set[int] = set()

    def tracked_open(path: Path, *, directory: bool) -> object:
        handle, information = original_open(path, directory=directory)
        opened.add(handle)
        return handle, information

    def tracked_close(handle: int) -> None:
        opened.discard(handle)
        original_close(handle)

    def fail_mark(handle: int) -> None:
        raise RegistryIOError("injected mark failure")

    monkeypatch.setattr(api, "open_tree_handle", tracked_open)
    monkeypatch.setattr(api, "close_tree_handle", tracked_close)
    monkeypatch.setattr(api, "mark_tree_handle_delete", fail_mark)
    with pytest.raises(RegistryIOError):
        store._io.secure_remove_tree(tree, 100)
    assert opened == set()
    monkeypatch.setattr(api, "mark_tree_handle_delete", original_mark)
    store._io.secure_remove_tree(tree, 100)
    assert not tree.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX dirfd swap defense")
def test_posix_openat_swap_attack_executes_without_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    tree = store.root / "trash" / ("b" * 32)
    store._io.ensure_directory(tree)
    child = tree / "child"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("keep", encoding="ascii")
    original_open = os.open
    swapped = False

    def attack_open(path: str | bytes, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal swapped
        if path == "child" and dir_fd is not None and flags & getattr(os, "O_DIRECTORY", 0) and not swapped:
            child.rmdir()
            child.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", attack_open)
    with pytest.raises(RegistryIOError):
        store._io.secure_remove_tree(tree, 100)
    assert swapped
    assert sentinel.read_text(encoding="ascii") == "keep"


def test_public_storage_errors_have_only_safe_code_and_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with pytest.raises(ValueError):
        BrowserSessionError("bad code")

    def unsafe_failure(path: Path) -> object:
        raise RegistryIOError(f"cookie=C:/private/{path.name}")

    monkeypatch.setattr(store._io, "open_lock", unsafe_failure)
    with pytest.raises(BrowserSessionError) as caught:
        store.acquire("safe.example")
    assert caught.value.code == "allocation_io"
    assert caught.value.domain == "safe.example"
    assert "cookie" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert caught.value.__suppress_context__


def test_lease_release_failure_is_safe_and_still_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    lease = store.acquire("release.example")

    def fail(path: Path, info: object) -> None:
        raise OSError(f"cookie=secret path={path}")

    monkeypatch.setattr(store, "_write_info", fail)
    with pytest.raises(BrowserSessionError) as caught:
        lease.close()
    assert caught.value.code == "release_failed"
    assert caught.value.domain == "release.example"
    assert "cookie" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert caught.value.__suppress_context__
    monkeypatch.undo()
    with store.acquire("release.example", timeout=0.5):
        pass


def test_invalid_deletion_binding_is_quarantined(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("deleting.example"):
        pass
    metadata = next((store.root / "metadata").glob("*.json"))
    value = json.loads(metadata.read_bytes())
    value["status"] = "deleting"
    value["tombstone_id"] = "not-an-id"
    metadata.write_text(json.dumps(value), encoding="ascii")
    with pytest.raises(BrowserSessionError, match="metadata_corrupt"):
        store.get("deleting.example")
    assert not list((store.root / "quarantine").glob("*.bad"))


def test_nonquarantining_read_and_profile_escape_fail_safely(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    with store.acquire("corrupt.example") as lease:
        metadata = next((store.root / "metadata").glob("*.json"))
        metadata.write_text("{}", encoding="ascii")
        with pytest.raises(BrowserSessionError, match="release_failed"):
            lease.close()
    store._safe_delete_profile(store.root / "profiles" / ("f" * 64))
    with pytest.raises(BrowserSessionError, match="profile_escape"):
        store._verify_profile(store.root)


def test_recovery_scan_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    store = BrowserSessionStore(root)
    for name in ("one", "two"):
        (store.root / "metadata" / name).write_text("{}", encoding="ascii")
    with pytest.raises(SessionLimitError, match="metadata_scan_limit"):
        BrowserSessionStore(root, max_scan_entries=1)


def test_tombstone_only_and_orphan_trash_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "sessions"
    store = BrowserSessionStore(root)
    with store.acquire("tombstone.example") as lease:
        session_id = lease.info.session_id
    original_remove = store._io.secure_remove_tree

    def fail(path: Path, max_entries: int) -> None:
        raise RegistryIOError("injected")

    monkeypatch.setattr(store._io, "secure_remove_tree", fail)
    with pytest.raises(BrowserSessionError):
        store.clear("tombstone.example", session_id, confirmation=True)
    next((root / "metadata").glob("*.json")).unlink()
    monkeypatch.setattr(store._io, "secure_remove_tree", original_remove)
    recovered = BrowserSessionStore(root)
    assert not list((root / "tombstones").iterdir())
    assert not list((root / "trash").iterdir())

    orphan = root / "trash" / ("f" * 32)
    recovered._io.ensure_directory(orphan)
    (orphan / "state").write_text("x", encoding="ascii")
    BrowserSessionStore(root)
    assert not orphan.exists()


def test_invalid_orphan_tombstone_is_quarantined(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    store = BrowserSessionStore(root)
    invalid = root / "tombstones" / ("a" * 32 + ".json")
    store._io.atomic_write(invalid, b"{}")
    BrowserSessionStore(root)
    assert not invalid.exists()
    assert list((root / "quarantine").glob("*.bad"))


def test_defensive_internal_state_branches_are_safe(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions")
    store._quarantine_metadata(store.root / "metadata" / "missing.json")
    store._delete_metadata(store.root / "metadata" / "missing.json")
    unsafe = store.root / "profiles" / ("e" * 64)
    unsafe.write_text("not-directory", encoding="ascii")
    with pytest.raises((BrowserSessionError, RegistryIOError)):
        store._verify_profile(unsafe)

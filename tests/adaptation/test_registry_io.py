from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from novel_crawler.adaptation.registry_io import (
    PosixRegistryIO,
    RegistryIOError,
    WindowsAPI,
    WindowsRegistryIO,
    default_registry_io,
)


class FakeWindowsAPI:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def reject_reparse(self, path: Path) -> None:
        self.events.append(("reparse", path.name))

    def apply_private_acl(self, path: Path) -> None:
        self.events.append(("acl", path.name))

    def verify_private_acl(self, path: Path) -> None:
        self.events.append(("verify", path.name))

    def flush_file(self, descriptor: int) -> None:
        assert descriptor >= 0
        self.events.append(("flush", "file"))

    def move_write_through(self, source: Path, destination: Path) -> None:
        self.events.append(("move", destination.name))
        os.replace(source, destination)


def test_windows_atomic_write_orders_acl_flush_and_write_through_move(tmp_path: Path) -> None:
    api = FakeWindowsAPI()
    io = WindowsRegistryIO(api=api)
    target = tmp_path / "value.json"

    io.ensure_directory(tmp_path)
    api.events.clear()
    io.atomic_write(target, b"payload")

    names = [event[0] for event in api.events]
    assert names.index("flush") < names.index("move")
    assert names.index("acl") < names.index("move")
    assert target.read_bytes() == b"payload"


def test_same_handle_bounded_read_rejects_oversize_and_non_regular(tmp_path: Path) -> None:
    io = default_registry_io()
    root = tmp_path / "private"
    io.ensure_directory(root)
    value = root / "value.json"
    io.atomic_write(value, b"12345")

    assert io.read_bounded(value, 5) == b"12345"
    with pytest.raises(RegistryIOError, match="maximum bytes"):
        io.read_bounded(value, 4)
    with pytest.raises(RegistryIOError, match="regular|opened"):
        io.read_bounded(root, 100)


def test_bounded_read_rejects_growth_after_fstat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    io = default_registry_io()
    root = tmp_path / "private"
    io.ensure_directory(root)
    value = root / "value.json"
    io.atomic_write(value, b"12345")
    original_fstat = os.fstat

    def stale_fstat(descriptor: int) -> object:
        metadata = original_fstat(descriptor)
        return SimpleNamespace(st_mode=metadata.st_mode, st_uid=getattr(metadata, "st_uid", 0), st_size=4)

    monkeypatch.setattr(os, "fstat", stale_fstat)
    with pytest.raises(RegistryIOError, match="maximum bytes"):
        io.read_bounded(value, 4)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes")
def test_posix_root_is_owned_private_and_regular_files_are_private(tmp_path: Path) -> None:
    io = default_registry_io()
    root = tmp_path / "private"
    io.ensure_directory(root)
    target = root / "data.json"
    io.atomic_write(target, b"{}")

    root_stat = root.stat()
    file_stat = target.stat()
    assert root_stat.st_uid == os.getuid()
    assert stat.S_IMODE(root_stat.st_mode) == 0o700
    assert stat.S_IMODE(file_stat.st_mode) == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL verification")
def test_windows_actual_private_acl_round_trip(tmp_path: Path) -> None:
    io = default_registry_io()
    root = tmp_path / "private"
    io.ensure_directory(root)
    io.verify_private(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL exactness")
def test_windows_acl_verifier_rejects_any_extra_granting_ace(tmp_path: Path) -> None:
    io = default_registry_io()
    root = tmp_path / "private"
    io.ensure_directory(root)
    changed = subprocess.run(
        ["icacls", str(root), "/grant", "*S-1-1-0:(F)"], capture_output=True, check=False
    )
    if changed.returncode != 0:
        pytest.skip("icacls mutation unavailable")
    with pytest.raises(RegistryIOError, match="ACL verification"):
        io.verify_private(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows fail-closed Win32 errors")
def test_windows_get_attributes_failure_is_not_treated_as_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = WindowsAPI()

    def access_denied(path: str) -> int:
        ctypes.set_last_error(5)
        return 0xFFFFFFFF

    import ctypes

    monkeypatch.setattr(api._kernel32, "GetFileAttributesW", access_denied)
    with pytest.raises(RegistryIOError, match="verification"):
        api.reject_reparse(tmp_path / "missing")


@pytest.mark.skipif(os.name != "nt", reason="Windows real durability APIs")
def test_windows_real_flush_and_write_through_are_exercised(tmp_path: Path) -> None:
    class SpyAPI(WindowsAPI):
        def __init__(self) -> None:
            self.events: list[str] = []
            super().__init__()

        def flush_file(self, descriptor: int) -> None:
            self.events.append("flush")
            super().flush_file(descriptor)

        def move_write_through(self, source: Path, destination: Path) -> None:
            self.events.append("move")
            super().move_write_through(source, destination)

    api = SpyAPI()
    io = WindowsRegistryIO(api=api)
    io.atomic_write(tmp_path / "private" / "value.json", b"payload")
    assert api.events == ["flush", "move"]


def test_open_lock_closes_descriptor_when_acl_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenLockACL(FakeWindowsAPI):
        def verify_private_acl(self, path: Path) -> None:
            if path.name == "registry.lock":
                raise RegistryIOError("injected lock ACL failure")
            super().verify_private_acl(path)

    closed: list[int] = []
    original_close = os.close

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(os, "close", close)
    io = WindowsRegistryIO(api=BrokenLockACL())
    with pytest.raises(RegistryIOError, match="injected"):
        io.open_lock(tmp_path / "private" / "registry.lock")
    assert closed


def test_nofollow_read_rejects_file_symlink(tmp_path: Path) -> None:
    io = default_registry_io()
    root = tmp_path / "private"
    io.ensure_directory(root)
    outside = tmp_path / "outside.json"
    outside.write_text("secret", encoding="utf-8")
    link = root / "linked.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(RegistryIOError, match="reparse|handle|symlink"):
        io.read_bounded(link, 100)


def test_permission_or_durability_failure_is_fail_closed(tmp_path: Path) -> None:
    class BrokenAPI(FakeWindowsAPI):
        def verify_private_acl(self, path: Path) -> None:
            raise RegistryIOError("private ACL verification failed")

    io = WindowsRegistryIO(api=BrokenAPI())
    with pytest.raises(RegistryIOError, match="verification"):
        io.ensure_directory(tmp_path / "private")


def test_windows_write_through_failure_leaves_target_uncommitted(tmp_path: Path) -> None:
    class BrokenMoveAPI(FakeWindowsAPI):
        def move_write_through(self, source: Path, destination: Path) -> None:
            raise RegistryIOError("injected write-through failure")

    io = WindowsRegistryIO(api=BrokenMoveAPI())
    target = tmp_path / "private" / "value.json"
    with pytest.raises(RegistryIOError, match="write-through"):
        io.atomic_write(target, b"secret")
    assert not target.exists()
    assert not list(target.parent.glob("*.tmp"))


def test_windows_lock_rejects_non_file_target(tmp_path: Path) -> None:
    io = WindowsRegistryIO(api=FakeWindowsAPI())
    with pytest.raises(RegistryIOError, match="lock"):
        io.open_lock(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir-fd durability ordering")
def test_posix_mkdir_file_fsync_replace_dir_fsync_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    original_mkdir = os.mkdir
    original_fsync = os.fsync
    original_replace = os.replace

    def mkdir(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        events.append("mkdir")
        original_mkdir(path, mode, dir_fd=dir_fd)

    def fsync(descriptor: int) -> None:
        kind = "dir_fsync" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file_fsync"
        events.append(kind)
        original_fsync(descriptor)

    def replace(source: str, destination: str, **kwargs: int) -> None:
        events.append("replace")
        original_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    io = PosixRegistryIO()
    root = tmp_path / "private"
    io.ensure_directory(root)
    events.clear()
    io.atomic_write(root / "nested" / "value.json", b"payload")

    assert events.index("mkdir") < events.index("file_fsync") < events.index("replace")
    assert events.index("replace") < len(events) - 1
    assert events[-1] == "dir_fsync"


@pytest.mark.skipif(os.name == "nt", reason="POSIX fault injection")
@pytest.mark.parametrize("boundary", ["mkdir", "file_fsync", "replace", "dir_fsync"])
def test_posix_durability_boundary_failures_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    io = PosixRegistryIO()
    root = tmp_path / "private"
    if boundary != "mkdir":
        io.ensure_directory(root)
    original_mkdir = os.mkdir
    original_fsync = os.fsync
    original_replace = os.replace
    file_was_flushed = False

    def mkdir(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if boundary == "mkdir":
            raise OSError("injected mkdir failure")
        original_mkdir(path, mode, dir_fd=dir_fd)

    def fsync(descriptor: int) -> None:
        nonlocal file_was_flushed
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if not is_directory:
            file_was_flushed = True
            if boundary == "file_fsync":
                raise OSError("injected file fsync failure")
        elif boundary == "dir_fsync" and file_was_flushed:
            raise OSError("injected directory fsync failure")
        original_fsync(descriptor)

    def replace(source: str, destination: str, **kwargs: int) -> None:
        if boundary == "replace":
            raise OSError("injected replace failure")
        original_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    target = root / "value.json"
    with pytest.raises(RegistryIOError):
        if boundary == "mkdir":
            io.ensure_directory(root)
        else:
            io.atomic_write(target, b"complete")
    assert not target.exists() or target.read_bytes() == b"complete"

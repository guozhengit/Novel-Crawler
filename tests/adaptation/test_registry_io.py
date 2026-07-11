from __future__ import annotations

import ctypes
import os
import stat
import subprocess
from ctypes import wintypes
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

    def move_write_through(self, source: Path, destination: Path, *, replace: bool = True) -> None:
        self.events.append(("move", destination.name))
        if not replace and destination.exists():
            raise RegistryIOError("already exists")
        os.replace(source, destination)

    def delete_private_path(self, path: Path) -> None:
        path.unlink(missing_ok=True)


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
    with pytest.raises(RegistryIOError, match="regular|opened|anchored"):
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


@pytest.mark.skipif(os.name != "nt", reason="Windows file/directory DACL flags")
def test_windows_private_acl_round_trip_uses_object_appropriate_ace_flags(tmp_path: Path) -> None:
    class AclHeader(ctypes.Structure):
        _fields_ = [
            ("AclRevision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("AclSize", ctypes.c_ushort),
            ("AceCount", ctypes.c_ushort),
            ("Sbz2", ctypes.c_ushort),
        ]

    def ace_flags(api: WindowsAPI, path: Path) -> list[int]:
        handle = api._open_path_handle(path, 0x00020000)
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        try:
            result = api._advapi32.GetSecurityInfo(
                handle, 1, api._DACL, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
            )
            assert result == 0 and dacl and descriptor
            acl = ctypes.cast(dacl, ctypes.POINTER(AclHeader)).contents
            assert acl.AceCount == 2
            result_flags: list[int] = []
            for index in range(acl.AceCount):
                ace = wintypes.LPVOID()
                assert api._advapi32.GetAce(dacl, index, ctypes.byref(ace))
                assert ace.value is not None
                result_flags.append(ctypes.c_ubyte.from_address(ace.value + 1).value)
            return result_flags
        finally:
            if descriptor:
                api._kernel32.LocalFree(descriptor)
            api._kernel32.CloseHandle(handle)

    api = WindowsAPI()
    io = WindowsRegistryIO(api=api)
    root = tmp_path / "private"
    target = root / "value.json"
    io.ensure_directory(root)
    io.atomic_write(target, b"payload")

    assert ace_flags(api, root) == [0x03, 0x03]
    assert ace_flags(api, target) == [0x00, 0x00]


@pytest.mark.skipif(os.name != "nt", reason="Windows object-specific SDDL")
@pytest.mark.parametrize(("attributes", "expected_flags"), [(0x10, "OICI"), (0x80, "")])
def test_windows_acl_application_selects_sddl_from_handle_type(
    monkeypatch: pytest.MonkeyPatch, attributes: int, expected_flags: str
) -> None:
    api = WindowsAPI()
    captured: list[str] = []

    def get_information(handle: int, information: object) -> bool:
        information._obj.dwFileAttributes = attributes  # type: ignore[attr-defined]
        return True

    def convert(sddl: str, revision: int, descriptor: object, size: object) -> bool:
        captured.append(sddl)
        descriptor._obj.value = 1  # type: ignore[attr-defined]
        return True

    def get_dacl(descriptor: object, present: object, dacl: object, defaulted: object) -> bool:
        present._obj.value = 1  # type: ignore[attr-defined]
        dacl._obj.value = 1  # type: ignore[attr-defined]
        return True

    monkeypatch.setattr(api._kernel32, "GetFileInformationByHandle", get_information)
    monkeypatch.setattr(api._advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW", convert)
    monkeypatch.setattr(api._advapi32, "GetSecurityDescriptorDacl", get_dacl)
    monkeypatch.setattr(api._advapi32, "SetSecurityInfo", lambda *args: 0)
    monkeypatch.setattr(api._kernel32, "LocalFree", lambda value: None)

    api._apply_private_handle(123)

    assert captured == [
        f"O:{api._owner_sid}D:P(A;{expected_flags};FA;;;{api._owner_sid})(A;{expected_flags};FA;;;SY)"
    ]


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


@pytest.mark.skipif(os.name != "nt", reason="Windows anchored handles")
def test_windows_anchor_holds_parent_without_delete_sharing(tmp_path: Path) -> None:
    io = default_registry_io()
    root = tmp_path / "private"
    nested = root / "nested"
    io.ensure_directory(root)
    io.ensure_directory(nested)
    outside = tmp_path / "outside"
    with io._api.anchor_guard(root, nested):  # type: ignore[attr-defined]
        assert not io._api._kernel32.MoveFileExW(str(root), str(outside), 0x1)  # type: ignore[attr-defined]
    assert root.exists() and not outside.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows canonical handle path")
def test_windows_anchor_rejects_final_path_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    io = default_registry_io()
    root = tmp_path / "private"
    io.ensure_directory(root)
    api = io._api  # type: ignore[attr-defined]
    monkeypatch.setattr(api, "_final_path", lambda handle: str(tmp_path / "outside"))
    with pytest.raises(RegistryIOError, match="canonical"):
        with api.anchor_guard(root, root):
            pass


@pytest.mark.skipif(os.name != "nt", reason="Windows canonical handle cleanup")
def test_windows_path_handle_closes_when_final_path_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    io = default_registry_io()
    root = tmp_path / "private"
    io.ensure_directory(root)
    api = io._api  # type: ignore[attr-defined]
    closed: list[int] = []
    original_close = api._kernel32.CloseHandle

    def close_handle(handle: int) -> bool:
        closed.append(handle)
        return bool(original_close(handle))

    def fail_final_path(handle: int) -> str:
        raise RegistryIOError("injected final path lookup failure")

    monkeypatch.setattr(api._kernel32, "CloseHandle", close_handle)
    monkeypatch.setattr(api, "_final_path", fail_final_path)

    with pytest.raises(RegistryIOError, match="injected final path"):
        api._open_path_handle(root, 0x00020000)

    assert len(closed) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows anchored create/move")
def test_windows_parent_swap_injection_cannot_redirect_create_or_move(tmp_path: Path) -> None:
    class SwapAPI(WindowsAPI):
        def __init__(self) -> None:
            self.swap_parent: Path | None = None
            self.swap_target: Path | None = None
            self.swap_attempts = 0
            super().__init__()

        def _attempt_swap(self) -> None:
            if self.swap_parent is None or self.swap_target is None:
                return
            self.swap_attempts += 1
            assert not self._kernel32.MoveFileExW(str(self.swap_parent), str(self.swap_target), 0x1)

        def create_private_directory(self, path: Path) -> None:
            self._attempt_swap()
            super().create_private_directory(path)

        def move_write_through(self, source: Path, destination: Path, *, replace: bool = True) -> None:
            self._attempt_swap()
            super().move_write_through(source, destination, replace=replace)

    api = SwapAPI()
    io = WindowsRegistryIO(api=api)
    root = tmp_path / "private"
    io.ensure_directory(root)
    api.swap_parent = root
    api.swap_target = tmp_path / "swapped"
    io.ensure_directory(root / "nested")
    io.atomic_write(root / "nested" / "value.json", b"inside")
    assert api.swap_attempts >= 2
    assert (root / "nested" / "value.json").read_bytes() == b"inside"
    assert not api.swap_target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows SetSecurityInfo ABI")
def test_windows_acl_uses_declared_set_security_info_abi() -> None:
    api = WindowsAPI()
    assert api._advapi32.SetSecurityInfo.argtypes == [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    assert api._advapi32.SetSecurityInfo.restype is wintypes.DWORD


@pytest.mark.skipif(os.name != "nt", reason="Windows bootstrap anchor")
def test_windows_existing_root_bootstrap_is_anchored_before_acl_operation(tmp_path: Path) -> None:
    root = tmp_path / "existing-root"
    root.mkdir()
    outside = tmp_path / "swapped-bootstrap"

    class BootstrapSwapAPI(WindowsAPI):
        def apply_private_acl(self, path: Path) -> None:
            if path == root:
                assert not self._kernel32.MoveFileExW(str(root), str(outside), 0x1)
            super().apply_private_acl(path)

    io = WindowsRegistryIO(api=BootstrapSwapAPI())
    io.ensure_directory(root)
    assert root.exists() and not outside.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows fail-closed Win32 errors")
def test_windows_get_attributes_failure_is_not_treated_as_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = WindowsAPI()

    def access_denied(path: str) -> int:
        ctypes.set_last_error(5)
        return 0xFFFFFFFF

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

        def move_write_through(self, source: Path, destination: Path, *, replace: bool = True) -> None:
            self.events.append("move")
            super().move_write_through(source, destination, replace=replace)

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
        def move_write_through(self, source: Path, destination: Path, *, replace: bool = True) -> None:
            raise RegistryIOError("injected write-through failure")

    io = WindowsRegistryIO(api=BrokenMoveAPI())
    target = tmp_path / "private" / "value.json"
    with pytest.raises(RegistryIOError, match="write-through"):
        io.atomic_write(target, b"secret")
    assert not target.exists()
    assert not list(target.parent.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows anchored failure cleanup")
@pytest.mark.parametrize("boundary", ["flush", "move"])
def test_windows_failed_publish_keeps_parent_anchored_through_temp_cleanup(
    tmp_path: Path, boundary: str
) -> None:
    class BrokenPublishAPI(WindowsAPI):
        failure: str | None = None
        cleanup_hook: object | None = None

        def flush_file(self, descriptor: int) -> None:
            if self.failure == "flush":
                raise RegistryIOError("injected flush failure")
            super().flush_file(descriptor)

        def move_write_through(self, source: Path, destination: Path, *, replace: bool = True) -> None:
            if self.failure == "move":
                raise RegistryIOError("injected move failure")
            super().move_write_through(source, destination, replace=replace)

        def delete_private_path(self, path: Path) -> None:
            if callable(self.cleanup_hook):
                self.cleanup_hook(path)
            super().delete_private_path(path)

    api = BrokenPublishAPI()
    io = WindowsRegistryIO(api=api)
    root = tmp_path / "private"
    displaced = tmp_path / "displaced-private"
    external = tmp_path / "external"
    external.mkdir()
    io.ensure_directory(root)
    api.failure = boundary
    swap_succeeded = False
    sentinel: Path | None = None
    junction_created = False

    def before_cleanup(path: Path) -> None:
        nonlocal swap_succeeded, sentinel, junction_created
        if path.parent == root and path.name.endswith(".tmp"):
            sentinel = external / path.name
            sentinel.write_bytes(b"outside-sentinel")
            swap_succeeded = bool(api._kernel32.MoveFileExW(str(root), str(displaced), 0x1))
            if swap_succeeded:
                linked = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(root), str(external)],
                    capture_output=True,
                    check=False,
                )
                if linked.returncode != 0:
                    assert api._kernel32.MoveFileExW(str(displaced), str(root), 0x1)
                    pytest.skip("directory junction creation unavailable")
                junction_created = True

    api.cleanup_hook = before_cleanup
    try:
        with pytest.raises(RegistryIOError, match=f"injected {boundary}"):
            io.atomic_write(root / "value.json", b"secret")
        sentinel_survived = sentinel is not None and sentinel.read_bytes() == b"outside-sentinel"
    finally:
        if junction_created:
            os.rmdir(root)
        if displaced.exists():
            assert api._kernel32.MoveFileExW(str(displaced), str(root), 0x1)

    assert not swap_succeeded
    assert sentinel_survived
    assert not list(root.glob("*.tmp"))


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor lifecycle")
def test_posix_child_descriptor_closes_when_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    io = PosixRegistryIO()
    closed: list[int] = []
    original_close = os.close

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(io, "_verify_directory_fd", lambda descriptor: (_ for _ in ()).throw(RegistryIOError("boom")))
    with pytest.raises(RegistryIOError, match="boom"):
        io.ensure_directory(tmp_path / "private")
    assert len(closed) >= 2

"""Fail-closed platform IO primitives for the sensitive config registry."""

from __future__ import annotations

import ctypes
import os
import stat
import uuid
from ctypes import wintypes
from io import BufferedRandom
from pathlib import Path
from typing import Any, Protocol

_GETUID: Any = getattr(os, "getuid", lambda: -1)
_FCHMOD: Any = getattr(os, "fchmod", None)


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD), ("ftCreationTimeLow", wintypes.DWORD),
        ("ftCreationTimeHigh", wintypes.DWORD), ("ftLastAccessTimeLow", wintypes.DWORD),
        ("ftLastAccessTimeHigh", wintypes.DWORD), ("ftLastWriteTimeLow", wintypes.DWORD),
        ("ftLastWriteTimeHigh", wintypes.DWORD), ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD), ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD), ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _AclHeader(ctypes.Structure):
    _fields_ = [
        ("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte),
        ("AclSize", ctypes.c_ushort), ("AceCount", ctypes.c_ushort), ("Sbz2", ctypes.c_ushort),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", ctypes.c_ushort)]


class RegistryIOError(RuntimeError):
    """A private-access or durability guarantee could not be established."""


class RegistryIOSizeError(RegistryIOError):
    """A same-handle bounded read exceeded its configured limit."""


class RegistryIO(Protocol):
    def ensure_directory(self, path: Path) -> None: ...
    def verify_private(self, path: Path) -> None: ...
    def atomic_write(self, path: Path, payload: bytes) -> None: ...
    def read_bounded(self, path: Path, limit: int) -> bytes: ...
    def durable_move(self, source: Path, destination: Path) -> None: ...
    def open_lock(self, path: Path) -> BufferedRandom: ...
    def reject_link(self, path: Path) -> None: ...


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise RegistryIOError("durable file write failed")
        view = view[written:]


def _read_limit(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise RegistryIOSizeError("file exceeds maximum bytes")
    return payload


class PosixRegistryIO:  # pragma: no cover - exercised by POSIX CI
    def reject_link(self, path: Path) -> None:
        descriptor = self._open_directory(path.parent, require_private=False)
        try:
            metadata = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise RegistryIOError("registry path must not contain symlinks")
        except FileNotFoundError:
            pass
        finally:
            os.close(descriptor)

    def _open_directory(self, path: Path, *, require_private: bool) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(os.sep, flags)
            for component in path.absolute().parts[1:]:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise RegistryIOError("registry directory cannot be opened safely") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise RegistryIOError("registry path is not a directory")
            if require_private and (metadata.st_uid != _GETUID() or stat.S_IMODE(metadata.st_mode) & 0o077):
                raise RegistryIOError("registry directory is not private to the current owner")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def ensure_directory(self, path: Path) -> None:
        path = path.absolute()
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(os.sep, flags)
        try:
            for name in path.parts[1:]:
                try:
                    child_fd = os.open(name, flags, dir_fd=parent_fd)
                except FileNotFoundError:
                    os.mkdir(name, 0o700, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                    child_fd = os.open(name, flags, dir_fd=parent_fd)
                    _FCHMOD(child_fd, 0o700)
                    self._verify_directory_fd(child_fd)
                    os.fsync(child_fd)
                try:
                    os.close(parent_fd)
                finally:
                    parent_fd = child_fd
            _FCHMOD(parent_fd, 0o700)
            self._verify_directory_fd(parent_fd)
            os.fsync(parent_fd)
        except OSError as exc:
            raise RegistryIOError("private registry directory setup failed") from exc
        finally:
            os.close(parent_fd)

    @staticmethod
    def _verify_directory_fd(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != _GETUID()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise RegistryIOError("private registry directory verification failed")

    def verify_private(self, path: Path) -> None:
        descriptor = self._open_directory(path, require_private=True)
        try:
            self._verify_directory_fd(descriptor)
        finally:
            os.close(descriptor)

    def atomic_write(self, path: Path, payload: bytes) -> None:
        self.ensure_directory(path.parent)
        parent_fd = self._open_directory(path.parent, require_private=True)
        temporary = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RegistryIOError("registry output is not a regular file")
            _FCHMOD(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as exc:
            raise RegistryIOError("atomic durable registry write failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            os.close(parent_fd)

    def read_bounded(self, path: Path, limit: int) -> bytes:
        parent_fd = self._open_directory(path.parent, require_private=True)
        descriptor = -1
        try:
            descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RegistryIOError("registry input is not a regular file")
            if metadata.st_uid != _GETUID() or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise RegistryIOError("registry input is not private")
            if metadata.st_size > limit:
                raise RegistryIOSizeError("file exceeds maximum bytes")
            return _read_limit(descriptor, limit)
        except OSError as exc:
            raise RegistryIOError("registry input cannot be opened safely") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_fd)

    def durable_move(self, source: Path, destination: Path) -> None:
        self.ensure_directory(destination.parent)
        source_fd = self._open_directory(source.parent, require_private=True)
        destination_fd = self._open_directory(destination.parent, require_private=True)
        try:
            os.replace(source.name, destination.name, src_dir_fd=source_fd, dst_dir_fd=destination_fd)
            os.fsync(source_fd)
            if destination_fd != source_fd:
                os.fsync(destination_fd)
        except OSError as exc:
            raise RegistryIOError("durable quarantine move failed") from exc
        finally:
            os.close(source_fd)
            os.close(destination_fd)

    def open_lock(self, path: Path) -> BufferedRandom:
        self.ensure_directory(path.parent)
        parent_fd = self._open_directory(path.parent, require_private=True)
        descriptor = -1
        try:
            descriptor = os.open(
                path.name,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RegistryIOError("registry lock is not a regular file")
            _FCHMOD(descriptor, 0o600)
            stream = os.fdopen(descriptor, "r+b")
            descriptor = -1
            return stream
        except OSError as exc:
            raise RegistryIOError("registry lock cannot be opened safely") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_fd)


class _WindowsAPI(Protocol):
    def reject_reparse(self, path: Path) -> None: ...
    def apply_private_acl(self, path: Path) -> None: ...
    def verify_private_acl(self, path: Path) -> None: ...
    def flush_file(self, descriptor: int) -> None: ...
    def move_write_through(self, source: Path, destination: Path) -> None: ...
    def create_private_directory(self, path: Path) -> None: ...


class WindowsAPI:  # pragma: no cover - validated by Windows integration tests
    _DACL = 0x00000004
    _OWNER = 0x00000001
    _PROTECTED_DACL = 0x80000000
    _INVALID_ATTRIBUTES = 0xFFFFFFFF
    _REPARSE = 0x400

    def __init__(self) -> None:
        if os.name != "nt":
            raise RegistryIOError("Windows security APIs are unavailable")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetFileAttributesW.restype = ctypes.c_ulong
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL
        self._kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self._kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self._kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        self._kernel32.MoveFileExW.restype = wintypes.BOOL
        self._kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
        self._kernel32.CreateDirectoryW.restype = wintypes.BOOL
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD),
        ]
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self._advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
        self._advapi32.SetFileSecurityW.restype = wintypes.BOOL
        self._advapi32.SetKernelObjectSecurity.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID]
        self._advapi32.SetKernelObjectSecurity.restype = wintypes.BOOL
        self._advapi32.GetFileSecurityW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        self._advapi32.GetFileSecurityW.restype = wintypes.BOOL
        self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD),
        ]
        self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        self._advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self._advapi32.OpenProcessToken.restype = wintypes.BOOL
        self._advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        self._advapi32.GetTokenInformation.restype = wintypes.BOOL
        self._advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
        self._advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self._advapi32.GetSecurityInfo.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
        ]
        self._advapi32.GetSecurityInfo.restype = wintypes.DWORD
        self._advapi32.GetAce.argtypes = [wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]
        self._advapi32.GetAce.restype = wintypes.BOOL
        self._advapi32.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
        self._advapi32.EqualSid.restype = wintypes.BOOL
        self._advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)]
        self._advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
        self._advapi32.GetSecurityDescriptorControl.argtypes = [
            wintypes.LPVOID, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(wintypes.DWORD),
        ]
        self._advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        self._owner_sid = self._current_owner_sid()
        self._owner_sid_pointer = self._sid_pointer(self._owner_sid)
        self._system_sid_pointer = self._sid_pointer("S-1-5-18")

    def _sid_pointer(self, value: str) -> wintypes.LPVOID:
        pointer = wintypes.LPVOID()
        if not self._advapi32.ConvertStringSidToSidW(value, ctypes.byref(pointer)):
            raise RegistryIOError("security SID construction failed")
        return pointer

    def _current_owner_sid(self) -> str:
        token = wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(self._kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise RegistryIOError("current owner SID lookup failed")
        try:
            needed = wintypes.DWORD()
            self._advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if not needed.value:
                raise RegistryIOError("current owner SID lookup failed")
            buffer = ctypes.create_string_buffer(needed.value)
            if not self._advapi32.GetTokenInformation(token, 1, buffer, needed.value, ctypes.byref(needed)):
                raise RegistryIOError("current owner SID lookup failed")
            sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
            rendered = wintypes.LPWSTR()
            if not self._advapi32.ConvertSidToStringSidW(sid, ctypes.byref(rendered)):
                raise RegistryIOError("current owner SID lookup failed")
            try:
                value = rendered.value
                if not value:
                    raise RegistryIOError("current owner SID lookup failed")
                return value
            finally:
                self._kernel32.LocalFree(rendered)
        finally:
            self._kernel32.CloseHandle(token)

    def reject_reparse(self, path: Path) -> None:
        if self.is_reparse(path):
            raise RegistryIOError("registry path must not be a symlink or reparse point")

    def is_reparse(self, path: Path) -> bool:
        ctypes.set_last_error(0)
        attributes = self._kernel32.GetFileAttributesW(str(path))
        if attributes == self._INVALID_ATTRIBUTES:
            error = ctypes.get_last_error()
            if error not in {2, 3}:
                raise RegistryIOError("reparse-point verification failed")
            return False
        return bool(attributes & self._REPARSE)

    def apply_private_acl(self, path: Path) -> None:
        descriptor = ctypes.c_void_p()
        sddl = f"O:{self._owner_sid}D:P(A;OICI;FA;;;{self._owner_sid})(A;OICI;FA;;;SY)"
        convert = self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        if not convert(sddl, 1, ctypes.byref(descriptor), None):
            raise RegistryIOError("private ACL construction failed")
        handle = self._open_path_handle(path, 0x00040000)
        try:
            security_info = self._DACL | self._PROTECTED_DACL
            if not self._advapi32.SetKernelObjectSecurity(handle, security_info, descriptor):
                raise RegistryIOError("private ACL application failed")
        finally:
            self._kernel32.CloseHandle(handle)
            self._kernel32.LocalFree(descriptor)

    def _open_path_handle(self, path: Path, access: int) -> int:
        handle = self._kernel32.CreateFileW(str(path), access, 0x7, None, 3, 0x02000000 | 0x00200000, None)
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            raise RegistryIOError("registry path handle cannot be opened safely")
        information = _ByHandleFileInformation()
        if not self._kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
            self._kernel32.CloseHandle(handle)
            raise RegistryIOError("registry path handle verification failed")
        if information.dwFileAttributes & self._REPARSE:
            self._kernel32.CloseHandle(handle)
            raise RegistryIOError("registry path handle is a reparse point")
        return handle

    def create_private_directory(self, path: Path) -> None:
        class SecurityAttributes(ctypes.Structure):
            _fields_ = [
                ("nLength", ctypes.c_ulong),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", ctypes.c_int),
            ]

        descriptor = ctypes.c_void_p()
        sddl = f"O:{self._owner_sid}D:P(A;OICI;FA;;;{self._owner_sid})(A;OICI;FA;;;SY)"
        convert = self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        if not convert(sddl, 1, ctypes.byref(descriptor), None):
            raise RegistryIOError("private ACL construction failed")
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, 0)
        try:
            if not self._kernel32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                if ctypes.get_last_error() != 183:
                    raise RegistryIOError("private registry directory creation failed")
        finally:
            self._kernel32.LocalFree(descriptor)

    def verify_private_acl(self, path: Path) -> None:
        handle = self._open_path_handle(path, 0x00020000)
        try:
            self._verify_private_handle(handle)
        finally:
            self._kernel32.CloseHandle(handle)

    def _verify_private_handle(self, handle: int) -> None:
        owner = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        security_info = self._OWNER | self._DACL
        result = self._advapi32.GetSecurityInfo(
            handle, 1, security_info, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
        )
        if result != 0 or not descriptor:
            raise RegistryIOError("private ACL verification failed")
        if not owner or not self._advapi32.EqualSid(owner, self._owner_sid_pointer):
            self._kernel32.LocalFree(descriptor)
            raise RegistryIOError("private ACL verification failed")
        control = ctypes.c_ushort()
        revision = wintypes.DWORD()
        if not self._advapi32.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            self._kernel32.LocalFree(descriptor)
            raise RegistryIOError("private ACL verification failed")
        if not control.value & 0x1000 or not dacl:
            self._kernel32.LocalFree(descriptor)
            raise RegistryIOError("private ACL verification failed")
        acl = ctypes.cast(dacl, ctypes.POINTER(_AclHeader)).contents
        if acl.AceCount != 2:
            self._kernel32.LocalFree(descriptor)
            raise RegistryIOError("private ACL verification failed")
        seen_owner = False
        seen_system = False
        for index in range(acl.AceCount):
            ace = wintypes.LPVOID()
            if not self._advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                self._kernel32.LocalFree(descriptor)
                raise RegistryIOError("private ACL verification failed")
            ace_address = ace.value
            if ace_address is None:
                self._kernel32.LocalFree(descriptor)
                raise RegistryIOError("private ACL verification failed")
            header = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
            mask = ctypes.c_ulong.from_address(ace_address + 4).value
            sid = wintypes.LPVOID(ace_address + 8)
            if header.AceType != 0 or header.AceFlags != 0x03 or mask != 0x001F01FF:
                self._kernel32.LocalFree(descriptor)
                raise RegistryIOError("private ACL verification failed")
            if self._advapi32.EqualSid(sid, self._owner_sid_pointer):
                seen_owner = True
            elif self._advapi32.EqualSid(sid, self._system_sid_pointer):
                seen_system = True
            else:
                self._kernel32.LocalFree(descriptor)
                raise RegistryIOError("private ACL verification failed")
        if not seen_owner or not seen_system:
            self._kernel32.LocalFree(descriptor)
            raise RegistryIOError("private ACL verification failed")
        rendered = ctypes.c_wchar_p()
        length = ctypes.c_ulong()
        convert = self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        try:
            if not convert(descriptor, 1, security_info, ctypes.byref(rendered), ctypes.byref(length)):
                raise RegistryIOError("private ACL verification failed")
            try:
                sddl = rendered.value or ""
            finally:
                self._kernel32.LocalFree(rendered)
        finally:
            self._kernel32.LocalFree(descriptor)
        owner_ace = f"(A;OICI;FA;;;{self._owner_sid})"
        system_ace = "(A;OICI;FA;;;SY)"
        if f"O:{self._owner_sid}" not in sddl or "D:P" not in sddl or sddl.count("(A;") != 2:
            raise RegistryIOError("private ACL verification failed")
        if owner_ace not in sddl or system_ace not in sddl:
            raise RegistryIOError("private ACL verification failed")

    def open_nofollow_fd(self, path: Path, *, write: bool, create: bool, exclusive: bool = True) -> int:
        import msvcrt

        access = 0x80000000 | (0x40000000 if write else 0)
        creation = (1 if exclusive else 4) if create else 3
        handle = self._kernel32.CreateFileW(str(path), access, 0x7, None, creation, 0x80 | 0x00200000, None)
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            raise RegistryIOError("registry file handle cannot be opened safely")
        try:
            information = _ByHandleFileInformation()
            if not self._kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
                raise RegistryIOError("registry handle verification failed")
            if information.dwFileAttributes & (self._REPARSE | 0x10):
                raise RegistryIOError("registry handle is not a regular non-reparse file")
            flags = getattr(os, "O_BINARY", 0) | (os.O_RDWR if write else os.O_RDONLY)
            descriptor = msvcrt.open_osfhandle(handle, flags)
            handle = invalid
            return descriptor
        finally:
            if handle != invalid:
                self._kernel32.CloseHandle(handle)

    def verify_private_fd(self, descriptor: int, path: Path) -> None:
        import msvcrt

        self._verify_private_handle(msvcrt.get_osfhandle(descriptor))

    def flush_file(self, descriptor: int) -> None:
        import msvcrt

        handle = msvcrt.get_osfhandle(descriptor)
        if not self._kernel32.FlushFileBuffers(handle):
            raise RegistryIOError("file durability flush failed")

    def move_write_through(self, source: Path, destination: Path) -> None:
        if not self._kernel32.MoveFileExW(str(source), str(destination), 0x1 | 0x8):
            raise RegistryIOError("write-through atomic move failed")


class WindowsRegistryIO:
    def __init__(self, *, api: _WindowsAPI | None = None) -> None:
        self._api = api or WindowsAPI()

    def ensure_directory(self, path: Path) -> None:
        path = path.absolute()
        for component in (path, *path.parents):
            self._api.reject_reparse(component)
        missing: list[Path] = []
        cursor = path
        while not cursor.exists():
            self._api.reject_reparse(cursor)
            missing.append(cursor)
            cursor = cursor.parent
        self._api.reject_reparse(cursor)
        for directory in reversed(missing):
            try:
                create_private = getattr(self._api, "create_private_directory", None)
                if create_private is None:
                    directory.mkdir(mode=0o700)
                else:
                    create_private(directory)
            except FileExistsError:
                pass
            except OSError as exc:
                raise RegistryIOError("private registry directory creation failed") from exc
            self._api.reject_reparse(directory)
            self._api.apply_private_acl(directory)
            self._api.verify_private_acl(directory)
        if not missing:
            self._api.reject_reparse(path)
            self._api.apply_private_acl(path)
            self._api.verify_private_acl(path)

    def verify_private(self, path: Path) -> None:
        self._api.reject_reparse(path)
        self._api.verify_private_acl(path)

    def reject_link(self, path: Path) -> None:
        self._api.reject_reparse(path)

    def atomic_write(self, path: Path, payload: bytes) -> None:
        self.ensure_directory(path.parent)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        descriptor = -1
        try:
            self._api.reject_reparse(temporary)
            secure_open = getattr(self._api, "open_nofollow_fd", None)
            descriptor = (
                secure_open(temporary, write=True, create=True)
                if secure_open is not None
                else os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RegistryIOError("registry output is not a regular file")
            _write_all(descriptor, payload)
            self._api.flush_file(descriptor)
            os.close(descriptor)
            descriptor = -1
            self._api.apply_private_acl(temporary)
            self._api.verify_private_acl(temporary)
            self._api.reject_reparse(path)
            self._api.move_write_through(temporary, path)
            self._api.verify_private_acl(path)
        except OSError as exc:
            raise RegistryIOError("atomic durable registry write failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def read_bounded(self, path: Path, limit: int) -> bytes:
        self._api.reject_reparse(path)
        descriptor = -1
        try:
            secure_open = getattr(self._api, "open_nofollow_fd", None)
            descriptor = (
                secure_open(path, write=False, create=False)
                if secure_open is not None
                else os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RegistryIOError("registry input is not a regular file")
            if metadata.st_size > limit:
                raise RegistryIOSizeError("file exceeds maximum bytes")
            verify_fd = getattr(self._api, "verify_private_fd", None)
            if verify_fd is None:
                self._api.verify_private_acl(path)
            else:
                verify_fd(descriptor, path)
            return _read_limit(descriptor, limit)
        except OSError as exc:
            raise RegistryIOError("registry input cannot be opened safely") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def durable_move(self, source: Path, destination: Path) -> None:
        self.ensure_directory(destination.parent)
        is_reparse = getattr(self._api, "is_reparse", None)
        if is_reparse is None:
            self._api.reject_reparse(source)
            source_is_reparse = False
        else:
            source_is_reparse = bool(is_reparse(source))
        self._api.reject_reparse(destination)
        self._api.move_write_through(source, destination)
        if not source_is_reparse:
            self._api.apply_private_acl(destination)
            self._api.verify_private_acl(destination)

    def open_lock(self, path: Path) -> BufferedRandom:
        self.ensure_directory(path.parent)
        self._api.reject_reparse(path)
        descriptor = -1
        try:
            secure_open = getattr(self._api, "open_nofollow_fd", None)
            descriptor = (
                secure_open(path, write=True, create=True, exclusive=False)
                if secure_open is not None
                else os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise RegistryIOError("registry lock is not a regular file")
            self._api.apply_private_acl(path)
            verify_fd = getattr(self._api, "verify_private_fd", None)
            if verify_fd is None:
                self._api.verify_private_acl(path)
            else:
                verify_fd(descriptor, path)
            stream = os.fdopen(descriptor, "r+b")
            descriptor = -1
            return stream
        except OSError as exc:
            raise RegistryIOError("registry lock cannot be opened safely") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def default_registry_io() -> RegistryIO:
    return WindowsRegistryIO() if os.name == "nt" else PosixRegistryIO()


__all__ = ["RegistryIO", "RegistryIOError", "RegistryIOSizeError", "WindowsRegistryIO", "default_registry_io"]

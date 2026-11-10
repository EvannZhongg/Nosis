"""Handle-relative filesystem operations for the file tools on Windows."""

import ctypes
import msvcrt
import os
import stat
from ctypes import wintypes as w
from uuid import uuid4


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")

_READ_ATTRIBUTES = 0x80
_TRAVERSE = 0x20
_DELETE = 0x10000
_SYNCHRONIZE = 0x100000
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_SHARE_READ_WRITE = 3
_SHARE_ALL = 7
_DIRECTORY = 1
_NON_DIRECTORY = 0x40
_OPEN = 1
_CREATE = 2
_DIRECTORY_ATTRIBUTE = 0x10
_REPARSE_ATTRIBUTE = 0x400
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_OBJ_CASE_INSENSITIVE = 0x40
_OBJ_DONT_REPARSE = 0x1000
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_SYNCHRONOUS_IO_NONALERT = 0x20
_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_DISPOSITION_INFO = 4
_FILE_RENAME_INFORMATION = 10


class _UnicodeString(ctypes.Structure):
    _fields_ = [("length", w.USHORT), ("capacity", w.USHORT), ("buffer", w.LPWSTR)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", w.ULONG), ("root", w.HANDLE),
        ("name", ctypes.POINTER(_UnicodeString)), ("flags", w.ULONG),
        ("security", w.LPVOID), ("qos", w.LPVOID),
    ]


class _IoStatus(ctypes.Structure):
    _fields_ = [("status", w.LPVOID), ("information", ctypes.c_size_t)]


class _AttributeTag(ctypes.Structure):
    _fields_ = [("attributes", w.DWORD), ("tag", w.DWORD)]


class _RenameInfo(ctypes.Structure):
    _fields_ = [
        ("replace", w.BOOL), ("directory", w.HANDLE),
        ("length", w.DWORD), ("name", w.WCHAR * 1),
    ]


_kernel.CreateFileW.argtypes = [
    w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE,
]
_kernel.CreateFileW.restype = w.HANDLE
_kernel.CloseHandle.argtypes = [w.HANDLE]
_kernel.CloseHandle.restype = w.BOOL
_kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
_kernel.GetFileInformationByHandleEx.restype = w.BOOL
_kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
_kernel.SetFileInformationByHandle.restype = w.BOOL
_ntdll.NtCreateFile.argtypes = [
    ctypes.POINTER(w.HANDLE), w.DWORD, ctypes.POINTER(_ObjectAttributes),
    ctypes.POINTER(_IoStatus), w.LPVOID, w.ULONG, w.ULONG, w.ULONG,
    w.ULONG, w.LPVOID, w.ULONG,
]
_ntdll.NtCreateFile.restype = w.LONG
_ntdll.NtSetInformationFile.argtypes = [
    w.HANDLE, ctypes.POINTER(_IoStatus), w.LPVOID, w.ULONG, ctypes.c_int,
]
_ntdll.NtSetInformationFile.restype = w.LONG
_ntdll.RtlNtStatusToDosError.argtypes = [w.LONG]
_ntdll.RtlNtStatusToDosError.restype = w.ULONG


def _attributes(handle: int) -> int:
    info = _AttributeTag()
    if not _kernel.GetFileInformationByHandleEx(
        handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.attributes


def _checked(handle: int, name: str) -> int:
    try:
        if _attributes(handle) & _REPARSE_ATTRIBUTE:
            raise ValueError(f"file path changed to a reparse point: {name}")
        return handle
    except BaseException:
        close(handle)
        raise


def _open(
    parent: int, name: str, access: int, share: int,
    *, disposition: int = _OPEN, options: int = 0,
) -> int:
    encoded = name.encode("utf-16-le", errors="surrogatepass")
    if len(encoded) > 65532:
        raise ValueError("file name is too long")
    buffer = ctypes.create_string_buffer(encoded, len(encoded) + 2)
    value = _UnicodeString(
        len(encoded), ctypes.sizeof(buffer), ctypes.cast(buffer, w.LPWSTR),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes), parent, ctypes.pointer(value),
        _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
    )
    handle = w.HANDLE()
    status = _ntdll.NtCreateFile(
        ctypes.byref(handle), access | _READ_ATTRIBUTES | _SYNCHRONIZE,
        ctypes.byref(attributes), ctypes.byref(_IoStatus()), None,
        0, share, disposition,
        options | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
        None, 0,
    )
    if status < 0:
        raise ctypes.WinError(_ntdll.RtlNtStatusToDosError(status))
    return _checked(handle.value, name)


def open_root(anchor: str) -> int:
    handle = _kernel.CreateFileW(
        anchor, _READ_ATTRIBUTES | _TRAVERSE, _SHARE_READ_WRITE, None, 3,
        0x02200000,  # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    return _checked(handle, anchor)


def close(directory: int) -> None:
    _kernel.CloseHandle(directory)


def open_directory(parent: int, name: str) -> int:
    # Keeping directory handles without delete sharing prevents moving them
    # out of the tree while an operation is in progress.
    return _open(parent, name, _TRAVERSE, _SHARE_READ_WRITE, options=_DIRECTORY)


def mkdir(parent: int, name: str) -> None:
    close(_open(
        parent, name, _TRAVERSE, _SHARE_READ_WRITE,
        disposition=_CREATE, options=_DIRECTORY,
    ))


def mode(parent: int, name: str) -> int:
    handle = _open(parent, name, 0, _SHARE_ALL)
    try:
        return stat.S_IFDIR if _attributes(handle) & _DIRECTORY_ATTRIBUTE else stat.S_IFREG
    finally:
        close(handle)


def open_read(parent: int, name: str) -> int:
    handle = _open(parent, name, _GENERIC_READ, _SHARE_ALL, options=_NON_DIRECTORY)
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise


def _mark_for_deletion(handle: int) -> None:
    delete = w.BOOL(True)
    if not _kernel.SetFileInformationByHandle(
        handle, _FILE_DISPOSITION_INFO, ctypes.byref(delete), ctypes.sizeof(delete),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _rename(handle: int, parent: int, name: str, replace: bool) -> None:
    encoded = name.encode("utf-16-le", errors="surrogatepass")
    buffer = ctypes.create_string_buffer(ctypes.sizeof(_RenameInfo) + len(encoded))
    info = _RenameInfo.from_buffer(buffer)
    info.replace = replace
    info.directory = parent
    info.length = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + _RenameInfo.name.offset, encoded, len(encoded))
    status = _ntdll.NtSetInformationFile(
        handle, ctypes.byref(_IoStatus()), ctypes.byref(buffer),
        ctypes.sizeof(buffer), _FILE_RENAME_INFORMATION,
    )
    if status < 0:
        raise ctypes.WinError(_ntdll.RtlNtStatusToDosError(status))


def write(
    parent: int, name: str, data: bytes, *, replace: bool, permissions: int | None
) -> None:
    handle = _open(
        parent, f".nosis-{uuid4().hex}.tmp", _GENERIC_WRITE | _DELETE, 0,
        disposition=_CREATE, options=_NON_DIRECTORY,
    )
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        try:
            _mark_for_deletion(handle)
        finally:
            close(handle)
        raise
    with os.fdopen(descriptor, "wb") as file:
        try:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
            _rename(handle, parent, name, replace)
        except BaseException:
            _mark_for_deletion(handle)
            raise


def unlink(parent: int, name: str) -> None:
    handle = _open(parent, name, _DELETE, _SHARE_ALL, options=_NON_DIRECTORY)
    try:
        _mark_for_deletion(handle)
    finally:
        close(handle)

"""Windows write-restricted process confinement.

The backend uses a restricted primary token whose restricting SID list
contains capabilities granted only on the workspace and a private temporary
directory.  Windows therefore intersects write access with those grants while
retaining the caller's normal read access.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO


TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_ADJUST_DEFAULT = 0x0080
SE_GROUP_LOGON_ID = 0xC0000000
DISABLE_MAX_PRIVILEGE = 0x0001
LUA_TOKEN = 0x0004
WRITE_RESTRICTED = 0x0008
TOKEN_GROUPS = 2
TOKEN_USER = 1
TOKEN_DEFAULT_DACL = 6
SE_PRIVILEGE_ENABLED = 0x00000002
WIN_WORLD_SID = 1
SECURITY_MAX_SID_SIZE = 68

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
GRANT_ACCESS = 1
REVOKE_ACCESS = 4
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x03
ACCESS_ALLOWED_ACE_TYPE = 0
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_EXECUTE = 0x001200A0
DELETE = 0x00010000
FILE_DELETE_CHILD = 0x00000040
STANDARD_RIGHTS_WRITE = 0x00020000
WORKSPACE_GRANT = (
    FILE_GENERIC_READ
    | FILE_GENERIC_WRITE
    | FILE_GENERIC_EXECUTE
    | DELETE
    | FILE_DELETE_CHILD
) & ~STANDARD_RIGHTS_WRITE
GENERIC_ALL = 0x10000000

STARTF_USESTDHANDLES = 0x00000100
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", wintypes.DWORD),
    ]


class TOKEN_GROUPS_ONE(ctypes.Structure):
    _fields_ = [
        ("GroupCount", wintypes.DWORD),
        ("Groups", SID_AND_ATTRIBUTES * 1),
    ]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", wintypes.DWORD),
        ("TrusteeForm", wintypes.DWORD),
        ("TrusteeType", wintypes.DWORD),
        ("ptstrName", ctypes.c_void_p),
    ]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", wintypes.DWORD),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", TRUSTEE_W),
    ]


class ACL(ctypes.Structure):
    _fields_ = [
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", wintypes.WORD),
        ("AceCount", wintypes.WORD),
        ("Sbz2", wintypes.WORD),
    ]


class ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class ACCESS_ALLOWED_ACE(ctypes.Structure):
    _fields_ = [
        ("Header", ACE_HEADER),
        ("Mask", wintypes.DWORD),
        ("SidStart", wintypes.DWORD),
    ]


class TOKEN_DEFAULT_DACL_INFO(ctypes.Structure):
    _fields_ = [("DefaultDacl", ctypes.c_void_p)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES_ONE(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", wintypes.DWORD),
        ("Privileges", LUID_AND_ATTRIBUTES * 1),
    ]


class STARTUPINFO_W(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEX_W(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFO_W),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _WindowsApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows sandbox APIs require Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel32.LocalFree.restype = ctypes.c_void_p
        self.kernel32.WaitForSingleObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.GetExitCodeProcess.restype = wintypes.BOOL

        self.advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL
        self.advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetTokenInformation.restype = wintypes.BOOL
        self.advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
        self.advapi32.GetLengthSid.restype = wintypes.DWORD
        self.advapi32.CopySid.argtypes = [
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.advapi32.CopySid.restype = wintypes.BOOL
        self.advapi32.CreateWellKnownSid.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.CreateWellKnownSid.restype = wintypes.BOOL
        self.advapi32.ConvertStringSidToSidW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
        self.advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.advapi32.EqualSid.restype = wintypes.BOOL
        self.advapi32.CreateRestrictedToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(SID_AND_ATTRIBUTES),
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi32.CreateRestrictedToken.restype = wintypes.BOOL
        self.advapi32.SetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.advapi32.SetTokenInformation.restype = wintypes.BOOL
        self.advapi32.LookupPrivilegeValueW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.POINTER(LUID),
        ]
        self.advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
        self.advapi32.AdjustTokenPrivileges.argtypes = [
            wintypes.HANDLE,
            wintypes.BOOL,
            ctypes.POINTER(TOKEN_PRIVILEGES_ONE),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL
        self.advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.advapi32.SetEntriesInAclW.argtypes = [
            wintypes.ULONG,
            ctypes.POINTER(EXPLICIT_ACCESS_W),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi32.SetEntriesInAclW.restype = wintypes.DWORD
        self.advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        self.advapi32.GetAce.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi32.GetAce.restype = wintypes.BOOL
        self.advapi32.CreateProcessAsUserW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFO_W),
            ctypes.POINTER(PROCESS_INFORMATION),
        ]
        self.advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
        self.kernel32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t)
        ]
        self.kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        self.kernel32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p
        ]
        self.kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        self.kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        self.kernel32.DeleteProcThreadAttributeList.restype = None

    def error(self, operation: str, code: int | None = None) -> OSError:
        error_code = ctypes.get_last_error() if code is None else code
        return OSError(
            error_code,
            f"{operation} failed: {ctypes.FormatError(error_code).strip()}",
        )

    def close_handle(self, handle: wintypes.HANDLE | None) -> None:
        if handle and not self.kernel32.CloseHandle(handle):
            raise self.error("CloseHandle")

    def local_free(self, pointer: ctypes.c_void_p | None) -> None:
        if pointer and self.kernel32.LocalFree(pointer):
            raise self.error("LocalFree")


class _WindowsProcess:
    def __init__(
        self,
        api: _WindowsApi,
        handle: wintypes.HANDLE,
        pid: int,
        argv: list[str],
    ) -> None:
        self._api = api
        self._handle = handle
        self.pid = pid
        self.args = argv
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        status = self._api.kernel32.WaitForSingleObject(self._handle, 0)
        if status == WAIT_TIMEOUT:
            return None
        if status != WAIT_OBJECT_0:
            raise self._api.error("WaitForSingleObject")
        return self._finish()

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        milliseconds = (
            INFINITE
            if timeout is None
            else max(0, min(INFINITE - 1, int(timeout * 1000)))
        )
        status = self._api.kernel32.WaitForSingleObject(
            self._handle, milliseconds
        )
        if status == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if status != WAIT_OBJECT_0:
            raise self._api.error("WaitForSingleObject")
        return self._finish()

    def _finish(self) -> int:
        exit_code = wintypes.DWORD()
        if not self._api.kernel32.GetExitCodeProcess(
            self._handle, ctypes.byref(exit_code)
        ):
            raise self._api.error("GetExitCodeProcess")
        self.returncode = exit_code.value
        self._api.close_handle(self._handle)
        self._handle = None
        return self.returncode

    def __del__(self) -> None:
        if getattr(self, "_handle", None):
            self._api.kernel32.CloseHandle(self._handle)


class WindowsWriteRestrictedSandbox:
    """Own one workspace's ACL capabilities and restricted token."""

    def __init__(self, workspace: Path, private_tmp: Path) -> None:
        self.workspace = workspace
        self.private_tmp = private_tmp
        self._api = _WindowsApi()
        self._workspace_sid: ctypes.c_void_p | None = None
        self._temporary_sid: ctypes.c_void_p | None = None
        self._user_sid: ctypes.Array | None = None
        self._token: wintypes.HANDLE | None = None
        self._granted_paths: list[tuple[Path, ctypes.c_void_p]] = []
        try:
            self._workspace_sid_value = _capability_sid(
                str(workspace), temporary=False
            )
            self._temporary_sid_value = _capability_sid(
                str(private_tmp), temporary=True
            )
            self._workspace_sid = self._convert_sid(
                self._workspace_sid_value
            )
            self._temporary_sid = self._convert_sid(
                self._temporary_sid_value
            )
            self._user_sid = self._current_user_sid()
            if self._grant_write(workspace, self._workspace_sid):
                self._granted_paths.append((workspace, self._workspace_sid))
            if self._grant_write(private_tmp, self._temporary_sid):
                self._granted_paths.append((private_tmp, self._temporary_sid))
            self._token = self._create_restricted_token()
        except BaseException:
            self.close()
            raise

    def start(
        self,
        argv: list[str],
        *,
        cwd: Path,
        environment: dict[str, str] | None,
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> _WindowsProcess:
        if self._token is None:
            raise RuntimeError("the Windows sandbox is closed")

        import msvcrt

        stdin_file = open(os.devnull, "rb")
        handles = [
            msvcrt.get_osfhandle(stdin_file.fileno()),
            msvcrt.get_osfhandle(stdout.fileno()),
            msvcrt.get_osfhandle(stderr.fileno()),
        ]
        for handle in handles:
            os.set_handle_inheritable(handle, True)
        attribute_buffer = None
        attribute_list = ctypes.c_void_p()
        try:
            startup_ex = STARTUPINFOEX_W()
            startup = startup_ex.StartupInfo
            startup.cb = ctypes.sizeof(startup_ex)
            startup.dwFlags = STARTF_USESTDHANDLES
            startup.hStdInput = handles[0]
            startup.hStdOutput = handles[1]
            startup.hStdError = handles[2]
            required = ctypes.c_size_t()
            self._api.kernel32.InitializeProcThreadAttributeList(
                None, 1, 0, ctypes.byref(required)
            )
            attribute_buffer = ctypes.create_string_buffer(required.value)
            if not self._api.kernel32.InitializeProcThreadAttributeList(
                attribute_buffer, 1, 0, ctypes.byref(required)
            ):
                raise self._api.error("InitializeProcThreadAttributeList")
            attribute_list = ctypes.cast(attribute_buffer, ctypes.c_void_p)
            inherited = (wintypes.HANDLE * len(handles))(*handles)
            if not self._api.kernel32.UpdateProcThreadAttribute(
                attribute_list, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.byref(inherited), ctypes.sizeof(inherited), None, None
            ):
                raise self._api.error("UpdateProcThreadAttribute")
            startup_ex.lpAttributeList = attribute_list
            info = PROCESS_INFORMATION()
            normalized_argv = [os.path.normpath(os.path.realpath(argv[0])), *argv[1:]]
            command_line = ctypes.create_unicode_buffer(
                subprocess.list2cmdline(normalized_argv)
            )
            environment_block = _environment_block(environment or os.environ)
            created = self._api.advapi32.CreateProcessAsUserW(
                self._token,
                None,
                command_line,
                None,
                None,
                True,
                CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT,
                ctypes.cast(environment_block, ctypes.c_void_p),
                str(cwd),
                ctypes.cast(ctypes.byref(startup_ex), ctypes.POINTER(STARTUPINFO_W)),
                ctypes.byref(info),
            )
            if not created:
                raise self._api.error("CreateProcessAsUserW")
            try:
                self._api.close_handle(info.hThread)
            except BaseException:
                self._api.close_handle(info.hProcess)
                raise
            return _WindowsProcess(
                self._api, info.hProcess, info.dwProcessId, normalized_argv
            )
        finally:
            if attribute_list:
                self._api.kernel32.DeleteProcThreadAttributeList(attribute_list)
            for handle in handles:
                os.set_handle_inheritable(handle, False)
            stdin_file.close()

    def close(self) -> None:
        failures: list[BaseException] = []
        if self._token is not None:
            try:
                self._api.close_handle(self._token)
            except BaseException as error:
                failures.append(error)
            self._token = None
        for path, sid in reversed(self._granted_paths):
            try:
                self._revoke_write(path, sid)
            except BaseException as error:
                failures.append(error)
        self._granted_paths.clear()
        for attribute in ("_workspace_sid", "_temporary_sid"):
            pointer = getattr(self, attribute, None)
            if pointer is not None:
                try:
                    self._api.local_free(pointer)
                except BaseException as error:
                    failures.append(error)
                setattr(self, attribute, None)
        if failures:
            raise ExceptionGroup("Windows sandbox cleanup failed", failures)

    def _convert_sid(self, value: str) -> ctypes.c_void_p:
        pointer = ctypes.c_void_p()
        if not self._api.advapi32.ConvertStringSidToSidW(
            value, ctypes.byref(pointer)
        ):
            raise self._api.error("ConvertStringSidToSidW")
        return pointer

    def _grant_write(self, path: Path, sid: ctypes.c_void_p) -> bool:
        old_acl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = self._api.advapi32.GetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(old_acl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise self._api.error("GetNamedSecurityInfoW", result)
        try:
            if old_acl and self._has_write_grant(old_acl, sid):
                return False
            new_acl = ctypes.c_void_p()
            entry = _explicit_access(sid, WORKSPACE_GRANT)
            result = self._api.advapi32.SetEntriesInAclW(
                1, ctypes.byref(entry), old_acl, ctypes.byref(new_acl)
            )
            if result:
                raise self._api.error("SetEntriesInAclW", result)
            try:
                result = self._api.advapi32.SetNamedSecurityInfoW(
                    str(path),
                    SE_FILE_OBJECT,
                    DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    new_acl,
                    None,
                )
                if result:
                    raise self._api.error("SetNamedSecurityInfoW", result)
                return True
            finally:
                self._api.local_free(new_acl)
        finally:
            self._api.local_free(descriptor)

    def _revoke_write(self, path: Path, sid: ctypes.c_void_p) -> None:
        old_acl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = self._api.advapi32.GetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(old_acl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise self._api.error("GetNamedSecurityInfoW", result)
        try:
            if not old_acl:
                return
            new_acl = ctypes.c_void_p()
            entry = _explicit_access(sid, 0, mode=REVOKE_ACCESS)
            result = self._api.advapi32.SetEntriesInAclW(
                1, ctypes.byref(entry), old_acl, ctypes.byref(new_acl)
            )
            if result:
                raise self._api.error("SetEntriesInAclW(REVOKE_ACCESS)", result)
            try:
                old_count = ctypes.cast(old_acl, ctypes.POINTER(ACL)).contents.AceCount
                new_count = ctypes.cast(new_acl, ctypes.POINTER(ACL)).contents.AceCount
                if new_count == old_count:
                    return
                result = self._api.advapi32.SetNamedSecurityInfoW(
                    str(path),
                    SE_FILE_OBJECT,
                    DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    new_acl,
                    None,
                )
                if result:
                    raise self._api.error(
                        "SetNamedSecurityInfoW(REVOKE_ACCESS)", result
                    )
            finally:
                self._api.local_free(new_acl)
        finally:
            self._api.local_free(descriptor)

    def _has_write_grant(
        self, acl_pointer: ctypes.c_void_p, sid: ctypes.c_void_p
    ) -> bool:
        acl = ctypes.cast(acl_pointer, ctypes.POINTER(ACL)).contents
        for index in range(acl.AceCount):
            ace_pointer = ctypes.c_void_p()
            if not self._api.advapi32.GetAce(
                acl_pointer, index, ctypes.byref(ace_pointer)
            ):
                raise self._api.error("GetAce")
            ace = ctypes.cast(
                ace_pointer, ctypes.POINTER(ACCESS_ALLOWED_ACE)
            ).contents
            if (
                ace.Header.AceType == ACCESS_ALLOWED_ACE_TYPE
                and ace.Header.AceFlags
                & SUB_CONTAINERS_AND_OBJECTS_INHERIT
                == SUB_CONTAINERS_AND_OBJECTS_INHERIT
                and ace.Mask == WORKSPACE_GRANT
            ):
                ace_sid = ctypes.c_void_p(ace_pointer.value + 8)
                if self._api.advapi32.EqualSid(ace_sid, sid):
                    return True
        return False

    def _create_restricted_token(self) -> wintypes.HANDLE:
        current_token = wintypes.HANDLE()
        rights = (
            TOKEN_ASSIGN_PRIMARY
            | TOKEN_DUPLICATE
            | TOKEN_QUERY
            | TOKEN_ADJUST_PRIVILEGES
            | TOKEN_ADJUST_DEFAULT
        )
        if not self._api.advapi32.OpenProcessToken(
            self._api.kernel32.GetCurrentProcess(),
            rights,
            ctypes.byref(current_token),
        ):
            raise self._api.error("OpenProcessToken")
        try:
            logon_sid = self._logon_sid(current_token)
            world_sid = ctypes.create_string_buffer(SECURITY_MAX_SID_SIZE)
            world_size = wintypes.DWORD(SECURITY_MAX_SID_SIZE)
            if not self._api.advapi32.CreateWellKnownSid(
                WIN_WORLD_SID, None, world_sid, ctypes.byref(world_size)
            ):
                raise self._api.error("CreateWellKnownSid")
            restricting_sids = (
                ctypes.cast(logon_sid, ctypes.c_void_p),
                ctypes.cast(world_sid, ctypes.c_void_p),
                self._workspace_sid,
                self._temporary_sid,
            )
            restricting = (SID_AND_ATTRIBUTES * len(restricting_sids))(
                *(SID_AND_ATTRIBUTES(sid, 0) for sid in restricting_sids)
            )
            token = wintypes.HANDLE()
            if not self._api.advapi32.CreateRestrictedToken(
                current_token,
                DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED,
                0,
                None,
                0,
                None,
                len(restricting_sids),
                restricting,
                ctypes.byref(token),
            ):
                raise self._api.error("CreateRestrictedToken")
            try:
                self._set_token_default_dacl(
                    token, (
                        *restricting_sids,
                        ctypes.cast(self._user_sid, ctypes.c_void_p),
                        ctypes.cast(world_sid, ctypes.c_void_p),
                    )
                )
                self._enable_privilege(token, "SeChangeNotifyPrivilege")
            except BaseException:
                self._api.close_handle(token)
                raise
            return token
        finally:
            self._api.close_handle(current_token)

    def _current_user_sid(self) -> ctypes.Array:
        current_token = wintypes.HANDLE()
        if not self._api.advapi32.OpenProcessToken(
            self._api.kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(current_token)
        ):
            raise self._api.error("OpenProcessToken")
        try:
            needed = wintypes.DWORD()
            self._api.advapi32.GetTokenInformation(
                current_token, TOKEN_USER, None, 0, ctypes.byref(needed)
            )
            token_user = ctypes.create_string_buffer(needed.value)
            if not self._api.advapi32.GetTokenInformation(
                current_token, TOKEN_USER, token_user, needed, ctypes.byref(needed)
            ):
                raise self._api.error("GetTokenInformation(TokenUser)")
            sid = ctypes.c_void_p.from_buffer(token_user).value
            length = self._api.advapi32.GetLengthSid(sid)
            copied = ctypes.create_string_buffer(length)
            if not self._api.advapi32.CopySid(length, copied, sid):
                raise self._api.error("CopySid(TokenUser)")
            return copied
        finally:
            self._api.close_handle(current_token)

    def _enable_privilege(self, token: wintypes.HANDLE, name: str) -> None:
        luid = LUID()
        if not self._api.advapi32.LookupPrivilegeValueW(
            None, name, ctypes.byref(luid)
        ):
            raise self._api.error("LookupPrivilegeValueW")
        privileges = TOKEN_PRIVILEGES_ONE(
            1,
            (LUID_AND_ATTRIBUTES(luid, SE_PRIVILEGE_ENABLED),),
        )
        ctypes.set_last_error(0)
        if not self._api.advapi32.AdjustTokenPrivileges(
            token,
            False,
            ctypes.byref(privileges),
            0,
            None,
            None,
        ):
            raise self._api.error("AdjustTokenPrivileges")
        if ctypes.get_last_error():
            raise self._api.error("AdjustTokenPrivileges")

    def _logon_sid(self, token: wintypes.HANDLE) -> ctypes.Array:
        needed = wintypes.DWORD()
        self._api.advapi32.GetTokenInformation(
            token, TOKEN_GROUPS, None, 0, ctypes.byref(needed)
        )
        if not needed.value:
            raise self._api.error("GetTokenInformation(TokenGroups)")
        groups = ctypes.create_string_buffer(needed.value)
        if not self._api.advapi32.GetTokenInformation(
            token,
            TOKEN_GROUPS,
            groups,
            needed,
            ctypes.byref(needed),
        ):
            raise self._api.error("GetTokenInformation(TokenGroups)")
        count = ctypes.cast(groups, ctypes.POINTER(wintypes.DWORD)).contents.value
        first = ctypes.addressof(groups) + TOKEN_GROUPS_ONE.Groups.offset
        for index in range(count):
            group = SID_AND_ATTRIBUTES.from_address(
                first + index * ctypes.sizeof(SID_AND_ATTRIBUTES)
            )
            if group.Attributes & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID:
                length = self._api.advapi32.GetLengthSid(group.Sid)
                if not length:
                    raise self._api.error("GetLengthSid")
                copied = ctypes.create_string_buffer(length)
                if not self._api.advapi32.CopySid(
                    length, copied, group.Sid
                ):
                    raise self._api.error("CopySid")
                return copied
        raise RuntimeError("the current access token has no logon SID")

    def _set_token_default_dacl(
        self,
        token: wintypes.HANDLE,
        sids: tuple[ctypes.c_void_p, ...],
    ) -> None:
        new_acl = ctypes.c_void_p()
        entries = (EXPLICIT_ACCESS_W * len(sids))(
            *(
                _explicit_access(sid, GENERIC_ALL, inheritance=0)
                for sid in sids
            )
        )
        result = self._api.advapi32.SetEntriesInAclW(
            len(entries), entries, None, ctypes.byref(new_acl)
        )
        if result:
            raise self._api.error("SetEntriesInAclW(TokenDefaultDacl)", result)
        try:
            info = TOKEN_DEFAULT_DACL_INFO(new_acl)
            if not self._api.advapi32.SetTokenInformation(
                token,
                TOKEN_DEFAULT_DACL,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                raise self._api.error("SetTokenInformation(TokenDefaultDacl)")
        finally:
            self._api.local_free(new_acl)


def _explicit_access(
    sid: ctypes.c_void_p,
    permissions: int,
    *,
    mode: int = GRANT_ACCESS,
    inheritance: int = SUB_CONTAINERS_AND_OBJECTS_INHERIT,
) -> EXPLICIT_ACCESS_W:
    if not isinstance(sid, ctypes.c_void_p):
        sid = ctypes.cast(sid, ctypes.c_void_p)
    trustee = TRUSTEE_W(
        None,
        0,
        TRUSTEE_IS_SID,
        TRUSTEE_IS_UNKNOWN,
        sid,
    )
    return EXPLICIT_ACCESS_W(
        permissions,
        mode,
        inheritance,
        trustee,
    )


def _capability_sid(path: str, *, temporary: bool) -> str:
    prefix = b"temp\0" if temporary else b"workspace\0"
    canonical = os.path.normcase(os.path.realpath(path)).encode("utf-8")
    digest = hashlib.sha256(prefix + canonical).digest()
    first = int.from_bytes(digest[:4], "little") % (2**30 - 1) + 1
    second = int.from_bytes(digest[4:8], "little") % (2**30 - 1) + 1
    suffix = "-1" if temporary else ""
    return f"S-1-4-{first}-{second}{suffix}"


def _environment_block(environment: dict[str, str]) -> ctypes.Array:
    entries = (
        f"{key}={value}"
        for key, value in sorted(environment.items(), key=lambda item: item[0].upper())
    )
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0\0")

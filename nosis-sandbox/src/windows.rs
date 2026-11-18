use super::*;
use std::{
    ffi::OsStr,
    os::windows::{ffi::OsStrExt, io::AsRawHandle},
    ptr::{null, null_mut},
};
use windows_sys::Win32::{
    Foundation::{
        CloseHandle, FILETIME, GetLastError, HANDLE, LUID, LocalFree, SetLastError, WAIT_ABANDONED,
        WAIT_OBJECT_0, WAIT_TIMEOUT,
    },
    Security::Authorization::{
        ConvertStringSidToSidW, EXPLICIT_ACCESS_W, GRANT_ACCESS, GetNamedSecurityInfoW,
        REVOKE_ACCESS, SE_FILE_OBJECT, SetEntriesInAclW, SetNamedSecurityInfoW, TRUSTEE_IS_SID,
        TRUSTEE_IS_UNKNOWN, TRUSTEE_W,
    },
    Security::{
        AdjustTokenPrivileges, CreateRestrictedToken, CreateWellKnownSid,
        DACL_SECURITY_INFORMATION, DISABLE_MAX_PRIVILEGE, GetTokenInformation, LUA_TOKEN,
        LUID_AND_ATTRIBUTES, LookupPrivilegeValueW, SE_PRIVILEGE_ENABLED, SID_AND_ATTRIBUTES,
        SUB_CONTAINERS_AND_OBJECTS_INHERIT, SetTokenInformation, TOKEN_ADJUST_DEFAULT,
        TOKEN_ADJUST_PRIVILEGES, TOKEN_ASSIGN_PRIMARY, TOKEN_DUPLICATE, TOKEN_PRIVILEGES,
        TOKEN_QUERY, TOKEN_USER, TokenDefaultDacl, TokenGroups, TokenUser, WRITE_RESTRICTED,
        WinWorldSid,
    },
    Storage::FileSystem::{
        CreateFileW, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS, FILE_GENERIC_WRITE,
        FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, GetFileAttributesW,
        INVALID_FILE_ATTRIBUTES, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, MoveFileExW,
        OPEN_EXISTING, WRITE_DAC,
    },
    System::{
        JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
            SetInformationJobObject, TerminateJobObject,
        },
        Threading::{
            CREATE_NEW_PROCESS_GROUP, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, CreateMutexW,
            CreateProcessAsUserW, DeleteProcThreadAttributeList, EXTENDED_STARTUPINFO_PRESENT,
            GetCurrentProcess, GetCurrentProcessId, GetExitCodeProcess, GetProcessTimes, INFINITE,
            InitializeProcThreadAttributeList, OpenProcess, OpenProcessToken,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST, PROCESS_INFORMATION,
            PROCESS_QUERY_LIMITED_INFORMATION, ReleaseMutex, ResumeThread, STARTF_USESTDHANDLES,
            STARTUPINFOEXW, UpdateProcThreadAttribute, WaitForSingleObject,
        },
    },
};

const FILE_GENERIC_READ: u32 = 0x00120089;
const FILE_GENERIC_EXECUTE: u32 = 0x001200A0;
const DELETE: u32 = 0x00010000;
const FILE_DELETE_CHILD: u32 = 0x40;
const STANDARD_RIGHTS_WRITE: u32 = 0x00020000;
const WORKSPACE_GRANT: u32 =
    (FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE | DELETE | FILE_DELETE_CHILD)
        & !STANDARD_RIGHTS_WRITE;
const GENERIC_ALL: u32 = 0x10000000;
const SE_GROUP_LOGON_ID: u32 = 0xC0000000;
const SYNCHRONIZE: u32 = 0x00100000;

pub(super) fn execute(
    program: &str,
    args: &[String],
    cwd: &Path,
    environment: &HashMap<String, String>,
    stdin: Option<Vec<u8>>,
    timeout: u64,
    locale_encoding: &str,
    spool: &Path,
    cancel_file: Option<&Path>,
    policy: WindowsPolicy,
) -> Result<(i32, String, String, bool, Option<Spool>, Option<Spool>), Box<dyn std::error::Error>> {
    let mut sandbox = Sandbox::new(&policy.workspace, &policy.private_tmp)?;
    let result = sandbox.run(
        program,
        args,
        cwd,
        environment,
        stdin,
        timeout,
        locale_encoding,
        spool,
        cancel_file,
    );
    let cleanup = sandbox.close();
    match (result, cleanup) {
        (Ok(v), Ok(())) => Ok(v),
        (Err(e), _) => Err(e),
        (Ok(_), Err(e)) => Err(Box::new(e)),
    }
}

struct Sandbox {
    token: HANDLE,
    leases: Vec<PathBuf>,
    job: HANDLE,
}

struct OwnedHandle(HANDLE);
impl OwnedHandle {
    fn into_raw(mut self) -> HANDLE {
        let raw = self.0;
        self.0 = null_mut();
        raw
    }
}
impl Drop for OwnedHandle {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}

struct OwnedSid(*mut core::ffi::c_void);
impl Drop for OwnedSid {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                LocalFree(self.0);
            }
        }
    }
}

impl Sandbox {
    fn new(workspace: &Path, private_tmp: &Path) -> Result<Self, Box<dyn std::error::Error>> {
        validate_workspace(workspace)?;
        cleanup_stale_leases()?;
        let nonce = uuid::Uuid::new_v4().to_string();
        let workspace_sid = capability_sid(workspace, false, &nonce);
        let temporary_sid = capability_sid(private_tmp, true, &nonce);
        let current_token = OwnedHandle(open_current_token(
            TOKEN_QUERY
                | TOKEN_DUPLICATE
                | TOKEN_ASSIGN_PRIMARY
                | TOKEN_ADJUST_PRIVILEGES
                | TOKEN_ADJUST_DEFAULT,
        )?);
        let groups = token_info(current_token.0, TokenGroups)?;
        let group_count = unsafe { *(groups.as_ptr() as *const u32) } as usize;
        let first = unsafe { groups.as_ptr().cast::<u8>().add(8) as *const SID_AND_ATTRIBUTES };
        let logon = (0..group_count)
            .find_map(|i| {
                let entry = unsafe { &*first.add(i) };
                (entry.Attributes & SE_GROUP_LOGON_ID as u32 == SE_GROUP_LOGON_ID as u32)
                    .then_some(entry.Sid)
            })
            .ok_or("the current access token has no logon SID")?;
        let mut world_buf = [0u8; 68];
        let mut world_size = world_buf.len() as u32;
        if unsafe {
            CreateWellKnownSid(
                WinWorldSid,
                null_mut(),
                world_buf.as_mut_ptr().cast(),
                &mut world_size,
            )
        } == 0
        {
            return Err(last_error("CreateWellKnownSid").into());
        }
        let ws_sid = OwnedSid(sid_from_string(&workspace_sid)?);
        let tmp_sid = OwnedSid(sid_from_string(&temporary_sid)?);
        let mut leases = vec![acquire_lease(workspace, &workspace_sid, false)?];
        match acquire_lease(private_tmp, &temporary_sid, true) {
            Ok(marker) => leases.push(marker),
            Err(error) => {
                for marker in leases.drain(..).rev() {
                    let _ = release_lease(&marker);
                }
                return Err(error.into());
            }
        }
        let restrict = [
            SID_AND_ATTRIBUTES {
                Sid: logon,
                Attributes: 0,
            },
            SID_AND_ATTRIBUTES {
                Sid: world_buf.as_mut_ptr().cast(),
                Attributes: 0,
            },
            SID_AND_ATTRIBUTES {
                Sid: ws_sid.0,
                Attributes: 0,
            },
            SID_AND_ATTRIBUTES {
                Sid: tmp_sid.0,
                Attributes: 0,
            },
        ];
        let mut token: HANDLE = null_mut();
        if unsafe {
            CreateRestrictedToken(
                current_token.0,
                DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED,
                0,
                null(),
                0,
                null(),
                restrict.len() as u32,
                restrict.as_ptr(),
                &mut token,
            )
        } == 0
        {
            let error = last_error("CreateRestrictedToken");
            for marker in leases.drain(..).rev() {
                let _ = release_lease(&marker);
            }
            return Err(error.into());
        }
        let token = OwnedHandle(token);
        let token_setup = (|| {
            let user_info = token_info(current_token.0, TokenUser)?;
            let user_sid = unsafe { (*(user_info.as_ptr() as *const TOKEN_USER)).User.Sid };
            set_default_dacl(
                token.0,
                &[
                    logon,
                    world_buf.as_mut_ptr().cast(),
                    ws_sid.0,
                    tmp_sid.0,
                    user_sid,
                ],
            )?;
            enable_privilege(token.0, "SeChangeNotifyPrivilege")
        })();
        if let Err(error) = token_setup {
            for marker in leases.drain(..).rev() {
                let _ = release_lease(&marker);
            }
            return Err(error.into());
        }
        drop(current_token);
        drop(ws_sid);
        drop(tmp_sid);
        let raw_job = unsafe { CreateJobObjectW(null(), null()) };
        if raw_job.is_null() {
            for marker in leases.drain(..).rev() {
                let _ = release_lease(&marker);
            }
            return Err(last_error("CreateJobObjectW").into());
        }
        let job = OwnedHandle(raw_job);
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if unsafe {
            SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                (&limits as *const _) as _,
                std::mem::size_of_val(&limits) as u32,
            )
        } == 0
        {
            for marker in leases.drain(..).rev() {
                let _ = release_lease(&marker);
            }
            return Err(last_error("SetInformationJobObject").into());
        }
        Ok(Self {
            token: token.into_raw(),
            leases,
            job: job.into_raw(),
        })
    }

    fn run(
        &mut self,
        program: &str,
        args: &[String],
        cwd: &Path,
        env: &HashMap<String, String>,
        stdin: Option<Vec<u8>>,
        timeout: u64,
        locale_encoding: &str,
        spool: &Path,
        cancel_file: Option<&Path>,
    ) -> Result<(i32, String, String, bool, Option<Spool>, Option<Spool>), Box<dyn std::error::Error>>
    {
        let input_path = spool.join(format!("nosis-shell-{}-stdin", uuid::Uuid::new_v4()));
        if let Some(data) = stdin {
            fs::write(&input_path, data)?;
        }
        let input = File::open(&input_path).or_else(|_| File::open("NUL"))?;
        let out_path = spool.join(format!("nosis-shell-{}-stdout", uuid::Uuid::new_v4()));
        let err_path = spool.join(format!("nosis-shell-{}-stderr", uuid::Uuid::new_v4()));
        let out = File::create(&out_path)?;
        let err = File::create(&err_path)?;
        let handles = [
            input.as_raw_handle() as HANDLE,
            out.as_raw_handle() as HANDLE,
            err.as_raw_handle() as HANDLE,
        ];
        for h in handles {
            set_inheritable(h)?;
        }
        let mut size = 0usize;
        unsafe {
            InitializeProcThreadAttributeList(null_mut(), 1, 0, &mut size);
        }
        let mut attr_mem = vec![0u128; (size + 15) / 16];
        let attr = attr_mem.as_mut_ptr().cast();
        if unsafe { InitializeProcThreadAttributeList(attr, 1, 0, &mut size) } == 0 {
            return Err(last_error("InitializeProcThreadAttributeList").into());
        }
        if unsafe {
            UpdateProcThreadAttribute(
                attr,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                handles.as_ptr().cast(),
                std::mem::size_of_val(&handles),
                null_mut(),
                null(),
            )
        } == 0
        {
            unsafe {
                DeleteProcThreadAttributeList(attr);
            }
            return Err(last_error("UpdateProcThreadAttribute").into());
        }
        let mut startup: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
        startup.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = handles[0];
        startup.StartupInfo.hStdOutput = handles[1];
        startup.StartupInfo.hStdError = handles[2];
        startup.lpAttributeList = attr;
        let env_block = environment_block(env);
        let mut command = command_line(program, args);
        let cwd = wide(cwd.as_os_str());
        let mut info: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
        let created = unsafe {
            CreateProcessAsUserW(
                self.token,
                null(),
                command.as_mut_ptr(),
                null(),
                null(),
                1,
                CREATE_NEW_PROCESS_GROUP
                    | CREATE_SUSPENDED
                    | CREATE_UNICODE_ENVIRONMENT
                    | EXTENDED_STARTUPINFO_PRESENT,
                env_block.as_ptr().cast(),
                cwd.as_ptr(),
                (&startup as *const STARTUPINFOEXW).cast(),
                &mut info,
            )
        };
        unsafe {
            DeleteProcThreadAttributeList(attr);
        }
        for h in handles {
            clear_inheritable(h);
        }
        if created == 0 {
            return Err(last_error("CreateProcessAsUserW").into());
        }
        if unsafe { AssignProcessToJobObject(self.job, info.hProcess) } == 0 {
            unsafe {
                TerminateJobObject(self.job, 1);
                CloseHandle(info.hThread);
                CloseHandle(info.hProcess);
            }
            return Err(last_error("AssignProcessToJobObject").into());
        }
        if unsafe { ResumeThread(info.hThread) } == u32::MAX {
            let error = last_error("ResumeThread");
            unsafe {
                TerminateJobObject(self.job, 1);
                CloseHandle(info.hThread);
                CloseHandle(info.hProcess);
            }
            return Err(error.into());
        }
        unsafe {
            CloseHandle(info.hThread);
        }
        let started = Instant::now();
        let mut timed_out = false;
        let mut cancelled = false;
        loop {
            let status = unsafe { WaitForSingleObject(info.hProcess, 50) };
            if status == WAIT_OBJECT_0 {
                break;
            }
            if status != WAIT_TIMEOUT {
                unsafe {
                    CloseHandle(info.hProcess);
                }
                return Err(last_error("WaitForSingleObject").into());
            }
            if cancel_file.is_some_and(Path::exists) {
                cancelled = true;
                unsafe {
                    TerminateJobObject(self.job, 1);
                    WaitForSingleObject(info.hProcess, INFINITE);
                }
                break;
            }
            if started.elapsed() >= Duration::from_secs(timeout) {
                timed_out = true;
                unsafe {
                    TerminateJobObject(self.job, 1);
                    WaitForSingleObject(info.hProcess, INFINITE);
                }
                break;
            }
        }
        let mut code = 1u32;
        let exit_read = unsafe { GetExitCodeProcess(info.hProcess, &mut code) };
        unsafe {
            CloseHandle(info.hProcess);
            CloseHandle(self.job);
        }
        self.job = null_mut();
        if exit_read == 0 {
            return Err(last_error("GetExitCodeProcess").into());
        }
        drop((input, out, err));
        if input_path.exists() {
            let _ = fs::remove_file(input_path);
        }
        let a = capture_existing(out_path, locale_encoding)?;
        let b = capture_existing(err_path, locale_encoding)?;
        if cancelled {
            if let Some(spool) = a.1 {
                let _ = fs::remove_file(spool.path);
            }
            if let Some(spool) = b.1 {
                let _ = fs::remove_file(spool.path);
            }
            return Err("command cancelled".into());
        }
        Ok((code as i32, a.0, b.0, timed_out, a.1, b.1))
    }

    fn close(&mut self) -> io::Result<()> {
        unsafe {
            CloseHandle(self.token);
            if !self.job.is_null() {
                CloseHandle(self.job);
            }
        }
        let mut failures = Vec::new();
        for marker in self.leases.drain(..).rev() {
            if let Err(error) = release_lease(&marker) {
                failures.push(error.to_string());
            }
        }
        if failures.is_empty() {
            Ok(())
        } else {
            Err(io::Error::other(format!(
                "Windows sandbox cleanup incomplete: {}",
                failures.join("; ")
            )))
        }
    }
}

fn capture_existing(path: PathBuf, locale_encoding: &str) -> io::Result<(String, Option<Spool>)> {
    super::output((path, 0), locale_encoding)
}
fn wide(value: &OsStr) -> Vec<u16> {
    value.encode_wide().chain(Some(0)).collect()
}
fn last_error(operation: &str) -> io::Error {
    io::Error::new(
        io::ErrorKind::Other,
        format!("{operation} failed with Windows error {}", unsafe {
            GetLastError()
        }),
    )
}
fn open_current_token(rights: u32) -> io::Result<HANDLE> {
    let mut token = null_mut();
    let process = unsafe { GetCurrentProcess() };
    if unsafe { OpenProcessToken(process, rights, &mut token) } == 0 {
        Err(last_error("OpenProcessToken"))
    } else {
        Ok(token)
    }
}
fn token_info(token: HANDLE, kind: i32) -> io::Result<Vec<u64>> {
    let mut n = 0;
    unsafe {
        GetTokenInformation(token, kind, null_mut(), 0, &mut n);
    }
    let mut b = vec![0u64; (n as usize + 7) / 8];
    if unsafe { GetTokenInformation(token, kind, b.as_mut_ptr().cast(), n, &mut n) } == 0 {
        Err(last_error("GetTokenInformation"))
    } else {
        Ok(b)
    }
}
fn sid_from_string(value: &str) -> io::Result<*mut core::ffi::c_void> {
    let v = wide(OsStr::new(value));
    let mut p = null_mut();
    if unsafe { ConvertStringSidToSidW(v.as_ptr(), &mut p) } == 0 {
        Err(last_error("ConvertStringSidToSidW"))
    } else {
        Ok(p)
    }
}
fn set_default_dacl(token: HANDLE, sids: &[*mut core::ffi::c_void]) -> io::Result<()> {
    let mut entries = sids
        .iter()
        .map(|sid| EXPLICIT_ACCESS_W {
            grfAccessPermissions: GENERIC_ALL,
            grfAccessMode: GRANT_ACCESS,
            grfInheritance: 0,
            Trustee: TRUSTEE_W {
                pMultipleTrustee: null_mut(),
                MultipleTrusteeOperation: 0,
                TrusteeForm: TRUSTEE_IS_SID,
                TrusteeType: TRUSTEE_IS_UNKNOWN,
                ptstrName: (*sid).cast(),
            },
        })
        .collect::<Vec<_>>();
    let mut acl = null_mut();
    if unsafe { SetEntriesInAclW(entries.len() as u32, entries.as_mut_ptr(), null(), &mut acl) }
        != 0
    {
        return Err(last_error("SetEntriesInAclW(TokenDefaultDacl)"));
    }
    #[repr(C)]
    struct DefaultDacl {
        acl: *mut core::ffi::c_void,
    }
    let info = DefaultDacl { acl: acl.cast() };
    let result = unsafe {
        SetTokenInformation(
            token,
            TokenDefaultDacl,
            (&info as *const DefaultDacl).cast(),
            std::mem::size_of_val(&info) as u32,
        )
    };
    unsafe {
        LocalFree(acl.cast());
    }
    if result == 0 {
        Err(last_error("SetTokenInformation(TokenDefaultDacl)"))
    } else {
        Ok(())
    }
}
fn enable_privilege(token: HANDLE, name: &str) -> io::Result<()> {
    let name = wide(OsStr::new(name));
    let mut luid: LUID = unsafe { std::mem::zeroed() };
    if unsafe { LookupPrivilegeValueW(null(), name.as_ptr(), &mut luid) } == 0 {
        return Err(last_error("LookupPrivilegeValueW"));
    }
    let privileges = TOKEN_PRIVILEGES {
        PrivilegeCount: 1,
        Privileges: [LUID_AND_ATTRIBUTES {
            Luid: luid,
            Attributes: SE_PRIVILEGE_ENABLED,
        }],
    };
    unsafe {
        SetLastError(0);
    }
    if unsafe { AdjustTokenPrivileges(token, 0, &privileges, 0, null_mut(), null_mut()) } == 0 {
        Err(last_error("AdjustTokenPrivileges"))
    } else if unsafe { GetLastError() } != 0 {
        Err(last_error("AdjustTokenPrivileges"))
    } else {
        Ok(())
    }
}
fn capability_sid(path: &Path, temp: bool, nonce: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(if temp {
        &b"temp\0"[..]
    } else {
        &b"workspace\0"[..]
    });
    h.update(path.to_string_lossy().to_lowercase().as_bytes());
    h.update(nonce.as_bytes());
    let x = h.finalize();
    let a = u32::from_le_bytes(x[0..4].try_into().unwrap()) % (2u32.pow(30) - 1) + 1;
    let b = u32::from_le_bytes(x[4..8].try_into().unwrap()) % (2u32.pow(30) - 1) + 1;
    format!("S-1-4-{a}-{b}{}", if temp { "-1" } else { "" })
}

#[derive(serde::Serialize, serde::Deserialize, Clone)]
struct LeaseRecord {
    path: String,
    sid: String,
    pid: u32,
    process_started: u64,
    managed: bool,
    released: bool,
    temporary: bool,
}

struct CapabilityMutex(HANDLE);
impl CapabilityMutex {
    fn acquire() -> io::Result<Self> {
        let name = wide(OsStr::new("Local\\NosisCapabilityLeases"));
        let handle = unsafe { CreateMutexW(null(), 0, name.as_ptr()) };
        if handle.is_null() {
            return Err(last_error("CreateMutexW"));
        }
        let status = unsafe { WaitForSingleObject(handle, INFINITE) };
        if status != WAIT_OBJECT_0 && status != WAIT_ABANDONED {
            unsafe {
                CloseHandle(handle);
            }
            return Err(last_error("WaitForSingleObject(CapabilityMutex)"));
        }
        Ok(Self(handle))
    }
}
impl Drop for CapabilityMutex {
    fn drop(&mut self) {
        unsafe {
            ReleaseMutex(self.0);
            CloseHandle(self.0);
        }
    }
}

fn lease_directory() -> PathBuf {
    std::env::temp_dir().join("nosis-capability-leases")
}
fn process_identity(pid: u32) -> io::Result<Option<u64>> {
    const ERROR_INVALID_PARAMETER: u32 = 87;
    let handle = unsafe { OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        let error = unsafe { GetLastError() };
        return if error == ERROR_INVALID_PARAMETER {
            Ok(None)
        } else {
            Err(io::Error::from_raw_os_error(error as i32))
        };
    }
    let status = unsafe { WaitForSingleObject(handle, 0) };
    if status == WAIT_OBJECT_0 {
        unsafe {
            CloseHandle(handle);
        }
        return Ok(None);
    }
    if status != WAIT_TIMEOUT {
        unsafe {
            CloseHandle(handle);
        }
        return Err(last_error("WaitForSingleObject(ProcessIdentity)"));
    }
    let mut created: FILETIME = unsafe { std::mem::zeroed() };
    let mut exited: FILETIME = unsafe { std::mem::zeroed() };
    let mut kernel: FILETIME = unsafe { std::mem::zeroed() };
    let mut user: FILETIME = unsafe { std::mem::zeroed() };
    let ok = unsafe { GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user) };
    unsafe {
        CloseHandle(handle);
    }
    if ok == 0 {
        return Err(last_error("GetProcessTimes"));
    }
    Ok(Some(
        ((created.dwHighDateTime as u64) << 32) | created.dwLowDateTime as u64,
    ))
}
fn current_process_identity() -> io::Result<u64> {
    process_identity(unsafe { GetCurrentProcessId() })?
        .ok_or_else(|| io::Error::other("cannot identify sandbox lease owner"))
}
fn marker_read(path: &Path) -> io::Result<LeaseRecord> {
    serde_json::from_slice(&fs::read(path)?).map_err(io::Error::other)
}
fn marker_write(path: &Path, record: &LeaseRecord) -> io::Result<()> {
    let pending = path.with_extension("tmp");
    let result = (|| {
        fs::write(
            &pending,
            serde_json::to_vec(record).map_err(io::Error::other)?,
        )?;
        let pending_wide = wide(pending.as_os_str());
        let path_wide = wide(path.as_os_str());
        if unsafe {
            MoveFileExW(
                pending_wide.as_ptr(),
                path_wide.as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
        } == 0
        {
            return Err(last_error("MoveFileExW(CapabilityLease)"));
        }
        Ok(())
    })();
    let _ = fs::remove_file(&pending);
    result
}
fn lease_active(marker: &Path, record: &LeaseRecord) -> io::Result<bool> {
    if record.released {
        return Ok(false);
    }
    const LEASE_TTL: u64 = 24 * 60 * 60;
    let age = marker
        .metadata()?
        .modified()?
        .elapsed()
        .unwrap_or_default()
        .as_secs();
    match process_identity(record.pid) {
        Ok(identity) => {
            if identity != Some(record.process_started) {
                return Ok(false);
            }
            Ok(true)
        }
        Err(_) => Ok(age < LEASE_TTL),
    }
}
fn cleanup_stale_leases() -> io::Result<()> {
    let _guard = CapabilityMutex::acquire()?;
    cleanup_stale_leases_locked()
}
fn cleanup_stale_leases_locked() -> io::Result<()> {
    let directory = lease_directory();
    fs::create_dir_all(&directory)?;
    let mut records = Vec::new();
    for entry in fs::read_dir(&directory)? {
        let marker = entry?.path();
        if marker.extension().and_then(|s| s.to_str()) != Some("lease") {
            continue;
        }
        match marker_read(&marker) {
            Ok(record) => records.push((marker, record)),
            Err(_) => {
                fs::remove_file(marker)?;
            }
        }
    }
    let mut groups: HashMap<(String, String), Vec<usize>> = HashMap::new();
    for (i, (_, record)) in records.iter().enumerate() {
        groups
            .entry((record.path.clone(), record.sid.clone()))
            .or_default()
            .push(i);
    }
    let mut stale_by_path: HashMap<String, Vec<usize>> = HashMap::new();
    for ((path, sid), indices) in groups {
        let mut stale = Vec::new();
        for index in &indices {
            if !lease_active(&records[*index].0, &records[*index].1)? {
                stale.push(*index);
            }
        }
        if stale.len() != indices.len() {
            continue;
        }
        let managed = indices.iter().any(|i| records[*i].1.managed);
        let path_buf = PathBuf::from(&path);
        if managed && path_buf.exists() {
            let sid_ptr = sid_from_string(&sid)?;
            let revoke_result = revoke(&path_buf, sid_ptr);
            unsafe {
                LocalFree(sid_ptr);
            }
            revoke_result?;
        }
        stale_by_path.entry(path).or_default().extend(stale);
    }
    for (path_value, indices) in stale_by_path {
        let path = PathBuf::from(&path_value);
        let any_active_for_path = records.iter().any(|(marker, record)| {
            record.path == path_value && lease_active(marker, record).unwrap_or(false)
        });
        let path_wide = wide(path.as_os_str());
        let attributes = unsafe { GetFileAttributesW(path_wide.as_ptr()) };
        let is_not_reparse =
            attributes != INVALID_FILE_ATTRIBUTES && attributes & FILE_ATTRIBUTE_REPARSE_POINT == 0;
        let should_remove_tmp = !any_active_for_path
            && indices.iter().all(|i| records[*i].1.temporary)
            && path.parent() == Some(dunce::canonicalize(std::env::temp_dir())?.as_path())
            && path
                .file_name()
                .and_then(|s| s.to_str())
                .is_some_and(|s| s.starts_with("nosis-sandbox-tmp-"))
            && is_not_reparse;
        if should_remove_tmp && path.exists() {
            fs::remove_dir_all(&path)?;
        }
        for index in indices {
            fs::remove_file(&records[index].0)?;
        }
    }
    Ok(())
}
fn acquire_lease(path: &Path, sid: &str, temporary: bool) -> io::Result<PathBuf> {
    let _guard = CapabilityMutex::acquire()?;
    cleanup_stale_leases_locked()?;
    let directory = lease_directory();
    fs::create_dir_all(&directory)?;
    let marker = directory.join(format!("{}.lease", uuid::Uuid::new_v4()));
    let record = LeaseRecord {
        path: dunce::canonicalize(path)?.to_string_lossy().to_lowercase(),
        sid: sid.to_owned(),
        pid: unsafe { GetCurrentProcessId() },
        process_started: current_process_identity()?,
        managed: true,
        released: false,
        temporary,
    };
    marker_write(&marker, &record)?;
    let sid_ptr = sid_from_string(sid)?;
    let result = grant(path, sid_ptr);
    unsafe {
        LocalFree(sid_ptr);
    }
    if let Err(error) = result {
        let _ = fs::remove_file(&marker);
        return Err(error);
    }
    Ok(marker)
}
fn release_lease(marker: &Path) -> io::Result<()> {
    let _guard = CapabilityMutex::acquire()?;
    if marker.exists() {
        let mut record = marker_read(marker)?;
        record.released = true;
        marker_write(marker, &record)?;
    }
    cleanup_stale_leases_locked()
}
fn validate_workspace(path: &Path) -> io::Result<()> {
    let p = wide(path.as_os_str());
    let h = unsafe {
        CreateFileW(
            p.as_ptr(),
            FILE_GENERIC_WRITE | WRITE_DAC,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            null(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            null_mut(),
        )
    };
    if h as isize == -1 {
        Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!(
                "Cannot prepare Windows workspace sandbox at '{}': current user must be able to write to the directory and change its permissions (WRITE_DAC). Select a directory you own and can modify instead of a protected directory or drive root. The command was not executed. Windows error: {}.",
                path.display(),
                unsafe { GetLastError() }
            ),
        ))
    } else {
        unsafe { CloseHandle(h) };
        Ok(())
    }
}
fn grant(path: &Path, sid: *mut core::ffi::c_void) -> io::Result<()> {
    acl_update(path, sid, true)
}
fn revoke(path: &Path, sid: *mut core::ffi::c_void) -> io::Result<()> {
    acl_update(path, sid, false)
}
fn acl_update(path: &Path, sid: *mut core::ffi::c_void, add: bool) -> io::Result<()> {
    let p = wide(path.as_os_str());
    let mut old = null_mut();
    let mut desc = null_mut();
    let r = unsafe {
        GetNamedSecurityInfoW(
            p.as_ptr(),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            null_mut(),
            null_mut(),
            &mut old,
            null_mut(),
            &mut desc,
        )
    };
    if r != 0 {
        return Err(io::Error::from_raw_os_error(r as i32));
    }
    let trustee = TRUSTEE_W {
        pMultipleTrustee: null_mut(),
        MultipleTrusteeOperation: 0,
        TrusteeForm: TRUSTEE_IS_SID,
        TrusteeType: TRUSTEE_IS_UNKNOWN,
        ptstrName: sid.cast(),
    };
    let entry = EXPLICIT_ACCESS_W {
        grfAccessPermissions: if add { WORKSPACE_GRANT } else { 0 },
        grfAccessMode: if add { GRANT_ACCESS } else { REVOKE_ACCESS },
        grfInheritance: SUB_CONTAINERS_AND_OBJECTS_INHERIT,
        Trustee: trustee,
    };
    let mut new = null_mut();
    let r = unsafe { SetEntriesInAclW(1, &entry, old, &mut new) };
    if r == 0 {
        let r = unsafe {
            SetNamedSecurityInfoW(
                p.as_ptr(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION,
                null_mut(),
                null_mut(),
                new,
                null_mut(),
            )
        };
        unsafe {
            LocalFree(new.cast());
            LocalFree(desc.cast());
        }
        if r == 0 {
            Ok(())
        } else {
            Err(io::Error::from_raw_os_error(r as i32))
        }
    } else {
        unsafe {
            LocalFree(desc.cast());
        }
        Err(io::Error::from_raw_os_error(r as i32))
    }
}
fn set_inheritable(h: HANDLE) -> io::Result<()> {
    use windows_sys::Win32::Foundation::{HANDLE_FLAG_INHERIT, SetHandleInformation};
    if unsafe { SetHandleInformation(h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) } == 0 {
        Err(last_error("SetHandleInformation"))
    } else {
        Ok(())
    }
}
fn clear_inheritable(h: HANDLE) {
    use windows_sys::Win32::Foundation::{HANDLE_FLAG_INHERIT, SetHandleInformation};
    unsafe {
        SetHandleInformation(h, HANDLE_FLAG_INHERIT, 0);
    }
}
fn environment_block(env: &HashMap<String, String>) -> Vec<u16> {
    let mut entries = env
        .iter()
        .map(|(k, v)| (k.to_uppercase(), format!("{k}={v}")))
        .collect::<Vec<_>>();
    entries.sort_by(|a, b| a.0.cmp(&b.0));
    let text = entries
        .into_iter()
        .map(|x| x.1)
        .collect::<Vec<_>>()
        .join("\0");
    text.encode_utf16().chain([0, 0]).collect()
}
fn command_line(program: &str, args: &[String]) -> Vec<u16> {
    let mut all = vec![program.to_owned()];
    all.extend(args.iter().cloned());
    let mut out = String::new();
    for (i, arg) in all.iter().enumerate() {
        if i > 0 {
            out.push(' ')
        }
        out.push_str(&quote_arg(arg));
    }
    out.encode_utf16().chain(Some(0)).collect()
}
fn quote_arg(arg: &str) -> String {
    if !arg.is_empty() && !arg.chars().any(|c| c == ' ' || c == '\t' || c == '"') {
        return arg.to_owned();
    }
    let mut out = String::from("\"");
    let mut slashes = 0;
    for c in arg.chars() {
        if c == '\\' {
            slashes += 1
        } else if c == '"' {
            out.push_str(&"\\".repeat(slashes * 2 + 1));
            out.push('"');
            slashes = 0
        } else {
            out.push_str(&"\\".repeat(slashes));
            slashes = 0;
            out.push(c)
        }
    }
    out.push_str(&"\\".repeat(slashes * 2));
    out.push('"');
    out
}

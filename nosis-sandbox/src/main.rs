use base64::{Engine, engine::general_purpose::STANDARD};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::{
    collections::HashMap,
    fs::{self, File},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

const OUTPUT_LIMIT: usize = 50 * 1024;

#[derive(Deserialize)]
struct Request {
    command: String,
    argv: Vec<String>,
    cwd: PathBuf,
    environment: HashMap<String, String>,
    stdin_base64: Option<String>,
    timeout_seconds: u64,
    spool_directory: PathBuf,
    cancel_file: Option<PathBuf>,
    policy: Policy,
}

#[derive(Deserialize)]
#[serde(tag = "platform", rename_all = "snake_case")]
#[allow(dead_code)]
enum Policy {
    Linux {
        workspace: PathBuf,
        read_only_paths: Vec<PathBuf>,
    },
    Macos {
        workspace: PathBuf,
        private_tmp: PathBuf,
        read_only_paths: Vec<PathBuf>,
    },
    Windows {
        workspace: PathBuf,
        private_tmp: Option<PathBuf>,
    },
    Host,
}

#[derive(Serialize)]
struct ResultPayload {
    command: String,
    exit_code: i32,
    stdout: String,
    stderr: String,
    timed_out: bool,
    timeout_seconds: u64,
    stdout_spool: Option<Spool>,
    stderr_spool: Option<Spool>,
}

#[derive(Serialize)]
struct Spool {
    path: PathBuf,
    size_chars: usize,
    encoding: &'static str,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(2);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let request: Request = serde_json::from_str(&input)?;
    if request.argv.is_empty() || request.timeout_seconds == 0 {
        return Err("invalid sandbox request".into());
    }
    fs::create_dir_all(&request.spool_directory)?;
    reap_stale_spools(&request.spool_directory);
    let (program, args, cwd, env, windows_policy) = prepare(&request)?;
    let stdin = request
        .stdin_base64
        .as_deref()
        .map(|value| STANDARD.decode(value))
        .transpose()?;
    let output = execute(
        &program,
        &args,
        &cwd,
        &env,
        stdin,
        request.timeout_seconds,
        &request.spool_directory,
        request.cancel_file.as_deref(),
        windows_policy,
    )?;
    let payload = ResultPayload {
        command: request.command,
        exit_code: output.0,
        stdout: output.1,
        stderr: output.2,
        timed_out: output.3,
        timeout_seconds: request.timeout_seconds,
        stdout_spool: output.4,
        stderr_spool: output.5,
    };
    serde_json::to_writer(io::stdout().lock(), &payload)?;
    Ok(())
}

fn reap_stale_spools(directory: &Path) {
    let Ok(entries) = fs::read_dir(directory) else {
        return;
    };
    let cutoff = std::time::SystemTime::now().checked_sub(Duration::from_secs(24 * 60 * 60));
    let Some(cutoff) = cutoff else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if !entry
            .file_name()
            .to_string_lossy()
            .starts_with("nosis-shell-")
        {
            continue;
        }
        let Ok(metadata) = fs::symlink_metadata(&path) else {
            continue;
        };
        if metadata.file_type().is_file()
            && metadata.modified().is_ok_and(|modified| modified < cutoff)
        {
            let _ = fs::remove_file(path);
        }
    }
}

#[cfg(target_os = "linux")]
fn prepare(
    request: &Request,
) -> Result<
    (
        String,
        Vec<String>,
        PathBuf,
        HashMap<String, String>,
        Option<WindowsPolicy>,
    ),
    Box<dyn std::error::Error>,
> {
    match &request.policy {
        Policy::Host => Ok((
            request.argv[0].clone(),
            request.argv[1..].to_vec(),
            request.cwd.clone(),
            request.environment.clone(),
            None,
        )),
        Policy::Linux {
            workspace,
            read_only_paths,
        } => {
            let bwrap = which("bwrap").ok_or("workspace shell requires bubblewrap (bwrap) on Linux; install bwrap and enable unprivileged user namespaces")?;
            let workspace = workspace.canonicalize()?;
            let mut args = vec![
                "--die-with-parent",
                "--unshare-user",
                "--unshare-pid",
                "--unshare-net",
                "--unshare-ipc",
                "--unshare-uts",
                "--unshare-cgroup-try",
                "--cap-drop",
                "ALL",
                "--new-session",
                "--tmpfs",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
            ]
            .into_iter()
            .map(str::to_owned)
            .collect::<Vec<_>>();
            for path in ["/usr", "/bin", "/sbin", "/lib", "/lib64"] {
                if Path::new(path).exists() {
                    args.extend(["--ro-bind".into(), path.into(), path.into()]);
                }
            }
            args.extend(["--tmpfs".into(), "/tmp".into()]);
            append_parents(&mut args, &workspace);
            args.extend([
                "--bind".into(),
                workspace.to_string_lossy().into(),
                workspace.to_string_lossy().into(),
            ]);
            for value in read_only_paths {
                let path = value.canonicalize()?;
                append_parents(&mut args, &path);
                args.extend([
                    "--ro-bind".into(),
                    path.to_string_lossy().into(),
                    path.to_string_lossy().into(),
                ]);
            }
            args.extend(["--chdir".into(), workspace.to_string_lossy().into()]);
            args.extend(request.argv.clone());
            let mut env = request.environment.clone();
            for key in ["HOME", "TMPDIR", "TMP", "TEMP"] {
                env.insert(key.into(), "/tmp".into());
            }
            Ok((bwrap, args, workspace, env, None))
        }
        _ => Err("sandbox policy does not match the current platform".into()),
    }
}

#[cfg(target_os = "macos")]
fn prepare(
    request: &Request,
) -> Result<
    (
        String,
        Vec<String>,
        PathBuf,
        HashMap<String, String>,
        Option<WindowsPolicy>,
    ),
    Box<dyn std::error::Error>,
> {
    match &request.policy {
        Policy::Host => Ok((
            request.argv[0].clone(),
            request.argv[1..].to_vec(),
            request.cwd.clone(),
            request.environment.clone(),
            None,
        )),
        Policy::Macos {
            workspace,
            private_tmp,
            read_only_paths,
        } => {
            let workspace = workspace.canonicalize()?;
            let private_tmp = private_tmp.canonicalize()?;
            let mut profile = format!(
                "(version 1)\n(deny default)\n(import \"system.sb\")\n(deny network*)\n(allow process-exec)\n(allow process-fork)\n(allow signal (target same-sandbox))\n(allow process-info* (target same-sandbox))\n(allow file-read* (subpath (param \"WORKSPACE\")) (subpath (param \"PRIVATE_TMP\")) (subpath \"/bin\") (subpath \"/usr/bin\") (subpath \"/usr/lib\") (subpath \"/usr/share\") (subpath \"/System\") (subpath \"/Library/Apple\") (literal \"/private/var/select/sh\") (literal \"/dev/null\") (literal \"/dev/urandom\"))\n(allow file-write* (subpath (param \"WORKSPACE\")) (subpath (param \"PRIVATE_TMP\")) (literal \"/dev/null\"))\n"
            );
            for path in std::iter::once(&workspace)
                .chain(std::iter::once(&private_tmp))
                .chain(read_only_paths.iter())
            {
                for parent in path.ancestors().filter(|p| *p != Path::new("/")) {
                    profile.push_str(&format!(
                        "(allow file-read-metadata file-test-existence (literal \"{}\"))\n",
                        escape_profile(&parent.to_string_lossy())
                    ));
                }
            }
            for path in read_only_paths {
                profile.push_str(&format!(
                    "(allow file-read* (subpath \"{}\"))\n",
                    escape_profile(&path.canonicalize()?.to_string_lossy())
                ));
            }
            let mut args = vec![
                "-D".into(),
                format!("WORKSPACE={}", workspace.display()),
                "-D".into(),
                format!("PRIVATE_TMP={}", private_tmp.display()),
                "-p".into(),
                profile,
            ];
            args.extend(request.argv.clone());
            let mut env = request.environment.clone();
            for key in ["HOME", "TMPDIR", "TMP", "TEMP"] {
                env.insert(key.into(), private_tmp.to_string_lossy().into());
            }
            Ok(("/usr/bin/sandbox-exec".into(), args, workspace, env, None))
        }
        _ => Err("sandbox policy does not match the current platform".into()),
    }
}

#[cfg(windows)]
fn prepare(
    request: &Request,
) -> Result<
    (
        String,
        Vec<String>,
        PathBuf,
        HashMap<String, String>,
        Option<WindowsPolicy>,
    ),
    Box<dyn std::error::Error>,
> {
    match &request.policy {
        Policy::Host => Ok((
            request.argv[0].clone(),
            request.argv[1..].to_vec(),
            request.cwd.clone(),
            request.environment.clone(),
            None,
        )),
        Policy::Windows {
            workspace,
            private_tmp,
        } => {
            let workspace = dunce::canonicalize(workspace)?;
            let (private_tmp, private_tmp_owner) = match private_tmp {
                Some(path) => (dunce::canonicalize(path)?, None),
                None => {
                    let owner = tempfile::Builder::new()
                        .prefix("nosis-sandbox-tmp-")
                        .tempdir()?;
                    (dunce::canonicalize(owner.path())?, Some(owner))
                }
            };
            let mut env = request.environment.clone();
            for key in ["HOME", "TMPDIR", "TMP", "TEMP"] {
                env.insert(key.into(), private_tmp.to_string_lossy().into());
            }
            Ok((
                request.argv[0].clone(),
                request.argv[1..].to_vec(),
                workspace.clone(),
                env,
                Some(WindowsPolicy {
                    workspace,
                    private_tmp,
                    _private_tmp_owner: private_tmp_owner,
                }),
            ))
        }
        _ => Err("sandbox policy does not match the current platform".into()),
    }
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
fn prepare(
    _: &Request,
) -> Result<
    (
        String,
        Vec<String>,
        PathBuf,
        HashMap<String, String>,
        Option<WindowsPolicy>,
    ),
    Box<dyn std::error::Error>,
> {
    Err("unsupported platform".into())
}

#[cfg(target_os = "linux")]
fn append_parents(args: &mut Vec<String>, path: &Path) {
    for parent in path.ancestors().collect::<Vec<_>>().into_iter().rev() {
        if parent != Path::new("/") {
            args.extend(["--dir".into(), parent.to_string_lossy().into()]);
        }
    }
}
#[cfg(target_os = "macos")]
fn escape_profile(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}
#[cfg(target_os = "linux")]
fn which(name: &str) -> Option<String> {
    std::env::var_os("PATH")
        .and_then(|paths| {
            std::env::split_paths(&paths)
                .map(|p| p.join(name))
                .find(|p| p.is_file())
        })
        .map(|p| p.to_string_lossy().into())
}

#[cfg(windows)]
struct WindowsPolicy {
    workspace: PathBuf,
    private_tmp: PathBuf,
    _private_tmp_owner: Option<tempfile::TempDir>,
}
#[cfg(not(windows))]
type WindowsPolicy = ();

fn execute(
    program: &str,
    args: &[String],
    cwd: &Path,
    env: &HashMap<String, String>,
    stdin: Option<Vec<u8>>,
    timeout: u64,
    spool_directory: &Path,
    cancel_file: Option<&Path>,
    windows_policy: Option<WindowsPolicy>,
) -> Result<(i32, String, String, bool, Option<Spool>, Option<Spool>), Box<dyn std::error::Error>> {
    #[cfg(windows)]
    if let Some(policy) = windows_policy {
        return windows::execute(
            program,
            args,
            cwd,
            env,
            stdin,
            timeout,
            spool_directory,
            cancel_file,
            policy,
        );
    }
    #[cfg(not(windows))]
    let _ = windows_policy;
    let mut command = Command::new(program);
    command
        .args(args)
        .current_dir(cwd)
        .env_clear()
        .envs(env)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn()?;
    if let Some(input) = stdin {
        if let Some(mut pipe) = child.stdin.take() {
            thread::spawn(move || {
                let _ = pipe.write_all(&input);
            });
        }
    } else {
        drop(child.stdin.take());
    }
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    let out_path = spool_directory.join(format!("nosis-shell-{}-stdout", uuid::Uuid::new_v4()));
    let err_path = spool_directory.join(format!("nosis-shell-{}-stderr", uuid::Uuid::new_v4()));
    let out_thread = thread::spawn(move || capture(stdout, &out_path));
    let err_thread = thread::spawn(move || capture(stderr, &err_path));
    let deadline = Instant::now() + Duration::from_secs(timeout);
    let mut timed_out = false;
    let mut cancelled = false;
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if cancel_file.is_some_and(Path::exists) {
            cancelled = true;
            kill_tree(&mut child);
            break child.wait()?;
        }
        if Instant::now() >= deadline {
            timed_out = true;
            kill_tree(&mut child);
            break child.wait()?;
        }
        thread::sleep(Duration::from_millis(50));
    };
    let out = out_thread.join().map_err(|_| "stdout capture failed")??;
    let err = err_thread.join().map_err(|_| "stderr capture failed")??;
    let (stdout, stdout_spool) = output(out)?;
    let (stderr, stderr_spool) = output(err)?;
    if cancelled {
        if let Some(spool) = stdout_spool {
            let _ = fs::remove_file(spool.path);
        }
        if let Some(spool) = stderr_spool {
            let _ = fs::remove_file(spool.path);
        }
        return Err("command cancelled".into());
    }
    Ok((
        status.code().unwrap_or(1),
        stdout,
        stderr,
        timed_out,
        stdout_spool,
        stderr_spool,
    ))
}

fn capture<R: Read>(mut stream: R, path: &Path) -> io::Result<(PathBuf, usize)> {
    let mut file = File::create(path)?;
    let mut buffer = [0u8; 8192];
    let mut size = 0;
    loop {
        let n = stream.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        size += n;
        file.write_all(&buffer[..n])?;
    }
    Ok((path.to_path_buf(), size))
}
fn output((path, _bytes): (PathBuf, usize)) -> io::Result<(String, Option<Spool>)> {
    let mut file = File::open(&path)?;
    let mut decoder = encoding_rs::UTF_8.new_decoder();
    let mut bytes = [0u8; 8192];
    let mut pending: Vec<u8> = Vec::new();
    let mut decoded = String::new();
    let mut head = Vec::with_capacity(OUTPUT_LIMIT);
    let mut tail = VecDeque::with_capacity(OUTPUT_LIMIT);
    let mut size_chars = 0;
    loop {
        let read = file.read(&mut bytes)?;
        let last = read == 0;
        // A chunk can end inside a multi-byte sequence, so the decoder keeps
        // the bytes it did not consume for the next round.
        pending.extend_from_slice(&bytes[..read]);
        decoded.reserve(pending.len().saturating_mul(3).max(16));
        let (status, consumed, _) = decoder.decode_to_string(&pending, &mut decoded, last);
        if matches!(status, encoding_rs::CoderResult::OutputFull) {
            return Err(io::Error::other("sandbox output decoder buffer exhausted"));
        }
        pending.drain(..consumed);
        for ch in decoded.drain(..) {
            size_chars += 1;
            if head.len() < OUTPUT_LIMIT {
                head.push(ch);
            }
            if tail.len() == OUTPUT_LIMIT {
                tail.pop_front();
            }
            tail.push_back(ch);
        }
        if last {
            break;
        }
    }
    if size_chars <= OUTPUT_LIMIT {
        fs::remove_file(path)?;
        return Ok((head.into_iter().collect(), None));
    }
    let mut kept = OUTPUT_LIMIT;
    let preview = loop {
        let head_count = (kept + 1) / 2;
        let tail_count = kept / 2;
        let marker = format!("\n... [truncated {} characters] ...\n", size_chars - kept);
        let mut rendered = head.iter().take(head_count).collect::<String>();
        rendered.push_str(&marker);
        rendered.extend(tail.iter().skip(OUTPUT_LIMIT - tail_count));
        let length = rendered.chars().count();
        if length <= OUTPUT_LIMIT {
            break rendered;
        }
        kept = kept.saturating_sub(length - OUTPUT_LIMIT);
    };
    Ok((
        preview,
        Some(Spool {
            path,
            size_chars,
            encoding: "utf-8",
        }),
    ))
}

#[cfg(unix)]
fn kill_tree(child: &mut std::process::Child) {
    unsafe {
        libc_killpg(child.id() as i32);
    }
    let _ = child.kill();
}
#[cfg(unix)]
unsafe fn libc_killpg(pid: i32) {
    unsafe extern "C" {
        fn killpg(pgrp: i32, sig: i32) -> i32;
    }
    let _ = killpg(pid, 9);
}
#[cfg(windows)]
fn kill_tree(child: &mut std::process::Child) {
    let _ = Command::new("taskkill")
        .args(["/F", "/T", "/PID", &child.id().to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    let _ = child.kill();
}
#[cfg(not(any(unix, windows)))]
fn kill_tree(child: &mut std::process::Child) {
    let _ = child.kill();
}

#[cfg(windows)]
mod windows;

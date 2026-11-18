#![cfg(windows)]

use serde_json::{Value, json};
use std::{
    collections::HashMap,
    fs,
    io::{Read, Write},
    net::TcpListener,
    path::Path,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
use tempfile::TempDir;

fn powershell() -> String {
    std::env::var("WINDIR").unwrap() + r"\System32\WindowsPowerShell\v1.0\powershell.exe"
}

fn request(workspace: &Path, private_tmp: &Path, script: &str) -> Value {
    request_timeout(workspace, private_tmp, script, 20)
}

fn request_timeout(workspace: &Path, private_tmp: &Path, script: &str, timeout: u64) -> Value {
    let mut environment = HashMap::new();
    environment.insert("PATH", std::env::var("PATH").unwrap_or_default());
    environment.insert("SystemRoot", std::env::var("SystemRoot").unwrap());
    environment.insert("TEMP", private_tmp.to_string_lossy().into_owned());
    environment.insert("TMP", private_tmp.to_string_lossy().into_owned());
    json!({
        "protocol_version": 1,
        "command": script,
        "argv": [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        "cwd": workspace,
        "environment": environment,
        "locale_encoding": "utf-8",
        "stdin_base64": null,
        "timeout_seconds": timeout,
        "spool_directory": std::env::temp_dir(),
        "policy": {"platform":"windows", "workspace":workspace, "private_tmp":private_tmp}
    })
}

fn run(request: Value) -> Value {
    finish(spawn(request))
}

fn spawn(request: Value) -> std::process::Child {
    let mut child = Command::new(env!("CARGO_BIN_EXE_nosis-sandbox"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(format!("{request}\n").as_bytes())
        .unwrap();
    child
}

fn finish(mut child: std::process::Child) -> Value {
    let _control = child.stdin.take().unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(output.stdout.splitn(2, |b| *b == b'\n').nth(1).unwrap()).unwrap()
}

fn acl(path: &Path) -> Vec<u8> {
    Command::new("icacls").arg(path).output().unwrap().stdout
}

fn sandbox_dirs() -> (TempDir, TempDir) {
    let workspace = tempfile::Builder::new()
        .prefix("nosis-workspace-sandbox-")
        .tempdir()
        .unwrap();
    let private = tempfile::Builder::new()
        .prefix("nosis-sandbox-tmp-")
        .tempdir()
        .unwrap();
    (workspace, private)
}

#[test]
fn workspace_and_private_temp_are_writable_and_acl_is_restored() {
    let (workspace, private) = sandbox_dirs();
    let workspace_acl_before = acl(workspace.path());
    let script = "Set-Content -LiteralPath inside.txt -Value workspace; Set-Content -LiteralPath (Join-Path $env:TEMP temp.txt) -Value temporary";
    let result = run(request(workspace.path(), private.path(), script));
    assert_eq!(result["exit_code"], 0, "{}", result["stderr"]);
    assert_eq!(
        fs::read_to_string(workspace.path().join("inside.txt"))
            .unwrap()
            .trim(),
        "workspace"
    );
    assert_eq!(
        fs::read_to_string(private.path().join("temp.txt"))
            .unwrap()
            .trim(),
        "temporary"
    );
    let workspace_acl_after = acl(workspace.path());
    assert_eq!(workspace_acl_after, workspace_acl_before);
}

#[test]
fn control_pipe_cancellation_restores_acl_without_another_helper() {
    for disconnect in [false, true] {
        let (workspace, private) = sandbox_dirs();
        let workspace_before = acl(workspace.path());
        let private_before = acl(private.path());
        let mut helper = spawn(request(
            workspace.path(),
            private.path(),
            "Set-Content -LiteralPath ready -Value yes; Start-Sleep -Seconds 20; Set-Content -LiteralPath survived -Value yes",
        ));
        let mut control = Some(helper.stdin.take().unwrap());
        let deadline = Instant::now() + Duration::from_secs(5);
        while !workspace.path().join("ready").exists() {
            assert!(Instant::now() < deadline, "command did not start");
            thread::sleep(Duration::from_millis(10));
        }
        if disconnect {
            drop(control.take());
        } else {
            control.as_mut().unwrap().write_all(&[0]).unwrap();
            // Keep the pipe open: the notification itself must cancel.
        }
        let output = helper.wait_with_output().unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("command cancelled"));
        assert_eq!(acl(workspace.path()), workspace_before);
        assert_eq!(acl(private.path()), private_before);
        assert!(!workspace.path().join("survived").exists());
    }
}

#[test]
fn concurrent_commands_keep_each_others_workspace_capability() {
    let (workspace, private) = sandbox_dirs();
    let before = acl(workspace.path());
    let first = spawn(request(
        workspace.path(),
        private.path(),
        "Start-Sleep -Milliseconds 400; Set-Content -LiteralPath first.txt -Value first",
    ));
    let second_private = tempfile::Builder::new()
        .prefix("nosis-sandbox-tmp-")
        .tempdir()
        .unwrap();
    let second = spawn(request(
        workspace.path(),
        second_private.path(),
        "Start-Sleep -Milliseconds 800; Set-Content -LiteralPath second.txt -Value second",
    ));
    assert_eq!(finish(first)["exit_code"], 0);
    assert_eq!(finish(second)["exit_code"], 0);
    assert_eq!(
        fs::read_to_string(workspace.path().join("first.txt"))
            .unwrap()
            .trim(),
        "first"
    );
    assert_eq!(
        fs::read_to_string(workspace.path().join("second.txt"))
            .unwrap()
            .trim(),
        "second"
    );
    assert_eq!(acl(workspace.path()), before);
}

#[test]
fn abandoned_helper_lease_is_reclaimed_on_next_startup() {
    let (workspace, private) = sandbox_dirs();
    let before = acl(workspace.path());
    let helper = spawn(request(
        workspace.path(),
        private.path(),
        "Start-Sleep -Seconds 30",
    ));
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline
        && !String::from_utf8_lossy(&acl(workspace.path())).contains("S-1-4-")
    {
        thread::sleep(Duration::from_millis(50));
    }
    assert!(String::from_utf8_lossy(&acl(workspace.path())).contains("S-1-4-"));
    let mut helper = helper;
    helper.kill().unwrap();
    let _ = helper.wait();
    let recovery = run(request(workspace.path(), private.path(), "$null = 1"));
    assert_eq!(recovery["exit_code"], 0, "{}", recovery["stderr"]);
    assert_eq!(acl(workspace.path()), before);
}

#[test]
fn host_files_remain_readable_but_host_writes_are_denied() {
    let (workspace, private) = sandbox_dirs();
    let outside = tempfile::Builder::new()
        .prefix("nosis-host-sandbox-probe-")
        .tempdir()
        .unwrap();
    let file = outside.path().join("host.txt");
    fs::write(&file, "host-readable").unwrap();
    let read_script = format!("Get-Content -Raw -LiteralPath '{}'", file.display());
    let read_result = run(request(workspace.path(), private.path(), &read_script));
    assert_eq!(read_result["exit_code"], 0, "{}", read_result["stderr"]);
    assert_eq!(
        read_result["stdout"].as_str().unwrap().trim(),
        "host-readable"
    );
    let write_script = format!(
        "$ErrorActionPreference='Stop'; Set-Content -LiteralPath '{}' -Value denied",
        file.display()
    );
    let write_result = run(request(workspace.path(), private.path(), &write_script));
    assert_ne!(write_result["exit_code"], 0);
    assert_eq!(fs::read_to_string(file).unwrap(), "host-readable");
}

#[test]
fn network_access_remains_available() {
    let (workspace, private) = sandbox_dirs();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = [0; 1024];
        let _ = stream.read(&mut request).unwrap();
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nnosis")
            .unwrap();
    });
    let script = format!("(Invoke-WebRequest -UseBasicParsing -Uri 'http://{address}/').Content");
    let result = run(request(workspace.path(), private.path(), &script));
    server.join().unwrap();
    assert_eq!(result["exit_code"], 0, "{}", result["stderr"]);
    assert!(!result["stdout"].as_str().unwrap().is_empty());
}

#[test]
fn timeout_terminates_the_entire_job_object_tree() {
    let (workspace, private) = sandbox_dirs();
    let marker = workspace
        .path()
        .parent()
        .unwrap()
        .join("child-survived.txt");
    let script = format!(
        "$child = \"Start-Sleep -Seconds 2; Set-Content -LiteralPath '{}' -Value survived\"; Start-Process -FilePath '{}' -ArgumentList @('-NoProfile','-Command',$child); Start-Sleep -Seconds 30",
        marker.display(),
        powershell()
    );
    let result = run(request_timeout(
        workspace.path(),
        private.path(),
        &script,
        1,
    ));
    assert!(result["timed_out"].as_bool().unwrap());
    thread::sleep(Duration::from_secs(3));
    assert!(!marker.exists());
}

#[test]
fn everyone_writable_host_paths_keep_the_windows_restricted_token_exception() {
    let (workspace, private) = sandbox_dirs();
    let public = tempfile::Builder::new()
        .prefix("nosis-world-writable-")
        .tempdir()
        .unwrap();
    let acl = Command::new("icacls")
        .arg(public.path())
        .args(["/grant", "*S-1-1-0:(OI)(CI)M"])
        .output()
        .unwrap();
    assert!(
        acl.status.success(),
        "{}",
        String::from_utf8_lossy(&acl.stderr)
    );
    let target = public.path().join("allowed.txt");
    let script = format!(
        "Set-Content -LiteralPath '{}' -Value allowed",
        target.display()
    );
    let result = run(request(workspace.path(), private.path(), &script));
    assert_eq!(result["exit_code"], 0, "{}", result["stderr"]);
    assert_eq!(fs::read_to_string(target).unwrap().trim(), "allowed");
}

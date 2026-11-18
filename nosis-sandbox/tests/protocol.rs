use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Command, Stdio},
};
use tempfile::tempdir;

fn argv(script: &str) -> Vec<String> {
    #[cfg(windows)]
    {
        vec![
            std::env::var("WINDIR").unwrap() + r"\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoLogo".into(),
            "-NoProfile".into(),
            "-NonInteractive".into(),
            "-Command".into(),
            script.into(),
        ]
    }
    #[cfg(not(windows))]
    {
        vec!["/bin/sh".into(), "-c".into(), script.into()]
    }
}

fn request(workspace: &Path, script: &str) -> Value {
    json!({
        "protocol_version": 1,
        "command": script,
        "argv": argv(script),
        "cwd": workspace,
        "environment": std::env::vars().collect::<std::collections::HashMap<_,_>>(),
        "locale_encoding": "gbk",
        "stdin_base64": null,
        "timeout_seconds": 10,
        "spool_directory": std::env::temp_dir(),
        "policy": {"platform":"host"}
    })
}

fn run(request: Value) -> Value {
    let mut helper = Command::new(env!("CARGO_BIN_EXE_nosis-sandbox"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut control = helper.stdin.take().unwrap();
    writeln!(control, "{request}").unwrap();
    let output = helper.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(output.stdout.splitn(2, |b| *b == b'\n').nth(1).unwrap()).unwrap()
}

#[test]
fn handshake_precedes_request_and_rejects_missing_or_wrong_version() {
    for version in [None, Some(999)] {
        let mut helper = Command::new(env!("CARGO_BIN_EXE_nosis-sandbox"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut output = BufReader::new(helper.stdout.take().unwrap());
        let mut greeting = String::new();
        output.read_line(&mut greeting).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&greeting).unwrap()["protocol_version"],
            1
        );
        // Other required fields are deliberately absent: version validation
        // must happen before schema decoding, platform setup or execution.
        let input = match version {
            Some(version) => json!({"protocol_version": version}),
            None => json!({}),
        };
        writeln!(helper.stdin.as_mut().unwrap(), "{input}").unwrap();
        let result = helper.wait_with_output().unwrap();
        assert!(!result.status.success());
        let error = String::from_utf8_lossy(&result.stderr);
        assert!(error.contains("protocol mismatch"), "{error}");
        assert!(error.contains("cargo build --release"), "{error}");
    }
}

#[test]
fn closing_control_pipe_cancels_a_running_command() {
    let workspace = tempdir().unwrap();
    #[cfg(windows)]
    let script = "[IO.File]::WriteAllText('ready', 'yes'); Start-Sleep 20; [IO.File]::WriteAllText('finished', 'yes')";
    #[cfg(not(windows))]
    let script = "touch ready; sleep 20; touch finished";
    let mut helper = Command::new(env!("CARGO_BIN_EXE_nosis-sandbox"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut control = helper.stdin.take().unwrap();
    writeln!(control, "{}", request(workspace.path(), script)).unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while !workspace.path().join("ready").exists() {
        assert!(
            std::time::Instant::now() < deadline,
            "command did not start"
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let cancelled_at = std::time::Instant::now();
    drop(control);
    let output = helper.wait_with_output().unwrap();
    assert!(cancelled_at.elapsed() < std::time::Duration::from_secs(3));
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("command cancelled"));
    assert!(!workspace.path().join("finished").exists());
}

#[test]
fn multibyte_output_survives_chunk_boundaries() {
    let workspace = tempdir().unwrap();
    let expected = format!("{}{}", "a".repeat(8191), "中".repeat(2000));
    std::fs::write(workspace.path().join("payload.txt"), &expected).unwrap();
    #[cfg(windows)]
    let script = "$b = [IO.File]::ReadAllBytes('payload.txt'); [Console]::OpenStandardOutput().Write($b, 0, $b.Length)";
    #[cfg(not(windows))]
    let script = "cat payload.txt";
    let result = run(request(workspace.path(), script));
    assert_eq!(result["exit_code"], 0, "{}", result["stderr"]);
    let stdout = result["stdout"].as_str().unwrap();
    assert!(!stdout.contains('\u{FFFD}'));
    assert_eq!(stdout, expected);
    assert_eq!(result["stdout_spool"], Value::Null);
}

#[test]
fn oversized_streams_keep_bounded_previews_and_complete_spools() {
    let workspace = tempdir().unwrap();
    #[cfg(windows)]
    let script = "[Console]::Out.Write(('x' * 60000)); [Console]::Error.Write(('y' * 60000))";
    #[cfg(not(windows))]
    let script = "head -c 60000 /dev/zero | tr '\\0' x; head -c 60000 /dev/zero | tr '\\0' y >&2";
    let result = run(request(workspace.path(), script));
    assert_eq!(result["exit_code"], 0, "{}", result["stderr"]);
    assert_eq!(
        result["stdout"].as_str().unwrap().chars().count(),
        50 * 1024
    );
    assert_eq!(
        result["stderr"].as_str().unwrap().chars().count(),
        50 * 1024
    );
    for key in ["stdout_spool", "stderr_spool"] {
        let spool = &result[key];
        assert_eq!(spool["size_chars"], 60000);
        assert_eq!(
            std::fs::metadata(spool["path"].as_str().unwrap())
                .unwrap()
                .len(),
            60000
        );
        std::fs::remove_file(spool["path"].as_str().unwrap()).unwrap();
    }
}

#[test]
fn locale_fallback_decodes_preview_and_keeps_spool_encoding() {
    let workspace = tempdir().unwrap();
    let mut payload = Vec::with_capacity(120_001);
    payload.push(b'A');
    for _ in 0..30_000 {
        payload.extend_from_slice(&[0xD6, 0xD0, 0xCE, 0xC4]);
    }
    std::fs::write(workspace.path().join("payload.bin"), &payload).unwrap();
    #[cfg(windows)]
    let script = "$b = [IO.File]::ReadAllBytes('payload.bin'); [Console]::OpenStandardOutput().Write($b, 0, $b.Length)";
    #[cfg(not(windows))]
    let script = "cat payload.bin";
    let result = run(request(workspace.path(), script));
    assert_eq!(result["exit_code"], 0, "{}", result["stderr"]);
    assert!(result["stdout"].as_str().unwrap().starts_with("A中文中文"));
    assert_eq!(
        result["stdout"].as_str().unwrap().chars().count(),
        50 * 1024
    );
    let spool = &result["stdout_spool"];
    assert_eq!(spool["encoding"], "GBK");
    assert_eq!(spool["size_chars"], 60_001);
    assert_eq!(
        std::fs::read(spool["path"].as_str().unwrap()).unwrap(),
        payload
    );
    std::fs::remove_file(spool["path"].as_str().unwrap()).unwrap();
}

use serde_json::{Value, json};
use std::{
    io::Write,
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
        "command": script,
        "argv": argv(script),
        "cwd": workspace,
        "environment": std::env::vars().collect::<std::collections::HashMap<_,_>>(),
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
    helper
        .stdin
        .take()
        .unwrap()
        .write_all(request.to_string().as_bytes())
        .unwrap();
    let output = helper.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
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

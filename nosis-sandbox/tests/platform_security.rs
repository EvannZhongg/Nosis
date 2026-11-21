#[cfg(any(target_os = "linux", target_os = "macos"))]
use serde_json::{Value, json};
#[cfg(any(target_os = "linux", target_os = "macos"))]
use std::{
    collections::HashMap,
    io::Write,
    path::Path,
    process::{Command, Stdio},
};
#[cfg(any(target_os = "linux", target_os = "macos"))]
use tempfile::tempdir;

#[cfg(any(target_os = "linux", target_os = "macos"))]
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

#[cfg(target_os = "linux")]
fn linux_request(workspace: &Path, plugin: &Path, script: &str) -> Value {
    json!({
        "protocol_version": 2,
        "command": script,
        "argv": ["/bin/sh", "-c", script],
        "cwd": workspace,
        "environment": std::env::vars().collect::<HashMap<_,_>>(),
        "locale_encoding": "utf-8",
        "stdin_base64": null,
        "timeout_seconds": 10,
        "spool_directory": std::env::temp_dir(),
        "policy": {"platform":"linux", "workspace":workspace, "read_only_paths":[plugin]}
    })
}

#[cfg(target_os = "linux")]
#[test]
fn linux_bubblewrap_enforces_workspace_and_read_only_mounts() {
    if Command::new("bwrap").arg("--version").output().is_err() {
        return;
    }
    let probe = Command::new("bwrap")
        .args([
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            "--ro-bind",
            "/",
            "/",
            "/bin/true",
        ])
        .output()
        .unwrap();
    if !probe.status.success() {
        return;
    }
    let root = tempdir().unwrap();
    let workspace = root.path().join("workspace");
    let plugin = root.path().join("plugin");
    std::fs::create_dir_all(&workspace).unwrap();
    std::fs::create_dir_all(&plugin).unwrap();
    let outside = root.path().join("outside.txt");
    std::fs::write(&outside, "host-secret").unwrap();
    let plugin_file = plugin.join("policy.txt");
    std::fs::write(&plugin_file, "read-only").unwrap();
    let script = format!(
        "printf workspace > inside.txt && cat '{}' > plugin.txt && ! cat '{}' > escaped.txt && ! printf changed > '{}'",
        plugin_file.display(),
        outside.display(),
        plugin_file.display()
    );
    let result = run(linux_request(&workspace, &plugin, &script));
    assert_eq!(result["exit_code"], 0, "{}", result["stderr"]);
    assert_eq!(
        std::fs::read_to_string(workspace.join("inside.txt")).unwrap(),
        "workspace"
    );
    assert_eq!(std::fs::read(workspace.join("escaped.txt")).unwrap(), b"");
    assert_eq!(
        std::fs::read_to_string(workspace.join("plugin.txt")).unwrap(),
        "read-only"
    );
    assert_eq!(std::fs::read_to_string(&plugin_file).unwrap(), "read-only");
}

#[cfg(target_os = "macos")]
fn macos_request(workspace: &Path, private_tmp: &Path, plugin: &Path, script: &str) -> Value {
    let mut environment: HashMap<String, String> = std::env::vars().collect();
    environment.insert("HOME".into(), private_tmp.to_string_lossy().into_owned());
    environment.insert("TMPDIR".into(), private_tmp.to_string_lossy().into_owned());
    environment.insert("TMP".into(), private_tmp.to_string_lossy().into_owned());
    environment.insert("TEMP".into(), private_tmp.to_string_lossy().into_owned());
    json!({
        "protocol_version": 2,
        "command": script,
        "argv": ["/bin/sh", "-c", script],
        "cwd": workspace,
        "environment": environment,
        "locale_encoding": "utf-8",
        "stdin_base64": null,
        "timeout_seconds": 10,
        "spool_directory": std::env::temp_dir(),
        "policy": {"platform":"macos", "workspace":workspace, "private_tmp":private_tmp, "read_only_paths":[plugin]}
    })
}

#[cfg(target_os = "macos")]
#[test]
fn macos_seatbelt_enforces_workspace_private_temp_and_read_only_mounts() {
    if !Path::new("/usr/bin/sandbox-exec").exists() {
        return;
    }
    let probe = Command::new("/usr/bin/sandbox-exec")
        .args(["-p", "(version 1) (allow default)", "/usr/bin/true"])
        .output()
        .unwrap();
    if probe.status.code() == Some(71) {
        return;
    }
    let root = tempdir().unwrap();
    let workspace = root.path().join("workspace");
    let plugin = root.path().join("plugin");
    let private_tmp = root.path().join("private");
    std::fs::create_dir_all(&workspace).unwrap();
    std::fs::create_dir_all(&plugin).unwrap();
    std::fs::create_dir_all(&private_tmp).unwrap();
    let outside = root.path().join("outside.txt");
    std::fs::write(&outside, "host-secret").unwrap();
    let plugin_file = plugin.join("policy.txt");
    std::fs::write(&plugin_file, "read-only").unwrap();
    let script = format!(
        "printf workspace > inside.txt && printf temporary > \"$TMPDIR/value.txt\" && cat '{}' > plugin.txt && ! cat '{}' > escaped.txt && ! printf changed > '{}'",
        plugin_file.display(),
        outside.display(),
        plugin_file.display()
    );
    let result = run(macos_request(&workspace, &private_tmp, &plugin, &script));
    assert_eq!(result["exit_code"], 0, "{}", result["stderr"]);
    assert_eq!(
        std::fs::read_to_string(workspace.join("inside.txt")).unwrap(),
        "workspace"
    );
    assert_eq!(
        std::fs::read_to_string(private_tmp.join("value.txt")).unwrap(),
        "temporary"
    );
    assert_eq!(std::fs::read(workspace.join("escaped.txt")).unwrap(), b"");
    assert_eq!(
        std::fs::read_to_string(workspace.join("plugin.txt")).unwrap(),
        "read-only"
    );
    assert_eq!(std::fs::read_to_string(&plugin_file).unwrap(), "read-only");
}

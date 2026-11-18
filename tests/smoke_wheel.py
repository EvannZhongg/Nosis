"""Check a platform wheel's assets and run its installed helper without Rust."""

import argparse
import base64
from email.parser import Parser
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from uuid import uuid4
import zipfile


def run_helper(helper: Path, request: dict) -> dict:
    with subprocess.Popen(
        [str(helper)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as process:
        assert json.loads(process.stdout.readline()) == {"protocol_version": 1}
        process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
        process.stdin.flush()
        # Keep the control pipe open: EOF cancels the command.
        process.wait(timeout=15)
        assert process.returncode == 0, process.stderr.read().decode("utf-8")
        result = json.loads(process.stdout.read())
    assert result["protocol_version"] == 1, result
    assert result["command"] == request["command"], result
    assert not result["timed_out"], result
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("platform")
    parser.add_argument("--sandbox", action="store_true", help="Also check workspace confinement on this host")
    args = parser.parse_args()
    platform = args.platform
    wheels = list(args.directory.glob("*.whl"))
    assert len(wheels) == 1, wheels
    wheel = wheels[0].resolve()
    assert wheel.name.endswith(f"-py3-none-{platform}.whl"), wheel.name
    executable = "nosis-sandbox.exe" if os.name == "nt" else "nosis-sandbox"
    helper_path = f"agent_core/bin/{executable}"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_path = next(name for name in names if name.endswith(".dist-info/WHEEL"))
        metadata = Parser().parsestr(archive.read(metadata_path).decode("utf-8"))
        assert metadata["Root-Is-Purelib"] == "false", metadata
        assert metadata.get_all("Tag") == [f"py3-none-{platform}"], metadata
        for asset in (
            helper_path,
            "interfaces/tui/dist/app.js",
            "interfaces/gui/static/index.html",
            "interfaces/mobile/dist/feishu.cjs",
        ):
            assert asset in names, asset
            assert archive.getinfo(asset).file_size > 0, asset
        assert any(name.startswith("interfaces/gui/static/assets/") and name.endswith(".js") for name in names)
        binary = archive.read(helper_path)
        arm64 = platform.endswith(("arm64", "aarch64"))
        if platform.startswith("win_"):
            assert binary[:2] == b"MZ"
            offset = struct.unpack_from("<I", binary, 0x3C)[0]
            assert binary[offset:offset + 4] == b"PE\x00\x00"
            machine = struct.unpack_from("<H", binary, offset + 4)[0]
            assert machine == (0xAA64 if arm64 else 0x8664), machine
        elif platform.startswith("macosx_"):
            assert binary[:4] == b"\xcf\xfa\xed\xfe"
            machine = struct.unpack_from("<I", binary, 4)[0]
            assert machine == (0x0100000C if arm64 else 0x01000007), machine
        else:
            assert binary[:5] == b"\x7fELF\x02"
            machine = struct.unpack_from("<H", binary, 18)[0]
            assert machine == (183 if arm64 else 62), machine
        if os.name != "nt":
            assert archive.getinfo(helper_path).external_attr >> 16 & 0o111

    root = Path(tempfile.gettempdir()) / f"nosis-wheel-{uuid4().hex}"
    # Windows mkdtemp() leaves a protected DACL that grants only SYSTEM,
    # Administrators and OWNER RIGHTS, so the helper's write-restricted token
    # cannot traverse into it. An ordinary workspace inherits the user's ACEs.
    root.mkdir(mode=0o777 if os.name == "nt" else 0o700)
    try:
        site = root / "site"
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps", "--no-compile",
             "--target", str(site), str(wheel)],
            check=True,
        )
        workspace = root / "workspace"
        workspace.mkdir()
        request = {
            "protocol_version": 1,
            "command": "wheel smoke test",
            "argv": [sys.executable, "-c",
                     "import sys; from pathlib import Path; "
                     "Path('result.txt').write_text(sys.stdin.read(), encoding='utf-8'); "
                     "sys.stdout.buffer.write(b'stdout\\n'); sys.stderr.buffer.write(b'stderr\\n')"],
            "cwd": str(workspace),
            "environment": {**os.environ, "PYTHONUTF8": "1"},
            "locale_encoding": "utf-8",
            "stdin_base64": base64.b64encode("wheel 输入".encode("utf-8")).decode("ascii"),
            "timeout_seconds": 10,
            "spool_directory": str(root),
            "policy": {"platform": "host"},
        }
        helper = site / helper_path
        result = run_helper(helper, request)
        assert result["exit_code"] == 0, result
        assert result["stdout"] == "stdout\n", result
        assert result["stderr"] == "stderr\n", result
        assert (workspace / "result.txt").read_text(encoding="utf-8") == "wheel 输入"
        if args.sandbox:
            outside = root / "outside.txt"
            outside.write_text("host-secret", encoding="utf-8")
            private_tmp = root / "private"
            private_tmp.mkdir()
            temporary_marker = private_tmp / "temporary.txt"
            policy = {"workspace": str(workspace)}
            environment = {**request["environment"], "NOSIS_WHEEL_OUTSIDE": str(outside)}
            if sys.platform == "win32":
                policy.update(platform="windows", private_tmp=str(private_tmp))
                environment.update(TEMP=str(private_tmp), TMP=str(private_tmp))
                powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
                argv = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                        "$ErrorActionPreference='Stop'; "
                        "[IO.File]::WriteAllText((Join-Path $env:TEMP 'temporary.txt'), 'temporary'); "
                        "[IO.File]::WriteAllText('confined.txt', 'workspace'); "
                        "[IO.File]::WriteAllText($env:NOSIS_WHEEL_OUTSIDE, 'escaped')"]
            else:
                policy.update(platform="linux" if sys.platform == "linux" else "macos", read_only_paths=[])
                if sys.platform == "darwin":
                    policy["private_tmp"] = str(private_tmp)
                argv = ["/bin/sh", "-c",
                        'printf workspace > confined.txt; printf escaped > "$NOSIS_WHEEL_OUTSIDE"']
            probe = {
                **request, "command": "workspace confinement probe", "argv": argv,
                "environment": environment,
                "stdin_base64": None,
            }
            # Prove the same command can change the host file without confinement.
            control = run_helper(helper, probe)
            assert control["exit_code"] == 0, control
            marker = workspace / "confined.txt"
            assert marker.is_file(), control
            assert marker.read_text(encoding="utf-8") == "workspace", control
            assert outside.read_text(encoding="utf-8") == "escaped", control
            marker.unlink()
            if sys.platform == "win32":
                assert temporary_marker.read_text(encoding="utf-8") == "temporary", control
                temporary_marker.unlink()
            outside.write_text("host-secret", encoding="utf-8")
            confined = run_helper(helper, {**probe, "policy": policy})
            assert marker.is_file(), confined
            assert marker.read_text(encoding="utf-8") == "workspace", confined
            if sys.platform == "win32":
                assert temporary_marker.is_file(), confined
                assert temporary_marker.read_text(encoding="utf-8") == "temporary", confined
            assert outside.read_text(encoding="utf-8") == "host-secret", confined
            if sys.platform != "linux":
                assert confined["exit_code"] != 0, confined
            # Linux may write to a private tmpfs shadow at the same path.
            print(f"Verified {policy['platform']} workspace confinement")
    finally:
        shutil.rmtree(root)
    print(f"Verified {wheel.name}")


if __name__ == "__main__":
    main()

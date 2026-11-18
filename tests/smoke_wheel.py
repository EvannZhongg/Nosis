"""Check a platform wheel's assets and run its installed helper without Rust."""

import base64
from email.parser import Parser
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zipfile


def main() -> None:
    directory, platform = sys.argv[1:]
    wheels = list(Path(directory).glob("*.whl"))
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

    with tempfile.TemporaryDirectory(prefix="nosis-wheel-") as temporary:
        root = Path(temporary)
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
        with subprocess.Popen(
            [str(site / helper_path)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as helper:
            assert json.loads(helper.stdout.readline()) == {"protocol_version": 1}
            helper.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
            helper.stdin.flush()
            # Keep the control pipe open: EOF cancels the command.
            helper.wait(timeout=15)
            assert helper.returncode == 0, helper.stderr.read().decode("utf-8")
            result = json.loads(helper.stdout.read())
        assert result["protocol_version"] == 1, result
        assert result["command"] == request["command"], result
        assert result["exit_code"] == 0, result
        assert result["stdout"] == "stdout\n", result
        assert result["stderr"] == "stderr\n", result
        assert not result["timed_out"], result
        assert (workspace / "result.txt").read_text(encoding="utf-8") == "wheel 输入"
    print(f"Verified {wheel.name}")


if __name__ == "__main__":
    main()

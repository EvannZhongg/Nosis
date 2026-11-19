from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from platform import machine

from setuptools import Distribution, setup
from setuptools.command.bdist_wheel import bdist_wheel
from setuptools.command.build_py import build_py


ROOT = Path(__file__).resolve().parent
CRATE = ROOT / "nosis-sandbox"


class BuildPyWithSandbox(build_py):
    def run(self) -> None:
        target = os.environ.get("CARGO_BUILD_TARGET")
        command = ["cargo", "build", "--release", "--locked", "--manifest-path", str(CRATE / "Cargo.toml")]
        if target:
            command.extend(["--target", target])
        subprocess.run(command, cwd=ROOT, check=True)
        super().run()
        target_dir = Path(os.environ.get("CARGO_TARGET_DIR", CRATE / "target"))
        if not target_dir.is_absolute():
            target_dir = ROOT / target_dir
        if target:
            target_dir /= target
        executable = "nosis-sandbox.exe" if os.name == "nt" else "nosis-sandbox"
        source = target_dir / "release" / executable
        destination = Path(self.build_lib) / "agent_core" / "bin" / executable
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if os.name != "nt":
            destination.chmod(0o755)

    def get_outputs(self, include_bytecode: int = 1) -> list[str]:
        outputs = super().get_outputs(include_bytecode)
        executable = "nosis-sandbox.exe" if os.name == "nt" else "nosis-sandbox"
        outputs.append(str(Path(self.build_lib) / "agent_core" / "bin" / executable))
        return outputs


class PlatformDistribution(Distribution):
    def has_ext_modules(self) -> bool:
        # The wheel contains a native executable even though it has no Python extension.
        return True


class PlatformWheel(bdist_wheel):
    """Tag the wheel for any Python, because only the helper binary is native."""

    def get_tag(self) -> tuple[str, str, str]:
        _, _, platform = super().get_tag()
        return "py3", "none", _apple_platform(platform)


def _apple_platform(default: str) -> str:
    """Name the helper's architecture instead of the interpreter's universal2 tag."""
    if sys.platform != "darwin":
        return default
    target = os.environ.get("CARGO_BUILD_TARGET", "")
    if target in ("aarch64-apple-darwin", "x86_64-apple-darwin"):
        arch = "arm64" if target.startswith("aarch64") else "x86_64"
    else:
        arch = "arm64" if machine() == "arm64" else "x86_64"
    version = os.environ.get("MACOSX_DEPLOYMENT_TARGET")
    if version is None:
        _, major, minor, _ = default.split("_", 3)
        # Apple Silicon starts at macOS 11; the interpreter may claim 10.9.
        version = f"{major}.{minor}"
        if arch == "arm64" and major == "10":
            version = "11.0"
    return f"macosx_{version.replace('.', '_')}_{arch}"


setup(
    distclass=PlatformDistribution,
    cmdclass={"build_py": BuildPyWithSandbox, "bdist_wheel": PlatformWheel},
)

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

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
        return "py3", "none", platform


setup(
    distclass=PlatformDistribution,
    cmdclass={"build_py": BuildPyWithSandbox, "bdist_wheel": PlatformWheel},
)

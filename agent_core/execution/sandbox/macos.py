import shutil
import tempfile
from pathlib import Path
from typing import Collection

from .. import process as command_process
from ..process import CancellationSignal, CommandExecutionResult
from .base import (
    SandboxBackend,
    SandboxPolicy,
    _require_supported_policy,
    _sandbox_environment,
)


class MacOSSandboxBackend(SandboxBackend):
    """Confine a command with the macOS Seatbelt sandbox."""

    _PROFILE = """\
(version 1)
(deny default)
(import "system.sb")
(deny network*)
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow file-read*
    (subpath (param "WORKSPACE"))
    (subpath (param "PRIVATE_TMP"))
    (subpath "/bin")
    (subpath "/usr/bin")
    (subpath "/usr/lib")
    (subpath "/usr/share")
    (subpath "/System")
    (subpath "/Library/Apple")
    (literal "/private/var/select/sh")
    (literal "/dev/null")
    (literal "/dev/urandom"))
(allow file-write*
    (subpath (param "WORKSPACE"))
    (subpath (param "PRIVATE_TMP"))
    (literal "/dev/null"))
"""

    def __init__(self) -> None:
        self._temporary_directory = Path(
            tempfile.mkdtemp(prefix="nosis-workspace-sandbox-")
        )

    def execute(
        self,
        command: str,
        *,
        working_directory: Path,
        policy: SandboxPolicy,
        timeout_seconds: int,
        cancellation: CancellationSignal | None = None,
        excluded_environment_names: Collection[str] = (),
    ) -> CommandExecutionResult:
        _require_supported_policy(policy, self.default_policy)
        workspace = working_directory.resolve()
        private_tmp = self._temporary_directory.resolve()
        profile = self._PROFILE + _ancestor_rules(workspace, private_tmp)
        return command_process._execute_process(
            [
                "/usr/bin/sandbox-exec",
                "-D",
                f"WORKSPACE={workspace}",
                "-D",
                f"PRIVATE_TMP={private_tmp}",
                "-p",
                profile,
                "/bin/sh",
                "-c",
                command,
            ],
            command=command,
            working_directory=workspace,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            environment=_sandbox_environment(
                private_tmp, excluded_environment_names
            ),
            spool_directory=private_tmp,
            reports_sandbox_entry_failure=True,
        )

    def close(self) -> None:
        shutil.rmtree(self._temporary_directory, ignore_errors=True)


def _ancestor_rules(*paths: Path) -> str:
    ancestors = {
        parent
        for path in paths
        for parent in path.parents
        if parent != Path("/")
    }
    literals = " ".join(
        f'(literal "{_escape_profile_string(str(path))}")'
        for path in sorted(ancestors, key=str)
    )
    return f"\n(allow file-read-metadata file-test-existence {literals})\n"


def _escape_profile_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')

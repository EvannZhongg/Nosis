import shutil
import tempfile
from pathlib import Path
from typing import Collection

from .. import process as command_process
from ...cancellation import CancellationSignal
from ..process import CommandExecutionResult
from .base import (
    SandboxBackend,
    SandboxPolicy,
    _require_supported_policy,
    _sandbox_environment,
)


class LinuxSandboxBackend(SandboxBackend):
    """Confine a command with bubblewrap and a minimal filesystem view."""

    def __init__(self) -> None:
        self._temporary_directory = Path(
            tempfile.mkdtemp(prefix="nosis-workspace-sandbox-")
        )

    def execute(
        self,
        command: str | tuple[str, ...],
        *,
        working_directory: Path,
        policy: SandboxPolicy,
        timeout_seconds: int,
        cancellation: CancellationSignal | None = None,
        excluded_environment_names: Collection[str] = (),
        stdin: bytes | None = None,
        read_only_paths: tuple[Path, ...] = (),
    ) -> CommandExecutionResult:
        _require_supported_policy(policy, self.default_policy)
        workspace = working_directory.resolve()
        private_tmp = self._temporary_directory.resolve()
        return command_process._execute_process(
            command_process.command_argv(command, workspace),
            command=command_process.command_display(command),
            working_directory=workspace,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            environment=_sandbox_environment(excluded_environment_names),
            spool_directory=private_tmp,
            stdin=stdin,
            policy={
                "platform": "linux",
                "workspace": str(workspace),
                "read_only_paths": [str(path.resolve()) for path in read_only_paths],
            },
        )

    def close(self) -> None:
        shutil.rmtree(self._temporary_directory, ignore_errors=True)

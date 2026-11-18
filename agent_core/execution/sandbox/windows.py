import os
import tempfile
from pathlib import Path
from typing import Collection

from .. import process as command_process
from ..process import CancellationSignal, CommandExecutionResult
from .base import (
    FilesystemAccess,
    NetworkAccess,
    SandboxBackend,
    SandboxPolicy,
    _require_supported_policy,
    _windows_sandbox_environment,
)


class WindowsSandboxBackend(SandboxBackend):
    """Launch a write-restricted process through the Rust sandbox helper."""

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
        if os.name != "nt":
            raise RuntimeError("the Windows sandbox backend requires Windows")
        workspace = working_directory.resolve()
        return command_process._execute_process(
            command_process.command_argv(command, workspace),
            command=command_process.command_display(command),
            working_directory=workspace,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            environment=_windows_sandbox_environment(excluded_environment_names),
            spool_directory=Path(tempfile.gettempdir()),
            stdin=stdin,
            policy={"platform": "windows", "workspace": str(workspace)},
        )

    @property
    def default_policy(self) -> SandboxPolicy:
        return SandboxPolicy(
            host_filesystem=FilesystemAccess.WRITE_RESTRICTED,
            network=NetworkAccess.ALLOW,
        )

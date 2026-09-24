import logging
import os
import shutil
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

_logger = logging.getLogger(__name__)


class WindowsSandboxBackend(SandboxBackend):
    """Restrict writes with a Windows WRITE_RESTRICTED access token.

    Windows keeps the caller's readable host view.  The restricted token
    adds write capabilities only for the workspace and a private temporary
    directory. Paths already writable by Everyone remain writable. Network
    access is not confined by this reduced-capability backend.
    """

    def __init__(self) -> None:
        self._temporary_directory: Path | None = None
        self._sandbox = None
        if os.name == "nt":
            from ...windows_sandbox import _CapabilityLeases, _WindowsApi

            _CapabilityLeases(_WindowsApi()).cleanup_stale()

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
        if os.name != "nt":
            raise RuntimeError("the Windows sandbox backend requires Windows")

        from ...windows_sandbox import WindowsWriteRestrictedSandbox

        workspace = working_directory.resolve()
        if self._temporary_directory is None:
            # Keep command output and private state outside the repository.
            self._temporary_directory = Path(
                tempfile.mkdtemp(prefix="nosis-sandbox-tmp-")
            )
        private_tmp = self._temporary_directory.resolve()
        if self._sandbox is None:
            try:
                self._sandbox = WindowsWriteRestrictedSandbox(workspace, private_tmp)
            except BaseException:
                self.close()
                raise
        elif self._sandbox.workspace != workspace:
            raise RuntimeError(
                "a Windows sandbox backend cannot be shared across workspaces"
            )
        return command_process._execute_process(
            command_process.platform_shell_launcher(command, workspace),
            command=command,
            working_directory=workspace,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            environment=_windows_sandbox_environment(
                private_tmp, excluded_environment_names
            ),
            spool_directory=private_tmp,
            process_launcher=self._sandbox.start,
        )

    @property
    def default_policy(self) -> SandboxPolicy:
        return SandboxPolicy(
            host_filesystem=FilesystemAccess.READ_ONLY,
            network=NetworkAccess.ALLOW,
        )

    def close(self) -> None:
        failures: list[Exception] = []
        if self._sandbox is not None:
            try:
                self._sandbox.close()
            except Exception as error:
                failures.append(error)
            else:
                self._sandbox = None
        if self._sandbox is None and self._temporary_directory is not None:
            try:
                if self._temporary_directory.exists():
                    shutil.rmtree(self._temporary_directory)
            except OSError as error:
                failures.append(error)
            else:
                self._temporary_directory = None
        if failures:
            error = ExceptionGroup("Windows sandbox cleanup incomplete", failures)
            _logger.warning(
                "Windows sandbox cleanup incomplete at %s; retaining resources "
                "for later cleanup", self._temporary_directory,
                exc_info=(type(error), error, error.__traceback__),
            )

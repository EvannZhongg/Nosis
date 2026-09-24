import os
from pathlib import Path
from typing import Collection, Protocol

from . import process as command_process
from .process import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    CancellationSignal,
    CommandExecutionResult,
    ShellLauncher,
)
from .sandbox.base import SandboxBackend, SandboxPolicy


class CommandExecutor(Protocol):
    def execute(
        self,
        command: str,
        timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        cancellation: CancellationSignal | None = None,
    ) -> CommandExecutionResult:
        raise NotImplementedError

    def close(self) -> None: ...


class HostCommandExecutor:
    """Run commands directly with the current host user's permissions.

    This executor provides no filesystem, network, temporary-directory, or
    process confinement.  Its working directory is only the command's cwd.
    """

    def __init__(
        self,
        working_directory: Path,
        shell_launcher: ShellLauncher | None = None,
    ) -> None:
        self._working_directory = working_directory
        self._shell_launcher = (
            shell_launcher or command_process.platform_shell_launcher
        )

    def execute(
        self,
        command: str,
        timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        cancellation: CancellationSignal | None = None,
    ) -> CommandExecutionResult:
        environment = None
        if os.name == "nt":
            environment = command_process._windows_shell_environment(
                os.environ.copy()
            )
        return command_process._execute_process(
            self._shell_launcher(command, self._working_directory),
            command=command,
            working_directory=self._working_directory,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            environment=environment,
        )

    def close(self) -> None:
        pass


class SandboxedCommandExecutor:
    """Run commands through a backend that enforces SandboxPolicy."""

    def __init__(
        self,
        working_directory: Path,
        backend: SandboxBackend,
        policy: SandboxPolicy | None = None,
        excluded_environment_names: Collection[str] = (),
    ) -> None:
        self._working_directory = working_directory
        self._backend = backend
        self._policy = policy or backend.default_policy
        self._excluded_environment_names = frozenset(
            excluded_environment_names
        )

    @property
    def policy(self) -> SandboxPolicy:
        return self._policy

    def execute(
        self,
        command: str,
        timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        cancellation: CancellationSignal | None = None,
    ) -> CommandExecutionResult:
        return self._backend.execute(
            command,
            working_directory=self._working_directory,
            policy=self._policy,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            excluded_environment_names=self._excluded_environment_names,
        )

    def close(self) -> None:
        self._backend.close()

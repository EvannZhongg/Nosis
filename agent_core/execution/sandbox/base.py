import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Collection

from ..process import (
    CancellationSignal,
    CommandExecutionResult,
    _windows_shell_environment,
)


class FilesystemAccess(StrEnum):
    """Filesystem access provided by a backend.

    WRITE_RESTRICTED retains reads and limits writes without guaranteeing
    read-only access; Windows can still write to Everyone-writable paths.
    """

    DENIED = "denied"
    READ_ONLY = "read_only"
    WRITE_RESTRICTED = "write_restricted"
    READ_WRITE = "read_write"


class NetworkAccess(StrEnum):
    DENY = "deny"
    ALLOW = "allow"


class TemporaryDirectoryMode(StrEnum):
    PRIVATE = "private"


class ProcessIsolation(StrEnum):
    ISOLATED = "isolated"


@dataclass(frozen=True)
class SandboxPolicy:
    """Isolation requested from a sandbox backend.

    This is deliberately a small policy surface.  It describes the first
    workspace sandbox contract without claiming that the host executor
    enforces any of it.
    """

    workspace_filesystem: FilesystemAccess = FilesystemAccess.READ_WRITE
    host_filesystem: FilesystemAccess = FilesystemAccess.DENIED
    network: NetworkAccess = NetworkAccess.DENY
    temporary_directory: TemporaryDirectoryMode = TemporaryDirectoryMode.PRIVATE
    processes: ProcessIsolation = ProcessIsolation.ISOLATED


class SandboxBackend(ABC):
    """Platform confinement mechanism used by SandboxedCommandExecutor."""

    @abstractmethod
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
        raise NotImplementedError

    @property
    def default_policy(self) -> SandboxPolicy:
        return SandboxPolicy()

    def close(self) -> None:
        """Release backend-owned resources such as roots or proxies."""


def _require_supported_policy(
    policy: SandboxPolicy, supported: SandboxPolicy
) -> None:
    if policy != supported:
        raise ValueError(
            "the workspace sandbox backend does not support the requested "
            "SandboxPolicy"
        )


def _sandbox_environment(
    private_tmp: Path,
    excluded_environment_names: Collection[str] = (),
) -> dict[str, str]:
    environment = os.environ.copy()
    for name in excluded_environment_names:
        environment.pop(name, None)
    environment.update(
        {
            "HOME": str(private_tmp),
            "TMPDIR": str(private_tmp),
            "TMP": str(private_tmp),
            "TEMP": str(private_tmp),
        }
    )
    return environment


def _windows_sandbox_environment(
    private_tmp: Path,
    excluded_environment_names: Collection[str] = (),
) -> dict[str, str]:
    return _windows_shell_environment(
        _sandbox_environment(private_tmp, excluded_environment_names)
    )

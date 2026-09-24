import platform

from .base import (
    FilesystemAccess,
    NetworkAccess,
    ProcessIsolation,
    SandboxBackend,
    SandboxPolicy,
    TemporaryDirectoryMode,
)
from .linux import LinuxSandboxBackend
from .macos import MacOSSandboxBackend
from .windows import WindowsSandboxBackend


def platform_workspace_sandbox_backend() -> SandboxBackend:
    system = platform.system()
    if system == "Darwin":
        return MacOSSandboxBackend()
    if system == "Linux":
        return LinuxSandboxBackend()
    if system == "Windows":
        return WindowsSandboxBackend()
    raise RuntimeError(f"workspace shell sandbox is not available on {system}")


__all__ = [
    "FilesystemAccess",
    "LinuxSandboxBackend",
    "MacOSSandboxBackend",
    "NetworkAccess",
    "ProcessIsolation",
    "SandboxBackend",
    "SandboxPolicy",
    "TemporaryDirectoryMode",
    "WindowsSandboxBackend",
    "platform_workspace_sandbox_backend",
]

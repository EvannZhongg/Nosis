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


class LinuxSandboxBackend(SandboxBackend):
    """Confine a command with bubblewrap and a minimal filesystem view."""

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
        executable = shutil.which("bwrap")
        if executable is None:
            raise RuntimeError(
                "workspace shell requires bubblewrap (bwrap) on Linux; "
                "install bwrap and enable unprivileged user namespaces"
            )
        workspace = working_directory.resolve()
        private_tmp = self._temporary_directory.resolve()
        argv = [
            executable,
            "--die-with-parent",
            # Bubblewrap always creates a mount namespace. These additional
            # namespaces are required: failure must never weaken isolation.
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            # Cgroup isolation is optional, as with bwrap's --unshare-all.
            "--unshare-cgroup-try",
            "--cap-drop",
            "ALL",
            "--new-session",
            "--tmpfs",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]
        for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
            if Path(path).exists():
                argv.extend(("--ro-bind", path, path))
        argv.extend(("--tmpfs", "/tmp"))
        _append_parent_directories(argv, workspace)
        argv.extend(("--bind", str(workspace), str(workspace)))
        argv.extend(("--chdir", str(workspace), "/bin/sh", "-c", command))
        # Preserve bwrap's nonzero status and setup diagnostic on failure;
        # never retry the command outside the sandbox.
        return command_process._execute_process(
            argv,
            command=command,
            working_directory=workspace,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            environment=_sandbox_environment(
                Path("/tmp"), excluded_environment_names
            ),
            spool_directory=private_tmp,
        )

    def close(self) -> None:
        shutil.rmtree(self._temporary_directory, ignore_errors=True)


def _append_parent_directories(argv: list[str], path: Path) -> None:
    parents = tuple(reversed(path.parents))
    for parent in (*parents, path):
        if parent != Path("/"):
            argv.extend(("--dir", str(parent)))

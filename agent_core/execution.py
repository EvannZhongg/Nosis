import locale
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
import atexit
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from .tools.base import ToolCall


DEFAULT_COMMAND_TIMEOUT_SECONDS = 60
MAX_COMMAND_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_COMMAND_OUTPUT_CHARS = 50 * 1024
SPOOL_FILE_PREFIX = "nosis-shell-"
STALE_SPOOL_AGE_SECONDS = 24 * 60 * 60
_SPOOL_PATHS: set[Path] = set()


@dataclass(frozen=True)
class CommandExecutionResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    timeout_seconds: int | None = None
    stdout_spool: "CommandOutputSpool | None" = None
    stderr_spool: "CommandOutputSpool | None" = None

@dataclass(frozen=True)
class CommandOutputSpool:
    """A complete command stream retained on disk for artifact creation."""

    path: Path
    size_chars: int
    encoding: str

    def cleanup(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            _SPOOL_PATHS.discard(self.path)
        except PermissionError:
            # A child process can briefly retain an inherited handle on
            # Windows after the parent shell has exited.  Cleanup is
            # best-effort here; callers must not lose an otherwise valid
            # command result because a temporary file is still locked.
            pass
        else:
            _SPOOL_PATHS.discard(self.path)


def _cleanup_registered_spools() -> None:
    for path in tuple(_SPOOL_PATHS):
        _unlink_best_effort(path)


atexit.register(_cleanup_registered_spools)


class CommandExecutor(Protocol):
    def execute(
        self,
        command: str,
        timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        cancellation: "CancellationSignal | None" = None,
    ) -> CommandExecutionResult:
        raise NotImplementedError

    def close(self) -> None: ...


class RunningProcess(Protocol):
    pid: int
    returncode: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


ProcessLauncher = Callable[..., RunningProcess]
ShellLauncher = Callable[[str, Path], list[str]]


class ExecutionScope(StrEnum):
    WORKSPACE = "workspace"
    HOST = "host"


_SCOPE_RANK = {
    ExecutionScope.WORKSPACE: 0,
    ExecutionScope.HOST: 1,
}


@dataclass(frozen=True)
class ExecutionAuthority:
    """The execution boundary an Agent may request and use unattended."""

    default_scope: ExecutionScope
    maximum_scope: ExecutionScope
    unattended_scope: ExecutionScope | None

    def __post_init__(self) -> None:
        if not self.allows(self.default_scope):
            raise ValueError("default execution scope exceeds authority")
        if (
            self.unattended_scope is not None
            and not self.allows(self.unattended_scope)
        ):
            raise ValueError("unattended execution scope exceeds authority")

    def allows(self, scope: ExecutionScope) -> bool:
        return _SCOPE_RANK[scope] <= _SCOPE_RANK[self.maximum_scope]

    def allows_unattended(self, scope: ExecutionScope) -> bool:
        return (
            self.unattended_scope is not None
            and _SCOPE_RANK[scope] <= _SCOPE_RANK[self.unattended_scope]
        )

    @property
    def scopes(self) -> tuple[ExecutionScope, ...]:
        return tuple(scope for scope in ExecutionScope if self.allows(scope))

    def intersect(self, other: "ExecutionAuthority") -> "ExecutionAuthority":
        maximum_scope = min(
            self.maximum_scope,
            other.maximum_scope,
            key=_SCOPE_RANK.__getitem__,
        )
        default_scope = min(
            self.default_scope,
            other.default_scope,
            maximum_scope,
            key=_SCOPE_RANK.__getitem__,
        )
        unattended = tuple(
            scope
            for scope in (self.unattended_scope, other.unattended_scope)
            if scope is not None
        )
        unattended_scope = (
            min((*unattended, maximum_scope), key=_SCOPE_RANK.__getitem__)
            if len(unattended) == 2
            else None
        )
        return ExecutionAuthority(
            default_scope=default_scope,
            maximum_scope=maximum_scope,
            unattended_scope=unattended_scope,
        )


APPROVAL_REQUIRED_AUTHORITY = ExecutionAuthority(
    default_scope=ExecutionScope.WORKSPACE,
    maximum_scope=ExecutionScope.HOST,
    unattended_scope=None,
)
WORKSPACE_ACCESS_AUTHORITY = ExecutionAuthority(
    default_scope=ExecutionScope.WORKSPACE,
    maximum_scope=ExecutionScope.HOST,
    unattended_scope=ExecutionScope.WORKSPACE,
)
WORKSPACE_ONLY_AUTHORITY = ExecutionAuthority(
    default_scope=ExecutionScope.WORKSPACE,
    maximum_scope=ExecutionScope.WORKSPACE,
    unattended_scope=ExecutionScope.WORKSPACE,
)
FULL_ACCESS_AUTHORITY = ExecutionAuthority(
    default_scope=ExecutionScope.HOST,
    maximum_scope=ExecutionScope.HOST,
    unattended_scope=ExecutionScope.HOST,
)


@dataclass(frozen=True)
class ResolvedExecution:
    scope: ExecutionScope | None
    authority: ExecutionAuthority
    executor: CommandExecutor | None = None


AuthoritySource = ExecutionAuthority | Callable[[], ExecutionAuthority]


class ExecutionRouter:
    """Resolve an invocation's authority, scope, and executor once."""

    def __init__(
        self,
        workspace_executor: CommandExecutor,
        host_executor: CommandExecutor,
        *,
        authority: AuthoritySource = WORKSPACE_ACCESS_AUTHORITY,
        host_tool: Callable[[str], bool] | None = None,
    ) -> None:
        self._workspace_executor = workspace_executor
        self._host_executor = host_executor
        self._authority = authority
        self._host_tool = host_tool or (lambda name: False)

    @property
    def authority(self) -> ExecutionAuthority:
        source = self._authority
        return source() if callable(source) else source

    @property
    def workspace_policy(self) -> object | None:
        return getattr(self._workspace_executor, "policy", None)

    def resolve(self, call: "ToolCall") -> ResolvedExecution:
        authority = self.authority
        if call.name == "shell":
            scope = self._shell_scope(call.arguments, authority)
            return ResolvedExecution(
                scope=scope,
                authority=authority,
                executor=(
                    self._workspace_executor
                    if scope is ExecutionScope.WORKSPACE
                    else self._host_executor
                ),
            )
        scope = ExecutionScope.HOST if self._host_tool(call.name) else None
        if scope is not None and not authority.allows(scope):
            raise PermissionError(
                f"execution scope '{scope.value}' exceeds authority"
            )
        return ResolvedExecution(scope=scope, authority=authority)

    def close(self) -> None:
        try:
            self._workspace_executor.close()
        finally:
            self._host_executor.close()

    @staticmethod
    def _shell_scope(
        arguments: dict[str, object],
        authority: ExecutionAuthority,
    ) -> ExecutionScope:
        value = arguments.get("scope", authority.default_scope.value)
        if not isinstance(value, str):
            raise ValueError("execution scope must be 'workspace' or 'host'")
        try:
            scope = ExecutionScope(value)
        except ValueError as error:
            raise ValueError(
                "execution scope must be 'workspace' or 'host'"
            ) from error
        if not authority.allows(scope):
            raise PermissionError(
                f"execution scope '{scope.value}' exceeds authority"
            )
        return scope


class FilesystemAccess(StrEnum):
    DENIED = "denied"
    READ_ONLY = "read_only"
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
    temporary_directory: TemporaryDirectoryMode = (
        TemporaryDirectoryMode.PRIVATE
    )
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
        cancellation: "CancellationSignal | None" = None,
    ) -> CommandExecutionResult:
        raise NotImplementedError

    @property
    def default_policy(self) -> SandboxPolicy:
        return SandboxPolicy()

    def close(self) -> None:
        """Release backend-owned resources such as roots or proxies."""


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
        cancellation: "CancellationSignal | None" = None,
    ) -> CommandExecutionResult:
        _require_workspace_policy(policy, self.default_policy)
        executable = shutil.which("bwrap")
        if executable is None:
            raise RuntimeError(
                "workspace shell requires bubblewrap (bwrap) on Linux"
            )
        workspace = working_directory.resolve()
        private_tmp = self._temporary_directory.resolve()
        argv = [
            executable,
            "--die-with-parent",
            "--unshare-all",
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
        _append_linux_parent_directories(argv, workspace)
        argv.extend(("--bind", str(workspace), str(workspace)))
        argv.extend(("--chdir", str(workspace), "/bin/sh", "-c", command))
        return _execute_process(
            argv,
            command=command,
            working_directory=workspace,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            environment=_sandbox_environment(Path("/tmp")),
            spool_directory=private_tmp,
        )

    def close(self) -> None:
        shutil.rmtree(self._temporary_directory, ignore_errors=True)


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
        cancellation: "CancellationSignal | None" = None,
    ) -> CommandExecutionResult:
        _require_workspace_policy(policy, self.default_policy)
        workspace = working_directory.resolve()
        private_tmp = self._temporary_directory.resolve()
        profile = self._PROFILE + _macos_ancestor_rules(
            workspace, private_tmp
        )
        return _execute_process(
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
            environment=_sandbox_environment(private_tmp),
            spool_directory=private_tmp,
            reports_sandbox_entry_failure=True,
        )

    def close(self) -> None:
        shutil.rmtree(self._temporary_directory, ignore_errors=True)


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

    def execute(
        self,
        command: str,
        *,
        working_directory: Path,
        policy: SandboxPolicy,
        timeout_seconds: int,
        cancellation: "CancellationSignal | None" = None,
    ) -> CommandExecutionResult:
        _require_workspace_policy(policy, self.default_policy)
        if os.name != "nt":
            raise RuntimeError("the Windows sandbox backend requires Windows")

        from .windows_sandbox import WindowsWriteRestrictedSandbox

        workspace = working_directory.resolve()
        if self._temporary_directory is None:
            # Keep command output and private state outside the repository.
            self._temporary_directory = Path(
                tempfile.mkdtemp(prefix="nosis-sandbox-tmp-")
            )
        private_tmp = self._temporary_directory.resolve()
        if self._sandbox is None:
            self._sandbox = WindowsWriteRestrictedSandbox(
                workspace, private_tmp
            )
        elif self._sandbox.workspace != workspace:
            raise RuntimeError(
                "a Windows sandbox backend cannot be shared across workspaces"
            )
        return _execute_process(
            platform_shell_launcher(command, workspace),
            command=command,
            working_directory=workspace,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            environment=_windows_sandbox_environment(private_tmp),
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
        if self._sandbox is not None:
            self._sandbox.close()
            self._sandbox = None
        if self._temporary_directory is not None:
            shutil.rmtree(self._temporary_directory, ignore_errors=True)
            self._temporary_directory = None


class CancellationSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...


class CommandCancelled(BaseException):
    """The Runtime cancelled a running command and its process group."""


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
        self._shell_launcher = shell_launcher or platform_shell_launcher

    def execute(
        self,
        command: str,
        timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        cancellation: CancellationSignal | None = None,
    ) -> CommandExecutionResult:
        return _execute_process(
            self._shell_launcher(command, self._working_directory),
            command=command,
            working_directory=self._working_directory,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
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
    ) -> None:
        self._working_directory = working_directory
        self._backend = backend
        self._policy = policy or backend.default_policy

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
        )

    def close(self) -> None:
        self._backend.close()


def platform_workspace_sandbox_backend() -> SandboxBackend:
    system = platform.system()
    if system == "Darwin":
        return MacOSSandboxBackend()
    if system == "Linux":
        return LinuxSandboxBackend()
    if system == "Windows":
        return WindowsSandboxBackend()
    raise RuntimeError(f"workspace shell sandbox is not available on {system}")


def _require_workspace_policy(
    policy: SandboxPolicy, supported: SandboxPolicy
) -> None:
    if policy != supported:
        raise ValueError(
            "the workspace sandbox backend does not support the requested "
            "SandboxPolicy"
        )


def _sandbox_environment(private_tmp: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(private_tmp),
            "TMPDIR": str(private_tmp),
            "TMP": str(private_tmp),
            "TEMP": str(private_tmp),
        }
    )
    return environment


def _windows_sandbox_environment(private_tmp: Path) -> dict[str, str]:
    environment = _sandbox_environment(private_tmp)
    # PowerShell otherwise emits ANSI sequences when TERM advertises a TTY.
    environment.pop("TERM", None)
    environment["NO_COLOR"] = "1"
    return environment


def _is_msys_runtime_path(value: str) -> bool:
    path = os.path.normcase(os.path.normpath(value)).rstrip("\\")
    parts = path.split("\\")
    try:
        git_index = max(
            index for index, part in enumerate(parts) if part == "git"
        )
    except ValueError:
        return False
    relative = parts[git_index + 1 :]
    return relative in (
        ["bin"],
        ["usr", "bin"],
        ["mingw32", "bin"],
        ["mingw64", "bin"],
    )


def _macos_ancestor_rules(*paths: Path) -> str:
    ancestors = {
        parent
        for path in paths
        for parent in path.parents
        if parent != Path("/")
    }
    literals = " ".join(
        f'(literal "{_escape_sandbox_string(str(path))}")'
        for path in sorted(ancestors, key=str)
    )
    return f"\n(allow file-read-metadata file-test-existence {literals})\n"


def _escape_sandbox_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _append_linux_parent_directories(argv: list[str], path: Path) -> None:
    parents = tuple(reversed(path.parents))
    for parent in (*parents, path):
        if parent == Path("/"):
            continue
        argv.extend(("--dir", str(parent)))


def _execute_process(
    argv: list[str],
    *,
    command: str,
    working_directory: Path,
    timeout_seconds: int,
    cancellation: CancellationSignal | None,
    environment: dict[str, str] | None = None,
    spool_directory: Path | None = None,
    reports_sandbox_entry_failure: bool = False,
    process_launcher: ProcessLauncher | None = None,
) -> CommandExecutionResult:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds < 1
        or timeout_seconds > MAX_COMMAND_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "command timeout must be an integer between 1 and "
            f"{MAX_COMMAND_TIMEOUT_SECONDS} seconds"
        )

    _reap_stale_spools()
    stdout_file = None
    stderr_file = None
    try:
        stdout_file = tempfile.NamedTemporaryFile(
            prefix=SPOOL_FILE_PREFIX,
            suffix="-stdout",
            dir=spool_directory,
            delete=False,
        )
        stdout_path = Path(stdout_file.name)
        _SPOOL_PATHS.add(stdout_path)
        stderr_file = tempfile.NamedTemporaryFile(
            prefix=SPOOL_FILE_PREFIX,
            suffix="-stderr",
            dir=spool_directory,
            delete=False,
        )
        stderr_path = Path(stderr_file.name)
        _SPOOL_PATHS.add(stderr_path)
    except BaseException:
        if stdout_file is not None:
            stdout_file.close()
        if stderr_file is not None:
            stderr_file.close()
        if stdout_file is not None:
            _unlink_best_effort(stdout_path)
        if stderr_file is not None:
            _unlink_best_effort(stderr_path)
        raise
    process = None
    timed_out = False
    try:
        if process_launcher is None:
            process = subprocess.Popen(
                argv,
                cwd=working_directory,
                env=environment,
                # Commands must never read the stream the UI protocol uses.
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
        else:
            process = process_launcher(
                argv,
                cwd=working_directory,
                environment=environment,
                stdout=stdout_file,
                stderr=stderr_file,
            )
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if cancellation is not None and cancellation.cancelled:
                _kill_process_tree(process)
                process.wait()
                raise CommandCancelled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process_tree(process)
                process.wait()
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        if process is not None:
            _kill_process_tree(process)
            process.wait()
        stdout_file.close()
        stderr_file.close()
        _unlink_best_effort(stdout_path)
        _unlink_best_effort(stderr_path)
        raise
    finally:
        stdout_file.close()
        stderr_file.close()

    try:
        stdout, stdout_spool = _read_spooled_output(stdout_path)
        stderr, stderr_spool = _read_spooled_output(stderr_path)
    except BaseException:
        _unlink_best_effort(stdout_path)
        _unlink_best_effort(stderr_path)
        raise
    if stdout_spool is None:
        _unlink_best_effort(stdout_path)
    if stderr_spool is None:
        _unlink_best_effort(stderr_path)
    if (
        reports_sandbox_entry_failure
        and process.returncode == 71
        and "sandbox_apply: Operation not permitted" in stderr
    ):
        raise RuntimeError(
            "workspace sandbox could not be entered: Operation not permitted"
        )
    return CommandExecutionResult(
        command=command,
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
        stdout_spool=stdout_spool,
        stderr_spool=stderr_spool,
    )


def platform_shell_launcher(command: str, working_directory: Path) -> list[str]:
    """Build the platform shell argv; execution scope never selects it."""
    if os.name == "nt":
        return _windows_powershell_argv(command, working_directory)
    return ["/bin/sh", "-c", command]


def _windows_powershell_argv(
    command: str, working_directory: Path
) -> list[str]:
    executable = _powershell_7()
    return [
        str(executable),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-WorkingDirectory",
        str(working_directory),
        "-Command",
        command,
    ]


def _powershell_7() -> str:
    candidates: list[str | None] = []
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(
            str(Path(program_files) / "PowerShell" / "7" / "pwsh.exe")
        )
    candidates.append(shutil.which("pwsh.exe"))
    for candidate in candidates:
        if candidate is None:
            continue
        executable = Path(candidate).resolve()
        if executable.is_file():
            return str(executable)
    raise RuntimeError(
        "Windows workspace shell requires PowerShell 7 (pwsh.exe)"
    )


def _kill_process_tree(process: RunningProcess) -> None:
    """Kill a command tree using its native lifecycle container when present."""
    terminate_tree = getattr(process, "terminate_tree", None)
    if callable(terminate_tree):
        try:
            terminate_tree()
            return
        except OSError:
            # A process may have exited and released its job between polling
            # and cancellation.  Keep the platform fallback for that race.
            pass
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _read_spooled_output(
    path: Path,
) -> tuple[str, CommandOutputSpool | None]:
    """Return a bounded preview and retain oversized streams on disk."""
    preview, size_chars, encoding = _read_text_preview(
        path, MAX_COMMAND_OUTPUT_CHARS
    )
    if size_chars <= MAX_COMMAND_OUTPUT_CHARS:
        return preview, None
    return preview, CommandOutputSpool(
        path=path,
        size_chars=size_chars,
        encoding=encoding,
    )


def _unlink_best_effort(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        # See CommandOutputSpool.cleanup: a transient Windows sharing
        # violation must not turn a successful command into a tool error.
        pass
    else:
        _SPOOL_PATHS.discard(path)


def _reap_stale_spools() -> None:
    cutoff = time.time() - STALE_SPOOL_AGE_SECONDS
    temp_directory = Path(tempfile.gettempdir())
    try:
        candidates = temp_directory.glob(f"{SPOOL_FILE_PREFIX}*")
    except OSError:
        return
    for path in candidates:
        if path in _SPOOL_PATHS:
            continue
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                _unlink_best_effort(path)
        except OSError:
            continue


_reap_stale_spools()


def _read_text_preview(path: Path, budget: int) -> tuple[str, int, str]:
    """Read a bounded head/tail preview without loading the whole file."""
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        try:
            preview, size_chars = _read_text_preview_with_encoding(
                path, budget, encoding, "strict"
            )
            return preview, size_chars, encoding
        except UnicodeDecodeError:
            continue
    preview, size_chars = _read_text_preview_with_encoding(
        path,
        budget,
        locale.getpreferredencoding(False),
        "replace",
    )
    return preview, size_chars, locale.getpreferredencoding(False)


def _read_text_preview_with_encoding(
    path: Path,
    budget: int,
    encoding: str,
    errors: str,
) -> tuple[str, int]:
    head: list[str] = []
    tail: deque[str] = deque(maxlen=budget)
    size_chars = 0
    with path.open("r", encoding=encoding, errors=errors, newline="") as file:
        while True:
            chunk = file.read(8192)
            if not chunk:
                break
            size_chars += len(chunk)
            remaining_head = budget - len(head)
            if remaining_head > 0:
                head.extend(chunk[:remaining_head])
            tail.extend(chunk)

    if size_chars <= budget:
        return "".join(head), size_chars
    kept_chars = budget
    omitted_chars = size_chars - kept_chars
    while True:
        marker = f"\n... [truncated {omitted_chars} characters] ...\n"
        kept_chars = max(0, budget - len(marker))
        next_omitted_chars = size_chars - kept_chars
        if next_omitted_chars == omitted_chars:
            break
        omitted_chars = next_omitted_chars
    head_chars = (kept_chars + 1) // 2
    tail_chars = kept_chars // 2
    return (
        "".join(head[:head_chars])
        + marker
        + ("".join(tail)[-tail_chars:] if tail_chars else ""),
        size_chars,
    )

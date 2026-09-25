import atexit
import locale
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..turn_control import AgentCancelled


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


class CancellationSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...


class CommandCancelled(AgentCancelled):
    """The Runtime cancelled a running command and its process group."""


class RunningProcess(Protocol):
    pid: int
    returncode: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


ProcessLauncher = Callable[..., RunningProcess]
ShellLauncher = Callable[[str, Path], list[str]]


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


def _windows_shell_environment(environment: dict[str, str]) -> dict[str, str]:
    """Keep PowerShell output plain for both host and workspace commands."""
    environment.pop("TERM", None)
    environment["NO_COLOR"] = "1"
    return environment


def _windows_powershell_argv(
    command: str, working_directory: Path
) -> list[str]:
    return [
        _powershell_7(),
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
    raise RuntimeError("Windows shell requires PowerShell 7 (pwsh.exe)")


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
    return preview, CommandOutputSpool(path, size_chars, encoding)


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


def _cleanup_registered_spools() -> None:
    for path in tuple(_SPOOL_PATHS):
        _unlink_best_effort(path)


def _read_text_preview(path: Path, budget: int) -> tuple[str, int, str]:
    """Read a bounded head/tail preview without loading the whole file."""
    preferred_encoding = locale.getpreferredencoding(False)
    for encoding in ("utf-8", preferred_encoding):
        try:
            preview, size_chars = _read_text_preview_with_encoding(
                path, budget, encoding, "strict"
            )
            return preview, size_chars, encoding
        except UnicodeDecodeError:
            continue
    preview, size_chars = _read_text_preview_with_encoding(
        path, budget, preferred_encoding, "replace"
    )
    return preview, size_chars, preferred_encoding


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


atexit.register(_cleanup_registered_spools)
_reap_stale_spools()

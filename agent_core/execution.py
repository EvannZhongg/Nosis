import locale
import os
import shutil
import signal
import subprocess
import atexit
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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


class CancellationSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...


class CommandCancelled(BaseException):
    """The Runtime cancelled a running command and its process group."""


class SubprocessCommandExecutor:
    def __init__(self, working_directory: Path) -> None:
        self._working_directory = working_directory

    def execute(
        self,
        command: str,
        timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        cancellation: CancellationSignal | None = None,
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
                prefix=SPOOL_FILE_PREFIX, suffix="-stdout", delete=False
            )
            stdout_path = Path(stdout_file.name)
            _SPOOL_PATHS.add(stdout_path)
            stderr_file = tempfile.NamedTemporaryFile(
                prefix=SPOOL_FILE_PREFIX, suffix="-stderr", delete=False
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
            process = subprocess.Popen(
                _shell_argv(command),
                cwd=self._working_directory,
                # Commands must never read the stream the UI protocol uses.
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
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
            # The command runs in its own process group, so an interrupted
            # wait would otherwise leave it running detached.
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


def _shell_argv(command: str) -> list[str]:
    """Build the argv that runs *command* in a POSIX shell.

    macOS and Linux provide one at /bin/sh. Windows does not, so Git
    Bash is used there to keep command syntax identical everywhere.
    """
    if os.name == "nt":
        return [_git_bash(), "--noprofile", "--norc", "-c", command]

    return ["/bin/sh", "-c", command]


def _git_bash() -> str:
    """Locate the Git Bash shipped with Git for Windows.

    The bash.exe in System32 launches WSL, which runs in a different
    filesystem and cannot see the workspace.
    """
    candidates = [shutil.which("bash")]
    program_files = os.environ.get("ProgramFiles")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if program_files:
        git_root = Path(program_files) / "Git"
        candidates += [
            str(git_root / "bin" / "bash.exe"),
            str(git_root / "usr" / "bin" / "bash.exe"),
        ]
    if local_app_data:
        candidates.append(
            str(Path(local_app_data, "Programs", "Git", "bin", "bash.exe"))
        )

    for candidate in candidates:
        if (
            candidate is not None
            and Path(candidate).is_file()
            and not _is_wsl_bash(candidate)
        ):
            return candidate

    raise RuntimeError(
        "shell requires Git Bash on Windows; install Git for Windows from "
        "https://git-scm.com/download/win"
    )


def _is_wsl_bash(path: str) -> bool:
    """Report whether *path* is the WSL launcher in System32."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    wsl_launcher = Path(system_root) / "System32" / "bash.exe"
    return os.path.normcase(str(Path(path).resolve())) == os.path.normcase(
        str(wsl_launcher.resolve())
    )


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the command together with the processes it started.

    os.killpg is POSIX-only, and on Windows killing the shell alone
    leaves the children it spawned holding the output pipes open.
    """
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


def _decode_output(data: bytes | None) -> str:
    """Turn captured output bytes into text.

    Programs on Windows write in the console or ANSI code page rather
    than UTF-8, and a stream that fails to decode is reported to the
    caller as None by subprocess.
    """
    if not data:
        return ""

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(
            locale.getpreferredencoding(False),
            errors="replace",
        )


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

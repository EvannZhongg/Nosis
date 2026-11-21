import atexit
import base64
import json
import locale
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..cancellation import CancellationSignal
from ..turn_control import AgentCancelled


DEFAULT_COMMAND_TIMEOUT_SECONDS = 60
MAX_COMMAND_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_COMMAND_OUTPUT_CHARS = 50 * 1024
_SPOOL_PATHS: set[Path] = set()
HELPER_PROTOCOL_VERSION = 1
_HELPER_STARTUP_TIMEOUT_SECONDS = 5
_HELPER_REPAIR = (
    "Reinstall the matching wheel with `python -m pip install --force-reinstall "
    "--no-cache-dir nosis-agent`, or, for a source checkout, run "
    "`cargo build --release --manifest-path nosis-sandbox/Cargo.toml`."
)


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
class TextExcerpt:
    head: str
    tail: str
    size_chars: int

    def render(self, kept_chars: int) -> str:
        if kept_chars >= self.size_chars:
            return self.head
        head_chars = (kept_chars + 1) // 2
        tail_chars = kept_chars // 2
        return (
            self.head[:head_chars]
            + f"\n... [truncated {self.size_chars - kept_chars} characters] ...\n"
            + (self.tail[-tail_chars:] if tail_chars else "")
        )


@dataclass(frozen=True)
class CommandOutputSpool:
    path: Path
    size_chars: int
    encoding: str

    def excerpt(self, limit: int) -> TextExcerpt:
        return _read_text_excerpt(self.path, limit, self.encoding, "replace")

    def cleanup(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            _SPOOL_PATHS.discard(self.path)
        except PermissionError:
            pass
        else:
            _SPOOL_PATHS.discard(self.path)


class CommandCancelled(AgentCancelled):
    """The Runtime cancelled a running command and its process tree."""


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
    stdin: bytes | None = None,
    policy: dict[str, object] | None = None,
) -> CommandExecutionResult:
    if cancellation is not None and cancellation.cancelled:
        raise CommandCancelled
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

    helper = _sandbox_executable()
    request = {
        "protocol_version": HELPER_PROTOCOL_VERSION,
        "command": command,
        "argv": argv,
        "cwd": str(working_directory),
        "environment": environment if environment is not None else os.environ.copy(),
        "locale_encoding": locale.getpreferredencoding(False),
        "stdin_base64": base64.b64encode(stdin).decode("ascii") if stdin is not None else None,
        "timeout_seconds": timeout_seconds,
        "spool_directory": str(spool_directory or tempfile.gettempdir()),
        "policy": policy or {"platform": "host"},
    }
    process = subprocess.Popen(
        [str(helper)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    assert process.stdin is not None
    output: dict[str, bytes] = {}
    hello: queue.Queue[bytes] = queue.Queue(maxsize=1)

    def read_stdout() -> None:
        assert process.stdout is not None
        hello.put(process.stdout.readline())
        output["stdout"] = process.stdout.read()

    readers = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=lambda: output.__setitem__("stderr", process.stderr.read()), daemon=True),
    ]
    for reader in readers:
        reader.start()
    unsubscribe = None
    pipe_lock = threading.Lock()

    def cancel_process() -> None:
        with pipe_lock:
            if not process.stdin.closed:
                try:
                    process.stdin.write(b"\x00")
                    process.stdin.flush()
                except OSError:
                    pass  # The helper may have exited concurrently.

    try:
        try:
            greeting = json.loads(hello.get(timeout=_HELPER_STARTUP_TIMEOUT_SECONDS))
            if greeting.get("protocol_version") != HELPER_PROTOCOL_VERSION:
                raise ValueError(f"helper reported {greeting.get('protocol_version')!r}")
        except (queue.Empty, ValueError, AttributeError) as error:
            # No request is sent until the handshake succeeds, including to
            # binaries that predate the handshake and wait for stdin EOF.
            process.kill()
            # Termination can be asynchronous; keep stdin open until exit so
            # an old helper cannot resume execution when it receives EOF.
            process.wait()
            raise RuntimeError(
                f"nosis-sandbox protocol mismatch or missing handshake "
                f"(expected {HELPER_PROTOCOL_VERSION}, executable: {helper}; "
                f"{str(error) or 'no handshake received'}). {_HELPER_REPAIR}"
            ) from error
        process.stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
        process.stdin.flush()
        if cancellation is not None:
            unsubscribe = cancellation.subscribe(cancel_process)
        process.wait()
    finally:
        if unsubscribe is not None:
            unsubscribe()
        with pipe_lock:
            process.stdin.close()
        process.wait()
        for reader in readers:
            reader.join()
        process.stdout.close()
        process.stderr.close()
    if process.returncode:
        if cancellation is not None and cancellation.cancelled:
            raise CommandCancelled
        diagnostic = output["stderr"].decode("utf-8", errors="replace").strip()
        raise RuntimeError(diagnostic or f"nosis-sandbox exited with status {process.returncode}")

    payload = json.loads(output["stdout"])
    if payload.get("protocol_version") != HELPER_PROTOCOL_VERSION:
        raise RuntimeError(f"nosis-sandbox result protocol mismatch. {_HELPER_REPAIR}")
    stderr = payload["stderr"]
    if (
        policy is not None
        and policy.get("platform") == "macos"
        and payload["exit_code"] == 71
        and "sandbox_apply: Operation not permitted" in stderr
    ):
        raise RuntimeError("workspace sandbox could not be entered: Operation not permitted")
    return CommandExecutionResult(
        command=payload["command"],
        exit_code=payload["exit_code"],
        stdout=payload["stdout"],
        stderr=stderr,
        timed_out=payload["timed_out"],
        timeout_seconds=payload["timeout_seconds"],
        stdout_spool=_spool_from_payload(payload["stdout_spool"]),
        stderr_spool=_spool_from_payload(payload["stderr_spool"]),
    )


def _sandbox_executable() -> Path:
    packaged = Path(__file__).parents[1] / "bin" / (
        "nosis-sandbox.exe" if os.name == "nt" else "nosis-sandbox"
    )
    if packaged.is_file():
        return packaged
    source = Path(__file__).parents[2] / "nosis-sandbox" / "target" / "release" / (
        "nosis-sandbox.exe" if os.name == "nt" else "nosis-sandbox"
    )
    if source.is_file():
        return source
    raise RuntimeError(
        "nosis-sandbox is not built; install a platform wheel or run "
        "cargo build --release --manifest-path nosis-sandbox/Cargo.toml"
    )


def _spool_from_payload(value: dict[str, object] | None) -> CommandOutputSpool | None:
    if value is None:
        return None
    spool = CommandOutputSpool(
        Path(value["path"]), int(value["size_chars"]), str(value["encoding"])
    )
    _SPOOL_PATHS.add(spool.path)
    return spool


def _cleanup_registered_spools() -> None:
    for path in tuple(_SPOOL_PATHS):
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            continue
        _SPOOL_PATHS.discard(path)


def command_argv(command: str | tuple[str, ...], working_directory: Path) -> list[str]:
    return list(command) if isinstance(command, tuple) else platform_shell_launcher(command, working_directory)


def command_display(command: str | tuple[str, ...]) -> str:
    return subprocess.list2cmdline(command) if isinstance(command, tuple) else command


def platform_shell_launcher(command: str, working_directory: Path) -> list[str]:
    """Build the platform shell argv; execution scope never selects it."""
    if os.name == "nt":
        return _windows_powershell_argv(command, working_directory)
    return ["/bin/sh", "-c", command]


def _windows_shell_environment(environment: dict[str, str]) -> dict[str, str]:
    environment.pop("TERM", None)
    environment["NO_COLOR"] = "1"
    return environment


def _windows_powershell_argv(command: str, working_directory: Path) -> list[str]:
    location = str(working_directory).replace("'", "''")
    return [
        _powershell_7(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
        "$ErrorActionPreference = 'Stop'\n"
        "Set-StrictMode -Version Latest\n"
        "$PSNativeCommandUseErrorActionPreference = $true\n"
        f"Set-Location -LiteralPath '{location}'\n" + command,
    ]


def _powershell_7() -> str:
    candidates: list[str | None] = []
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(str(Path(program_files) / "PowerShell" / "7" / "pwsh.exe"))
    candidates.append(shutil.which("pwsh.exe"))
    for candidate in candidates:
        if candidate is not None:
            executable = Path(candidate).resolve()
            if executable.is_file():
                return str(executable)
    raise RuntimeError("Windows shell requires PowerShell 7 (pwsh.exe)")


def _read_text_excerpt(path: Path, limit: int, encoding: str, errors: str) -> TextExcerpt:
    head: list[str] = []
    tail: deque[str] = deque(maxlen=limit)
    size_chars = 0
    with path.open("r", encoding=encoding, errors=errors, newline="") as file:
        while chunk := file.read(8192):
            size_chars += len(chunk)
            if len(head) < limit:
                head.extend(chunk[: limit - len(head)])
            tail.extend(chunk)
    return TextExcerpt("".join(head), "".join(tail), size_chars)


atexit.register(_cleanup_registered_spools)

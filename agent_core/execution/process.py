import atexit
import base64
import json
import locale
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..turn_control import AgentCancelled


DEFAULT_COMMAND_TIMEOUT_SECONDS = 60
MAX_COMMAND_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_COMMAND_OUTPUT_CHARS = 50 * 1024
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


class CancellationSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...


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
    cancel_descriptor, cancel_name = tempfile.mkstemp(prefix="nosis-cancel-")
    os.close(cancel_descriptor)
    cancel_file = Path(cancel_name)
    cancel_file.unlink()
    request["cancel_file"] = str(cancel_file)
    try:
        process = subprocess.Popen(
            [str(helper)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    except BaseException:
        cancel_file.unlink(missing_ok=True)
        raise
    assert process.stdin is not None
    process.stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8"))
    process.stdin.close()
    output: dict[str, bytes] = {}
    readers = [
        threading.Thread(target=lambda key=key, stream=stream: output.__setitem__(key, stream.read()), daemon=True)
        for key, stream in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for reader in readers:
        reader.start()
    try:
        while process.poll() is None:
            if cancellation is not None and cancellation.cancelled:
                cancel_file.touch()
            time.sleep(0.05)
        for reader in readers:
            reader.join()
    finally:
        cancel_file.unlink(missing_ok=True)
    if process.returncode:
        if cancellation is not None and cancellation.cancelled:
            raise CommandCancelled
        diagnostic = output["stderr"].decode("utf-8", errors="replace").strip()
        raise RuntimeError(diagnostic or f"nosis-sandbox exited with status {process.returncode}")

    payload = json.loads(output["stdout"])
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

import os
import json
from pathlib import Path
from typing import Callable

from ...execution import (
    MAX_COMMAND_TIMEOUT_SECONDS,
    CommandExecutionResult,
    CommandOutputSpool,
)
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext


WINDOWS_SHELL_NOTE = (
    "Commands run through Git Bash, so use POSIX shell syntax: chain "
    "commands with ';' or '&&', and tools such as cat, grep, sed and "
    "printf are available."
)
POSIX_SHELL_NOTE = (
    "Commands run through /bin/sh, so use POSIX shell syntax: chain "
    "commands with ';' or '&&'."
)


def _describe(default_timeout_seconds: int) -> str:
    shell_note = (
        WINDOWS_SHELL_NOTE if os.name == "nt" else POSIX_SHELL_NOTE
    )
    return (
        "Execute a shell command with the workspace as the current "
        f"directory. {shell_note} Every call starts a fresh shell, so "
        "directory and environment changes do not persist. The command is "
        f"killed after {default_timeout_seconds} seconds, and its exit "
        "code, stdout and stderr are returned."
    )


class ShellTool(Tool):
    name = "shell"

    def available(self, context: ToolExecutionContext) -> bool:
        return context.command_executor is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        default_timeout_seconds = _default_timeout(context)
        return ToolDefinition(
            name=self.name,
            description=_describe(default_timeout_seconds),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "Shell command to execute with the workspace as "
                            "the current directory."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": default_timeout_seconds,
                        "description": (
                            "Optional timeout in seconds, at most the "
                            "configured default of "
                            f"{default_timeout_seconds}."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue | ToolOutput:
        default_timeout_seconds = _default_timeout(context)
        command = arguments.get("command")
        timeout_seconds = arguments.get(
            "timeout_seconds",
            default_timeout_seconds,
        )
        if not isinstance(command, str) or not command:
            raise ValueError("shell requires a non-empty string 'command'")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds < 1
            or timeout_seconds > MAX_COMMAND_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "shell requires 'timeout_seconds' to be an integer "
                f"between 1 and {MAX_COMMAND_TIMEOUT_SECONDS}"
            )
        if timeout_seconds > default_timeout_seconds:
            raise ValueError(
                "shell 'timeout_seconds' cannot exceed the configured "
                f"default of {default_timeout_seconds} seconds"
            )
        if not set(arguments) <= {"command", "timeout_seconds"}:
            raise ValueError(
                "shell accepts only 'command' and 'timeout_seconds'"
            )
        executor = context.command_executor
        if executor is None:
            raise ValueError("this runtime has no command executor")
        execution = executor.execute(command, timeout_seconds)
        output = {
            "command": execution.command,
            "exit_code": execution.exit_code,
            "stdout": execution.stdout,
            "stderr": execution.stderr,
            "timed_out": execution.timed_out,
            "timeout_seconds": execution.timeout_seconds,
        }
        stdout_spool = execution.stdout_spool
        stderr_spool = execution.stderr_spool
        if stdout_spool is None and stderr_spool is None:
            return output

        writer = _shell_artifact_writer(
            execution,
            stdout_spool,
            stderr_spool,
        )
        return ToolOutput(
            output=output,
            artifact_writer=writer,
            artifact_cleanup=_cleanup_spools(stdout_spool, stderr_spool),
        )


def _default_timeout(context: ToolExecutionContext) -> int:
    timeout_seconds = context.shell_timeout_seconds
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds < 1
        or timeout_seconds > MAX_COMMAND_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "default shell timeout must be an integer between 1 and "
            f"{MAX_COMMAND_TIMEOUT_SECONDS} seconds"
        )
    return timeout_seconds


def _cleanup_spools(
    stdout_spool: CommandOutputSpool | None,
    stderr_spool: CommandOutputSpool | None,
) -> Callable[[], None]:
    def cleanup() -> None:
        if stdout_spool is not None:
            stdout_spool.cleanup()
        if stderr_spool is not None:
            stderr_spool.cleanup()

    return cleanup


def _shell_artifact_writer(
    execution: CommandExecutionResult,
    stdout_spool: CommandOutputSpool | None,
    stderr_spool: CommandOutputSpool | None,
) -> Callable[[Path], int]:
    """Stream the complete shell result as the canonical ToolResult JSON."""

    def write(path: Path) -> int:
        size_chars = 0
        with path.open("w", encoding="utf-8", newline="\n") as file:
            size_chars += _write_text(file, '{"ok": true, "output": {')
            size_chars += _write_json_value(file, "command", execution.command)
            size_chars += _write_json_value(file, "exit_code", execution.exit_code)
            size_chars += _write_json_stream_value(
                file, "stdout", stdout_spool, execution.stdout
            )
            size_chars += _write_json_stream_value(
                file, "stderr", stderr_spool, execution.stderr
            )
            size_chars += _write_json_value(file, "timed_out", execution.timed_out)
            size_chars += _write_json_value(
                file, "timeout_seconds", execution.timeout_seconds
            )
            size_chars += _write_text(file, "}}")
        return size_chars

    return write


def _write_text(file, value: str) -> int:
    file.write(value)
    return len(value)


def _write_json_value(file, key: str, value: object) -> int:
    prefix = ", " if file.tell() > len('{"ok": true, "output": {') else ""
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = f"{prefix}{json.dumps(key)}: {rendered}"
    file.write(text)
    return len(text)


def _write_json_stream_value(
    file,
    key: str,
    spool: CommandOutputSpool | None,
    preview: str,
) -> int:
    prefix = ", " if file.tell() > len('{"ok": true, "output": {') else ""
    key_text = f"{prefix}{json.dumps(key)}: "
    file.write(key_text)
    size_chars = len(key_text)
    if spool is None:
        rendered = json.dumps(preview, ensure_ascii=False)
        file.write(rendered)
        return size_chars + len(rendered)

    file.write('"')
    size_chars += 1
    with spool.path.open(
        "r", encoding=spool.encoding, errors="replace", newline=""
    ) as source:
        for chunk in iter(lambda: source.read(8192), ""):
            escaped = json.dumps(chunk, ensure_ascii=False)[1:-1]
            file.write(escaped)
            size_chars += len(escaped)
    file.write('"')
    return size_chars + 1

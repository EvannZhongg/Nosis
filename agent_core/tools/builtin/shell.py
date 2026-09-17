import os
import json
from pathlib import Path
from typing import Callable

from ...execution import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    CommandExecutionResult,
    CommandOutputSpool,
)
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext


# Shell timeout policy is fixed for every runtime: the default is what a call
# gets when it omits ``timeout_seconds``, and the maximum is the longest a
# call may ask for.  Both are the executor's bounds, so the schema cannot
# advertise a timeout the executor would refuse.
DEFAULT_SHELL_TIMEOUT_SECONDS = DEFAULT_COMMAND_TIMEOUT_SECONDS
MAX_SHELL_TIMEOUT_SECONDS = MAX_COMMAND_TIMEOUT_SECONDS


WINDOWS_SHELL_NOTE = (
    "Commands run through Git Bash, so use POSIX shell syntax: chain "
    "commands with ';' or '&&', and tools such as cat, grep, sed and "
    "printf are available."
)
POSIX_SHELL_NOTE = (
    "Commands run through /bin/sh, so use POSIX shell syntax: chain "
    "commands with ';' or '&&'."
)


def _describe() -> str:
    shell_note = (
        WINDOWS_SHELL_NOTE if os.name == "nt" else POSIX_SHELL_NOTE
    )
    return (
        "Execute a shell command with the workspace as the current "
        f"directory. {shell_note} Every call starts a fresh shell, so "
        "directory and environment changes do not persist. The command is "
        f"killed after {DEFAULT_SHELL_TIMEOUT_SECONDS} seconds by default; "
        f"the timeout may be set up to {MAX_SHELL_TIMEOUT_SECONDS} "
        "seconds. Its exit code, stdout and stderr are returned."
    )


class ShellTool(Tool):
    name = "shell"

    def available(self, context: ToolExecutionContext) -> bool:
        return context.command_executor is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        properties: dict[str, JSONValue] = {
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
                "maximum": MAX_SHELL_TIMEOUT_SECONDS,
                "description": (
                    "Optional timeout in seconds. Defaults to "
                    f"{DEFAULT_SHELL_TIMEOUT_SECONDS} seconds, "
                    "with a maximum of "
                    f"{MAX_SHELL_TIMEOUT_SECONDS}."
                ),
            },
        }
        if context.jobs is not None:
            properties["background"] = {
                "type": "boolean",
                "description": (
                    "Run without blocking this tool batch. The call returns "
                    "a job handle and the completed result is delivered "
                    "automatically later in the same turn."
                ),
                "default": False,
            }
        return ToolDefinition(
            name=self.name,
            description=_describe(),
            parameters={
                "type": "object",
                "properties": properties,
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue | ToolOutput:
        command = arguments.get("command")
        timeout_seconds = arguments.get(
            "timeout_seconds",
            DEFAULT_SHELL_TIMEOUT_SECONDS,
        )
        background = arguments.get("background", False)
        if not isinstance(command, str) or not command:
            raise ValueError("shell requires a non-empty string 'command'")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds < 1
            or timeout_seconds > MAX_SHELL_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "shell requires 'timeout_seconds' to be an integer "
                f"between 1 and {MAX_SHELL_TIMEOUT_SECONDS}"
            )
        if not isinstance(background, bool):
            raise ValueError("shell requires 'background' to be a boolean")
        if not set(arguments) <= {"command", "timeout_seconds", "background"}:
            raise ValueError(
                "shell accepts only 'command', 'timeout_seconds' and 'background'"
            )
        executor = context.command_executor
        if executor is None:
            raise ValueError("this runtime has no command executor")
        if background:
            jobs = context.jobs
            turn_id = context.session.current_turn_id
            if jobs is None or turn_id is None:
                raise ValueError("background shell requires an active JobManager turn")
            handle = jobs.submit(
                "shell",
                turn_id,
                lambda cancellation: _command_output(
                    executor.execute(
                        command,
                        timeout_seconds,
                        cancellation=cancellation,
                    )
                ),
            )
            return handle.to_dict()
        execution = (
            executor.execute(command, timeout_seconds)
            if context.cancellation is None
            else executor.execute(
                command,
                timeout_seconds,
                cancellation=context.cancellation,
            )
        )
        return _command_output(execution)


def _command_output(execution: CommandExecutionResult) -> JSONValue | ToolOutput:
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

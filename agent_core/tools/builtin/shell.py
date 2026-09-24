import os
from pathlib import Path
from typing import Callable

from ...execution.authority import ExecutionScope
from ...execution.process import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    CommandExecutionResult,
    CommandOutputSpool,
)
from ...execution.sandbox.base import FilesystemAccess, NetworkAccess
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext


# Shell timeout policy is fixed for every runtime: the default is what a call
# gets when it omits ``timeout_seconds``, and the maximum is the longest a
# call may ask for.  Both are the executor's bounds, so the schema cannot
# advertise a timeout the executor would refuse.
DEFAULT_SHELL_TIMEOUT_SECONDS = DEFAULT_COMMAND_TIMEOUT_SECONDS
MAX_FOREGROUND_SHELL_TIMEOUT_SECONDS = 15 * 60
MAX_BACKGROUND_SHELL_TIMEOUT_SECONDS = MAX_COMMAND_TIMEOUT_SECONDS


WINDOWS_SHELL_NOTE = (
    "On Windows, every scope runs through PowerShell 7. Shell dialect is "
    "independent of execution scope. Use PowerShell syntax for commands; "
    "running POSIX .sh scripts is outside the Windows shell contract."
)
POSIX_SHELL_NOTE = (
    "Commands run through /bin/sh, so use POSIX shell syntax: chain "
    "commands with ';' or '&&'."
)


class ShellTool(Tool):
    name = "shell"

    def available(self, context: ToolExecutionContext) -> bool:
        return context.execution_router is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        supports_background = context.jobs is not None
        maximum_timeout = (
            MAX_BACKGROUND_SHELL_TIMEOUT_SECONDS
            if supports_background
            else MAX_FOREGROUND_SHELL_TIMEOUT_SECONDS
        )
        shell_note = (
            WINDOWS_SHELL_NOTE if os.name == "nt" else POSIX_SHELL_NOTE
        )
        timeout_note = (
            "Foreground calls may run for up to "
            f"{MAX_FOREGROUND_SHELL_TIMEOUT_SECONDS} seconds; calls with "
            "background=true may run for up to "
            f"{MAX_BACKGROUND_SHELL_TIMEOUT_SECONDS} seconds."
            if supports_background
            else "Calls may run for up to "
            f"{MAX_FOREGROUND_SHELL_TIMEOUT_SECONDS} seconds."
        )
        timeout_description = (
            "Optional timeout in seconds. Defaults to "
            f"{DEFAULT_SHELL_TIMEOUT_SECONDS} seconds, with a foreground "
            f"maximum of {MAX_FOREGROUND_SHELL_TIMEOUT_SECONDS}. Values up "
            f"to {MAX_BACKGROUND_SHELL_TIMEOUT_SECONDS} require "
            "background=true."
            if supports_background
            else "Optional timeout in seconds. Defaults to "
            f"{DEFAULT_SHELL_TIMEOUT_SECONDS} seconds, with a maximum of "
            f"{MAX_FOREGROUND_SHELL_TIMEOUT_SECONDS}."
        )
        router = context.execution_router
        if router is None:
            raise RuntimeError("shell execution router is unavailable")
        workspace_policy = router.workspace_policy
        authority = router.authority
        default_scope = authority.default_scope
        filesystem_description = (
            "Workspace scope primarily grants writes to the workspace and "
            "private temporary directory; host files remain readable. On "
            "Windows, the restricted token is a best-effort write boundary: "
            "locations already writable by Everyone are an exception. "
            if getattr(workspace_policy, "host_filesystem", None)
            is FilesystemAccess.WRITE_RESTRICTED
            else "Workspace scope is confined to the workspace. "
        )
        network_description = (
            "Network access remains available. "
            if getattr(workspace_policy, "network", None)
            is NetworkAccess.ALLOW
            else "Network access is disabled. "
        )
        workspace_description = filesystem_description + network_description
        scope_guidance = (
            "Use explicit workspace scope to run with reduced authority. "
            if default_scope is ExecutionScope.HOST
            else (
                "Request host scope only when the command must cross that "
                "boundary. "
                if authority.allows(ExecutionScope.HOST)
                else "Host scope is not available to this Agent. "
            )
        )
        description = (
            "Execute a shell command with the workspace as the current "
            f"directory. {default_scope.value.title()} scope is the default. "
            f"{workspace_description}"
            f"{scope_guidance}"
            f"{shell_note} Every call starts a fresh shell, so directory and "
            "environment changes do not persist. The command is killed after "
            f"{DEFAULT_SHELL_TIMEOUT_SECONDS} seconds by default. "
            f"{timeout_note} Its exit code, stdout and stderr are returned."
        )
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
                "maximum": maximum_timeout,
                "description": timeout_description,
            },
            "scope": {
                "type": "string",
                "enum": [scope.value for scope in authority.scopes],
                "description": (
                    "Execution boundary. 'workspace' uses the platform's "
                    "workspace sandbox; 'host' runs with the current "
                    "user's host access and may require approval."
                ),
                "default": default_scope.value,
            },
        }
        if supports_background:
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
            description=description,
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
        if not isinstance(background, bool):
            raise ValueError("shell requires 'background' to be a boolean")
        maximum_timeout = (
            MAX_BACKGROUND_SHELL_TIMEOUT_SECONDS
            if background
            else MAX_FOREGROUND_SHELL_TIMEOUT_SECONDS
        )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds < 1
            or timeout_seconds > maximum_timeout
        ):
            raise ValueError(
                "shell requires 'timeout_seconds' to be an integer "
                f"between 1 and {maximum_timeout}"
            )
        if not set(arguments) <= {
            "command",
            "timeout_seconds",
            "background",
            "scope",
        }:
            raise ValueError(
                "shell accepts only 'command', 'timeout_seconds', "
                "'background' and 'scope'"
            )
        route = context.execution
        if route is None or route.scope is None or route.executor is None:
            raise RuntimeError("shell execution was not resolved")
        executor = route.executor
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
    """Stream the complete shell result as plain text.

    The streams are written verbatim rather than JSON-escaped: an
    artifact is paged back through ``read_file``, and escaping would
    fold the whole run onto a single unreadable line.
    """

    def write(path: Path) -> int:
        size_chars = 0
        with path.open("w", encoding="utf-8", newline="\n") as file:
            size_chars += _write_text(file, f"$ {execution.command}\n")
            size_chars += _write_text(
                file, f"exit_code: {execution.exit_code}\n"
            )
            size_chars += _write_text(
                file, f"timed_out: {str(execution.timed_out).lower()}\n"
            )
            if execution.timeout_seconds is not None:
                size_chars += _write_text(
                    file,
                    f"timeout_seconds: {execution.timeout_seconds}\n",
                )
            size_chars += _write_stream(
                file, "stdout", stdout_spool, execution.stdout
            )
            size_chars += _write_stream(
                file, "stderr", stderr_spool, execution.stderr
            )
        return size_chars

    return write


def _write_text(file, value: str) -> int:
    file.write(value)
    return len(value)


def _write_stream(
    file,
    name: str,
    spool: CommandOutputSpool | None,
    preview: str,
) -> int:
    """Write one stream verbatim, from its spool when it has one."""
    size_chars = _write_text(file, f"\n--- {name} ---\n")
    if spool is None:
        return size_chars + _write_text(file, preview)

    with spool.path.open(
        "r", encoding=spool.encoding, errors="replace", newline=""
    ) as source:
        for chunk in iter(lambda: source.read(8192), ""):
            size_chars += _write_text(file, chunk)
    return size_chars

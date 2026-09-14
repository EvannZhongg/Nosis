import os
from dataclasses import asdict

from ...execution import MAX_COMMAND_TIMEOUT_SECONDS
from ..base import JSONValue, Tool, ToolDefinition
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
    ) -> JSONValue:
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
        return asdict(executor.execute(command, timeout_seconds))


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

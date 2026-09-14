from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..execution import DEFAULT_COMMAND_TIMEOUT_SECONDS
from ..session_paths import default_sessions_directory
from ..workspace import Workspace

if TYPE_CHECKING:
    from ..execution import CommandExecutor
    from ..llm import LLMProvider
    from ..mcp.manager import McpClientManager
    from ..session import Session
    from ..subagent import SubagentRuntime


@dataclass(frozen=True)
class ToolExecutionContext:
    """The Runtime dependencies handed to every Tool call.

    The Runtime builds one context per Agent: the heavy dependencies are
    the Runtime's own singletons, while ``session`` names the Agent the
    call belongs to. Everything here is read-only for the duration of a
    call, so parallel invocations of one shared Tool stay isolated.

    A dependency left as ``None`` makes the tools that need it unavailable
    rather than failing at call time; see :meth:`Tool.available`.
    """

    workspace: Workspace
    session: "Session"
    sessions_directory: Path = field(
        default_factory=default_sessions_directory
    )
    command_executor: "CommandExecutor | None" = None
    shell_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS
    vision_provider: "LLMProvider | None" = None
    mcp: "McpClientManager | None" = None
    subagents: "SubagentRuntime | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sessions_directory",
            self.sessions_directory.expanduser().resolve(),
        )

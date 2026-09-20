from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..session_paths import default_sessions_directory
from ..workspace import Workspace
from .base import JSONValue

if TYPE_CHECKING:
    from ..execution import CommandExecutor
    from ..llm import LLMProvider
    from ..mcp.manager import McpClientManager
    from ..plan import PlanManager
    from ..session import Session
    from ..skills import SkillRegistry
    from ..subagent import SubagentRuntime
    from ..jobs import JobManager
    from ..jobs import CancellationToken


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
    workspace_command_executor: "CommandExecutor | None" = None
    host_command_executor: "CommandExecutor | None" = None
    max_generation_tokens: int | None = None
    # Whether the Agent's own model accepts image input.  It decides
    # which of the two image tools this Agent gets: the model either sees
    # images itself via ``read_image``, or delegates to the vision
    # provider via ``analyze_image``.
    vision_input: bool = False
    vision_provider: "LLMProvider | None" = None
    mcp: "McpClientManager | None" = None
    subagents: "SubagentRuntime | None" = None
    jobs: "JobManager | None" = None
    cancellation: "CancellationToken | None" = None
    skills: "SkillRegistry | None" = None
    plan: "PlanManager | None" = None
    ask_user: (
        Callable[[str, list[dict[str, JSONValue]], bool], JSONValue] | None
    ) = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sessions_directory",
            self.sessions_directory.expanduser().resolve(),
        )

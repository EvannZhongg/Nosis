"""Application notifications and optional interactive capabilities."""

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

from agent_core import AgentEvent, PlanSnapshot
if TYPE_CHECKING:
    from agent_core.mcp.manager import McpServerStatus


@dataclass(frozen=True)
class RuntimeCallbacks:
    on_event: Callable[[AgentEvent, str], None] = lambda event, turn_id: None
    on_phase: Callable[[str, tuple[str, ...]], None] = lambda phase, warnings: None
    on_plan: Callable[[PlanSnapshot], None] = lambda snapshot: None
    on_session_title: Callable[[str, str, str], None] = lambda session_id, title, source: None
    on_mcp_status: Callable[["McpServerStatus"], None] = lambda status: None
    request_permission: Callable[..., bool] = lambda command, **kwargs: False
    ask_user: Callable[[str, list[dict[str, object]], bool], object] | None = None

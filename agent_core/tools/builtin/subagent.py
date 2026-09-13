"""Tool for delegating a task to an independent Agent session."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from ...tools.config import ToolConfig
from ...llm import LLMProvider
from ...prompts import load_subagent_prompt
from ...session import Session
from ...session_store import JsonlSessionStore
from ...session_paths import session_directory
from ...session_paths import default_sessions_directory
from ...workspace import Workspace
from ...content import ImagePart
from ..base import JSONValue, Tool, ToolDefinition
from ..base import ToolPolicy
if TYPE_CHECKING:
    from ...config import AgentConfig


class SubagentTool(Tool):
    """Run a complete child-agent loop and return only its final answer.

    A fresh :class:`Session` is created for every invocation.  The complete
    child transcript is persisted under the parent session's ``subagents``
    directory, while the parent receives a plain string containing the final
    assistant response.
    """

    def __init__(
        self,
        provider: LLMProvider | Callable[[], LLMProvider],
        config: AgentConfig,
        workspace: Workspace,
        *,
        tools: Iterable[Tool] = (),
        system_prompt: str | None = None,
        sessions_directory: Path | None = None,
        parent_session_id: str | None = None,
        parent_session: Session | None = None,
        tool_policy: ToolPolicy | None = None,
    ) -> None:
        self._provider = provider
        self._config = config
        self._workspace = workspace
        self._tools = tuple(tools)
        self._system_prompt = system_prompt
        if sessions_directory is None:
            if not parent_session_id:
                raise ValueError(
                    "parent_session_id is required when sessions_directory is not provided"
                )
            sessions_directory = default_sessions_directory()
        sessions_root = sessions_directory.expanduser().resolve()
        # Keep artifacts addressable from the shared Session root; only the
        # child transcript is nested under its parent's ``subagents`` folder.
        self._artifact_sessions_directory = sessions_root
        if parent_session_id:
            sessions_directory = (
                session_directory(
                    sessions_root,
                    parent_session_id,
                )
                / "subagents"
            )
        self._store = JsonlSessionStore(sessions_directory)
        self._tool_policy = tool_policy
        self._parent_session = parent_session

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="subagent",
            description=(
                "Delegate a task to an independent sub-agent. "
                "Returns only the sub-agent's final report."
            ),
            parameters={
                "type": "object",
                "properties": {"task": {"type": "string"}},
                "required": ["task"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, JSONValue]) -> JSONValue:
        task = arguments.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("subagent task must be a non-empty string")

        provider = self._provider() if callable(self._provider) else self._provider
        from ...agent import Agent
        # Deliberately do not pass the parent session: child context is fully
        # isolated and receives only the task description as user input.
        session = Session()
        from dataclasses import replace
        child_config = replace(
            self._config,
            tools=self._config.subagent_tools,
            subagent_tools=ToolConfig(enabled=frozenset()),
        )
        child = Agent(
            provider=provider,
            session=session,
            system_prompt=self._system_prompt or load_subagent_prompt(self._workspace),
            config=child_config,
            workspace=self._workspace,
            tools=self._tools,
            tool_policy=self._tool_policy,
            sessions_directory=self._artifact_sessions_directory,
        )
        attachments: tuple[ImagePart, ...] = ()
        if self._parent_session is not None:
            for item in reversed(self._parent_session.items):
                if item.role != "user":
                    continue
                attachments = tuple(
                    part for part in item.parts if isinstance(part, ImagePart)
                )
                break
        result = child.run(task.strip(), attachments=attachments)
        context_fields = {}
        if session.archived_summary is not None:
            context_fields = {
                "archived_summary": session.archived_summary,
                "archived_item_count": session.archived_item_count,
            }
        self._store.append_turn(
            session.session_id,
            result.request,
            result.response,
            result.items,
            **context_fields,
        )
        # Parent receives only the final assistant response, never child
        # reasoning or intermediate tool calls.
        return result.response.content or ""


class SubagentRegistry:
    """Registry for extensible sub-agent tool implementations."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"subagent '{name}' is already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def tools(self) -> tuple[Tool, ...]:
        return tuple(self._tools.values())

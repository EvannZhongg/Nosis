"""Sub-agent roles and the Runtime that runs them.

A role is a name, a description the parent model reads, and the set of
Tool names the role may use. Roles share the Runtime's Tool Catalog, so
adding one costs a config entry rather than a new Tool implementation.
"""
from dataclasses import dataclass, replace
from typing import Iterable

from .config import AgentConfig
from .content import ImagePart
from .llm import LLMProvider
from .prompts import load_subagent_prompt
from .session import Session
from .session_paths import session_directory
from .session_store import JsonlSessionStore
from .tools.catalog import ToolCatalog
from .tools.context import ToolExecutionContext
from .tools.base import ToolPolicy


@dataclass(frozen=True)
class SubagentRole:
    name: str
    description: str
    tools: frozenset[str]


class SubagentRoleRegistry:
    """The Runtime's roles, keyed by name and ordered for the schema."""

    def __init__(self, roles: Iterable[SubagentRole] = ()) -> None:
        self._roles: dict[str, SubagentRole] = {}
        for role in roles:
            if role.name in self._roles:
                raise ValueError(
                    f"subagent role '{role.name}' is already registered"
                )
            self._roles[role.name] = role

    def __bool__(self) -> bool:
        return bool(self._roles)

    def __iter__(self):
        return iter(self._roles.values())

    def get(self, name: str) -> SubagentRole:
        role = self._roles.get(name)
        if role is None:
            known = ", ".join(self._roles) or "none"
            raise ValueError(
                f"unknown subagent role '{name}'; available roles: {known}"
            )
        return role


class SubagentRuntime:
    """Runs a child Agent for a role, isolated from the parent.

    The Runtime owns only Runtime-level dependencies. Everything that
    belongs to one invocation — which Session is the parent, which
    workspace, where transcripts go — arrives with the parent's
    :class:`ToolExecutionContext`, so a single instance is shared by the
    main Agent and by parallel sub-agent calls.
    """

    def __init__(
        self,
        provider: LLMProvider,
        config: AgentConfig,
        catalog: ToolCatalog,
        roles: SubagentRoleRegistry,
        *,
        vision_provider: LLMProvider | None = None,
        tool_policy: ToolPolicy | None = None,
    ) -> None:
        self._provider = provider
        self._config = config
        self._catalog = catalog
        self._roles = roles
        self._vision_provider = vision_provider
        self._tool_policy = tool_policy

    @property
    def roles(self) -> SubagentRoleRegistry:
        return self._roles

    def run(
        self,
        role_name: str,
        task: str,
        parent: ToolExecutionContext,
    ) -> str:
        from .agent import Agent

        role = self._roles.get(role_name)
        session = Session()
        # A child never reaches the sub-agent runtime, so it cannot spawn
        # a grandchild: recursion is bounded by the context, not by
        # rewriting the child's tool configuration.
        context = replace(
            parent,
            session=session,
            vision_provider=self._vision_provider,
            subagents=None,
        )
        child = Agent(
            provider=self._provider,
            session=session,
            system_prompt=load_subagent_prompt(parent.workspace, role),
            config=self._config,
            tools=self._catalog.select(
                role.tools,
                context,
                policy=self._tool_policy,
            ),
            context=context,
        )
        # The child receives only the task text plus whatever the parent
        # was last shown, never the parent's transcript.
        result = child.run(task, attachments=_latest_attachments(parent.session))
        self._store(parent).append_turn(
            session.session_id,
            result.request,
            result.response,
            result.items,
            workspace=parent.workspace.path,
            **_archive_fields(session),
        )
        # The parent sees the final report only: no child reasoning and
        # no intermediate tool calls.
        return result.response.content or ""

    def _store(self, parent: ToolExecutionContext) -> JsonlSessionStore:
        """Nest the child transcript under the parent session.

        Keeping it out of the sessions root keeps child transcripts out of
        the user-visible session list. That root already belongs to the
        parent's workspace, so it is not grouped by workspace again.
        """
        return JsonlSessionStore(
            session_directory(
                parent.sessions_directory,
                parent.workspace.path,
                parent.session.session_id,
            )
            / "subagents",
            group_by_workspace=False,
        )


def _latest_attachments(session: Session) -> tuple[ImagePart, ...]:
    for item in reversed(session.items):
        if item.role == "user":
            return tuple(
                part for part in item.parts if isinstance(part, ImagePart)
            )
    return ()


def _archive_fields(session: Session) -> dict[str, object]:
    if session.archived_summary is None:
        return {}
    return {
        "archived_summary": session.archived_summary,
        "archived_item_count": session.archived_item_count,
    }

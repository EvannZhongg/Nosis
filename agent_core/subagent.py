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
from .skills import SkillRegistry
from .tools.catalog import ToolCatalog
from .tools.context import ToolExecutionContext
from .tools.base import ToolPolicy
from .tools.builtin import AnalyzeImageTool, ReadImageTool, ReadSkillTool


def vision_aware_tool_names(
    configured: Iterable[str],
    provider: LLMProvider,
    vision_provider: LLMProvider | None,
) -> tuple[str, ...]:
    """Add the image tool this agent can actually use, if any.

    The two image tools are alternatives, never both. A vision-capable
    model gets ``read_image`` and looks at the pixels itself. A text-only
    model has images stripped from its context, so its only route is
    ``analyze_image``, which spends a second provider call turning them
    into text -- and that requires the Runtime to have resolved a vision
    provider to hand them to.
    """
    names = tuple(configured)
    if "image" in provider.capabilities.input_modalities:
        return (*names, ReadImageTool.name)
    if vision_provider is None:
        return names
    return (*names, AnalyzeImageTool.name)


def skill_aware_tool_names(
    configured: Iterable[str],
    skills: SkillRegistry | None,
) -> tuple[str, ...]:
    """Add progressive skill loading when the Runtime found skills."""
    names = tuple(configured)
    if not skills:
        return names
    return (*names, ReadSkillTool.name)


@dataclass(frozen=True)
class SubagentRole:
    """A sub-agent role: its purpose, its tools, and the models it runs on.

    Each role carries its own providers, so one runtime can host a cheap
    read-only role and an expensive editing role side by side.
    """

    name: str
    description: str
    tools: tuple[str, ...]
    provider: LLMProvider
    vision_provider: LLMProvider | None = None


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

    The Runtime owns only Runtime-level dependencies; each role brings its
    own providers. Everything that belongs to one invocation — which
    Session is the parent, which workspace, where transcripts go — arrives
    with the parent's :class:`ToolExecutionContext`, so a single instance
    is shared by the main Agent and by parallel sub-agent calls.
    """

    def __init__(
        self,
        config: AgentConfig,
        catalog: ToolCatalog,
        roles: SubagentRoleRegistry,
        *,
        tool_policy: ToolPolicy | None = None,
    ) -> None:
        self._config = config
        self._catalog = catalog
        self._roles = roles
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
        store = self._store(parent)
        session.attach_journal_sink(
            lambda events: store.append_events(session.session_id, events)
        )
        # A child never reaches the sub-agent runtime, so it cannot spawn
        # a grandchild: recursion is bounded by the context, not by
        # rewriting the child's tool configuration.
        context = replace(
            parent,
            session=session,
            vision_input=(
                "image" in role.provider.capabilities.input_modalities
            ),
            vision_provider=role.vision_provider,
            subagents=None,
        )
        child = Agent(
            provider=role.provider,
            session=session,
            system_prompt=load_subagent_prompt(
                parent.workspace, role, parent.skills
            ),
            config=self._config,
            tools=self._catalog.select(
                skill_aware_tool_names(
                    vision_aware_tool_names(
                        role.tools, role.provider, role.vision_provider
                    ),
                    parent.skills,
                ),
                context,
                policy=self._tool_policy,
            ),
            context=context,
        )
        # The child receives only the task text plus whatever the parent
        # was last shown, never the parent's transcript.
        result = child.run(task, attachments=_latest_attachments(parent.session))
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
    """Return the images the parent was last shown by the user.

    Only what the person actually attached is inherited. A message the
    runtime synthesized to carry a tool's images belongs to the parent's
    own investigation, and passing it down would both hide the user's
    real attachment and spend the child's context on an image its task
    never mentioned.
    """
    for item in reversed(session.items):
        if item.role == "user" and not item.is_tool_media:
            return tuple(
                part for part in item.parts if isinstance(part, ImagePart)
            )
    return ()

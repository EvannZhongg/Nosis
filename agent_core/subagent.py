"""Sub-agent roles and the Runtime that runs them.

A role is a name, a description the parent model reads, private instructions
for the child, and the set of Tool names the role may use. Roles share the
Runtime's Tool Catalog, so adding one costs configuration rather than a new
Tool implementation.
"""
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Callable, Iterable

from .config import AgentConfig
from .content import AttachmentPart, ImagePart
from .llm import AuxiliaryModelCall, LLMProvider
from .memory import MemoryManager
from .prompting import render_subagent_prompt
from .session import Session
from .session_paths import session_directory
from .session_store import JsonlSessionStore
from .skills import SkillRegistry
from .tool_result import ToolResultNormalizer
from .tools.catalog import ToolCatalog
from .tools.context import ToolExecutionContext
from .tools.base import JSONValue, ToolPolicy
from .tools.builtin import AnalyzeImageTool, ReadImageTool, ReadSkillTool
from .execution.process import CancellationSignal
from .turn_control import TurnControl
from .workspace_instructions import WorkspaceInstructions

if TYPE_CHECKING:
    from .agent import AgentEvent


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
    """A sub-agent role: its purpose, instructions, tools, and providers.

    Each role carries its own providers, so one runtime can host a cheap
    read-only role and an expensive editing role side by side.
    """

    name: str
    description: str
    tools: tuple[str, ...]
    provider: LLMProvider
    instructions: str = ""
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
        subagent_prompt_template: str,
        consolidator_prompt: str,
        workspace_instructions: WorkspaceInstructions,
        *,
        tool_policy: ToolPolicy | None = None,
        consolidator_provider: LLMProvider | None = None,
        on_auxiliary_call: Callable[[str, str, AuxiliaryModelCall], None] | None = None,
        memory_factory: (
            Callable[[Callable[[AuxiliaryModelCall], None] | None], MemoryManager] | None
        ) = None,
    ) -> None:
        self._config = config
        self._catalog = catalog
        self._roles = roles
        self._subagent_prompt_template = subagent_prompt_template
        self._consolidator_prompt = consolidator_prompt
        self._workspace_instructions = workspace_instructions
        self._tool_policy = tool_policy
        self._consolidator_provider = consolidator_provider
        self._on_auxiliary_call = on_auxiliary_call
        self._memory_factory = memory_factory

    @property
    def roles(self) -> SubagentRoleRegistry:
        return self._roles

    def run(
        self,
        role_name: str,
        task: str,
        parent: ToolExecutionContext,
        cancellation: CancellationSignal | None = None,
        *,
        on_progress: Callable[[dict[str, JSONValue]], None] | None = None,
    ) -> str:
        from .agent import Agent

        role = self._roles.get(role_name)
        session = Session()
        on_auxiliary_call = (
            partial(self._on_auxiliary_call, role_name, session.session_id)
            if self._on_auxiliary_call is not None else None
        )
        memory = (
            self._memory_factory(on_auxiliary_call)
            if self._memory_factory is not None else None
        )
        store = self._store(parent)
        child_session_directory = store.directory / session.session_id
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
            jobs=None,
            plan=None,
            memory=memory,
            cancellation=cancellation,
            execution=None,
        )
        child = Agent(
            provider=role.provider,
            session=session,
            system_prompt=render_subagent_prompt(
                self._subagent_prompt_template,
                parent.workspace,
                role,
                parent.skills,
                instructions=self._workspace_instructions,
            ),
            consolidator_prompt=self._consolidator_prompt,
            consolidator_provider=self._consolidator_provider,
            memory_context=(
                memory.store.load(parent.workspace.path) if memory is not None else None
            ),
            on_auxiliary_call=on_auxiliary_call,
            config=self._config,
            tools=self._catalog.select(
                (
                    *skill_aware_tool_names(
                        vision_aware_tool_names(
                            role.tools, role.provider, role.vision_provider
                        ),
                        parent.skills,
                    ),
                    *(("remember",) if memory is not None else ()),
                ),
                context,
                policy=self._tool_policy,
            ),
            context=context,
            tool_result_normalizer=ToolResultNormalizer(
                parent.workspace,
                session.session_id,
                sessions_directory=parent.sessions_directory,
                artifact_directory=child_session_directory,
            ),
        )
        # The child receives only the task text plus whatever the parent
        # was last shown, never the parent's transcript.
        with store.open_journal(session.session_id) as sink:
            session.attach_journal_sink(sink)
            try:
                result = child.run(
                    task,
                    on_event=(
                        _SubagentProgress(on_progress) if on_progress is not None else None
                    ),
                    attachments=_latest_attachments(parent.session),
                    turn_control=(
                        TurnControl(external_cancelled=lambda: cancellation.cancelled)
                        if cancellation is not None
                        else None
                    ),
                )
                if memory is not None:
                    memory.reconcile_pending()
            finally:
                session.attach_journal_sink(None)
        # Full report delivery is separate from the bounded progress snapshots.
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


class _SubagentProgress:
    """Project execution events without exposing results or final response text."""

    def __init__(self, publish: Callable[[dict[str, JSONValue]], None]) -> None:
        self._publish = publish
        self._recent_tools: list[dict[str, JSONValue]] = []
        self._pending_text: tuple[int, str] | None = None
        self._text: str | None = None

    def __call__(self, event: "AgentEvent") -> None:
        from .agent import (
            AssistantMessageEvent, ToolBatchStartedEvent, ToolCallEvent, ToolResultEvent,
        )

        if isinstance(event, AssistantMessageEvent):
            # Only a following tool batch proves this is intermediate text.
            # Streaming text and final response text are never published.
            self._pending_text = (event.model_call_index, event.content[:1000])
            return
        if isinstance(event, ToolBatchStartedEvent):
            if (
                self._pending_text is not None
                and self._pending_text[0] == event.model_call_index
            ):
                self._text = self._pending_text[1]
            self._pending_text = None
            return
        elif isinstance(event, ToolCallEvent):
            self._recent_tools.append({
                "tool_call_id": event.tool_call.id,
                "name": event.tool_call.name,
                "status": "running",
            })
            self._recent_tools = self._recent_tools[-5:]
        elif isinstance(event, ToolResultEvent):
            for call in self._recent_tools:
                if call["tool_call_id"] == event.tool_result.tool_call_id:
                    call["status"] = (
                        "failed" if event.tool_result.error is not None else "completed"
                    )
                    break
            else:
                return
        else:
            return
        self._publish({
            "recent_tools": [dict(call) for call in self._recent_tools],
            "text": self._text,
        })


def _latest_attachments(session: Session) -> tuple[AttachmentPart, ...]:
    """Return the attachments the parent was last shown by the user.

    Only what the person actually attached is inherited. A message the
    runtime synthesized to carry a tool's images belongs to the parent's
    own investigation, and passing it down would both hide the user's
    real attachment and spend the child's context on an image its task
    never mentioned.
    """
    for item in reversed(session.items):
        if item.role == "user" and item.origin == "conversation":
            return tuple(
                part for part in item.parts if isinstance(part, AttachmentPart)
            )
    return ()

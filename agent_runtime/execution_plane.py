"""Workspace resources and their replaceable Session binding."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_core import (
    Agent,
    AgentConfig,
    ContextWindow,
    ExecutionRouter,
    JobManager,
    MemoryManager,
    Workspace,
    WorkspaceInstructions,
    SkillRegistry,
    ToolCatalog,
)
from agent_core.llm import LLMProvider
from agent_core.image_generation import ImageGenerator
from agent_core.tools.config import ToolSettings

from .computer import DesktopComputer

if TYPE_CHECKING:
    from .mcp_client import McpClientHandle
    from .config import PromptTemplates
    from .plugins import PluginAgent


@dataclass(eq=False)
class ExecutionPlane:
    """One resource assembly, with fresh Agent state for each Session binding."""

    workspace: Workspace
    provider_name: str
    agent_config: AgentConfig
    configuration_fingerprint: str
    instructions: WorkspaceInstructions
    provider: LLMProvider
    fast_provider: LLMProvider
    vision_provider: LLMProvider | None
    image_generator: ImageGenerator | None
    catalog: ToolCatalog
    skills: SkillRegistry
    prompts: "PromptTemplates"
    plugin_agents: tuple["PluginAgent", ...]
    computer_tools: tuple[str, ...]
    execution_router: ExecutionRouter
    mcp: "McpClientHandle"
    tool_settings: ToolSettings = field(default_factory=ToolSettings)
    memory: MemoryManager | None = None
    agent: Agent | None = None
    jobs: JobManager | None = None
    context_window: ContextWindow | None = None
    computer: DesktopComputer | None = None
    runtime_warnings: tuple[str, ...] = ()

    def matches(
        self,
        *,
        workspace: Workspace,
        provider_name: str,
        agent_config: AgentConfig,
        configuration_fingerprint: str,
        instructions: WorkspaceInstructions,
    ) -> bool:
        return (
            self.workspace == workspace
            and self.provider_name == provider_name
            and self.agent_config == agent_config
            and self.configuration_fingerprint
            == configuration_fingerprint
            and self.instructions.fingerprint == instructions.fingerprint
        )

    def unbind_session(self) -> None:
        jobs = self.jobs
        self.jobs = None
        self.agent = None
        self.memory = None
        self.context_window = None
        if jobs is not None:
            jobs.close()

    def close(self) -> None:
        try:
            if self.computer is not None:
                self.computer.close()
        finally:
            try:
                self.unbind_session()
            finally:
                try:
                    self.mcp.release()
                finally:
                    self.execution_router.close()


__all__ = ["ExecutionPlane"]

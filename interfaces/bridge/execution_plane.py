"""One fully assembled Agent execution lifecycle."""

from dataclasses import dataclass

from agent_core import (
    Agent,
    AgentConfig,
    CommandExecutor,
    ContextWindow,
    JobManager,
    MemoryManager,
    Workspace,
    WorkspaceInstructions,
)
from agent_core.mcp.manager import McpClientManager


@dataclass(eq=False)
class ExecutionPlane:
    """Resources and inputs that make one concrete Agent runtime."""

    workspace: Workspace
    provider_name: str
    agent_config: AgentConfig
    configuration_fingerprint: str
    instructions: WorkspaceInstructions
    memory: MemoryManager | None
    agent: Agent
    workspace_executor: CommandExecutor
    host_executor: CommandExecutor
    jobs: JobManager
    mcp: McpClientManager
    context_window: ContextWindow
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

    def close(self) -> None:
        try:
            self.jobs.close()
        finally:
            try:
                self.mcp.close()
            finally:
                try:
                    self.workspace_executor.close()
                finally:
                    self.host_executor.close()


__all__ = ["ExecutionPlane"]

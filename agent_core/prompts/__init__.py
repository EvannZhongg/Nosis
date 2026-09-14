from importlib.resources import files
from typing import TYPE_CHECKING

from agent_core.workspace import Workspace

if TYPE_CHECKING:
    from agent_core.subagent import SubagentRole


def load_system_prompt(workspace: Workspace) -> str:
    template = (
        files("agent_core.prompts")
        .joinpath("Soul.md")
        .read_text(encoding="utf-8")
    )
    workspace_value = str(workspace.path)
    return template.replace("{{workspace}}", workspace_value).strip()


def load_consolidator_prompt() -> str:
    return (
        files("agent_core.prompts")
        .joinpath("Consolidator.md")
        .read_text(encoding="utf-8")
        .strip()
    )


def load_subagent_prompt(
    workspace: Workspace,
    role: "SubagentRole",
) -> str:
    template = files("agent_core.prompts").joinpath("SubAgent.md").read_text(encoding="utf-8")
    return (
        template.replace("{{workspace}}", str(workspace.path))
        .replace("{{role}}", role.name)
        .replace("{{role_description}}", role.description)
        .strip()
    )


__all__ = ["load_consolidator_prompt", "load_subagent_prompt", "load_system_prompt"]

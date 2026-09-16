from importlib.resources import files
from typing import TYPE_CHECKING

from agent_core.workspace import Workspace

if TYPE_CHECKING:
    from agent_core.skills import SkillRegistry
    from agent_core.subagent import SubagentRole


def load_system_prompt(
    workspace: Workspace,
    skills: "SkillRegistry | None" = None,
) -> str:
    template = (
        files("agent_core.prompts")
        .joinpath("Soul.md")
        .read_text(encoding="utf-8")
    )
    workspace_value = str(workspace.path)
    prompt = template.replace("{{workspace}}", workspace_value).strip()
    return _with_skills(prompt, skills)


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
    skills: "SkillRegistry | None" = None,
) -> str:
    template = files("agent_core.prompts").joinpath("SubAgent.md").read_text(encoding="utf-8")
    prompt = (
        template.replace("{{workspace}}", str(workspace.path))
        .replace("{{role}}", role.name)
        .replace("{{role_description}}", role.description)
        .strip()
    )
    return _with_skills(prompt, skills)


def _with_skills(prompt: str, skills: "SkillRegistry | None") -> str:
    section = skills.prompt_section() if skills else ""
    if not section:
        return prompt
    return f"{prompt}\n\n{section}"


__all__ = ["load_consolidator_prompt", "load_subagent_prompt", "load_system_prompt"]

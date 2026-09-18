from typing import TYPE_CHECKING

from agent_core.workspace import Workspace

if TYPE_CHECKING:
    from agent_core.skills import SkillRegistry
    from agent_core.subagent import SubagentRole


def render_system_prompt(
    template: str,
    workspace: Workspace,
    skills: "SkillRegistry | None" = None,
) -> str:
    prompt = template.replace("{{workspace}}", str(workspace.path)).strip()
    return _with_skills(prompt, skills)


def render_subagent_prompt(
    template: str,
    workspace: Workspace,
    role: "SubagentRole",
    skills: "SkillRegistry | None" = None,
) -> str:
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


__all__ = ["render_subagent_prompt", "render_system_prompt"]

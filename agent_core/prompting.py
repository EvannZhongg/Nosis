from typing import TYPE_CHECKING

from agent_core.workspace import Workspace

if TYPE_CHECKING:
    from agent_core.memory import MemoryContext
    from agent_core.skills import SkillRegistry
    from agent_core.subagent import SubagentRole
    from agent_core.workspace_instructions import WorkspaceInstructions


def render_system_prompt(
    template: str,
    workspace: Workspace,
    skills: "SkillRegistry | None" = None,
    *,
    instructions: "WorkspaceInstructions | None" = None,
    memory: "MemoryContext | None" = None,
) -> str:
    prompt = template.replace("{{workspace}}", str(workspace.path)).strip()
    return _with_runtime_context(prompt, instructions, skills, memory)


def render_subagent_prompt(
    template: str,
    workspace: Workspace,
    role: "SubagentRole",
    skills: "SkillRegistry | None" = None,
    *,
    instructions: "WorkspaceInstructions | None" = None,
) -> str:
    prompt = (
        template.replace("{{workspace}}", str(workspace.path))
        .replace("{{role}}", role.name)
        .replace("{{role_description}}", role.description)
        .strip()
    )
    if role.instructions:
        prompt = f"{prompt}\n\n## Role Instructions\n{role.instructions.strip()}"
    return _with_runtime_context(prompt, instructions, skills, None)


def _with_runtime_context(
    prompt: str,
    instructions: "WorkspaceInstructions | None",
    skills: "SkillRegistry | None",
    memory: "MemoryContext | None",
) -> str:
    sections = [prompt]
    instruction_section = _instruction_section(instructions)
    if instruction_section:
        sections.append(instruction_section)
    skill_section = skills.prompt_section() if skills else ""
    if skill_section:
        sections.append(skill_section)
    memory_section = memory.prompt_section() if memory else ""
    if memory_section:
        sections.append(memory_section)
    return "\n\n".join(sections)


def _instruction_section(
    instructions: "WorkspaceInstructions | None",
) -> str:
    if not instructions:
        return ""
    sections = [
        "## Workspace Instructions",
        "These runtime-loaded instructions persist for this Runtime. "
        "They are already loaded; do not search for instruction files to "
        "discover rules during normal work. You may still read or edit those "
        "files when the user explicitly asks. "
        "Earlier sources override later sources when instructions conflict.\n"
        "Priority from highest to lowest:\n"
        + "\n".join(
            f"{index}. `{document.label}`"
            for index, document in enumerate(instructions.documents, start=1)
        ),
    ]
    sections.extend(
        f"### {document.label}\n{document.content}"
        for document in instructions.documents
        if document.content
    )
    return "\n\n".join(sections)


__all__ = ["render_subagent_prompt", "render_system_prompt"]

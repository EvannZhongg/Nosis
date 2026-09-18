import tempfile
import unittest
from pathlib import Path

from agent_core import (
    LLMProvider,
    LLMResponse,
    SubagentRole,
    Workspace,
    WorkspaceInstruction,
    WorkspaceInstructions,
)
from agent_core.prompting import render_subagent_prompt, render_system_prompt


SYSTEM_TEMPLATE = "I am Nosis.\n\nCurrent workspace: {{workspace}}"
SUBAGENT_TEMPLATE = (
    "Role {{role}}: {{role_description}}\n\n"
    "Current workspace: {{workspace}}"
)


class _TextOnlyProvider(LLMProvider):
    """A role needs a provider; the prompt does not depend on which."""

    @property
    def max_context_tokens(self) -> int:
        return 1000

    def count_input_tokens(self, request) -> int:
        return 1

    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        return LLMResponse(content="unused")


class PromptsTest(unittest.TestCase):
    def instructions(self, root: Path) -> WorkspaceInstructions:
        return WorkspaceInstructions(
            documents=(
                WorkspaceInstruction(
                    "~/.nosis/AGENTS.md", "global rule"
                ),
                WorkspaceInstruction(
                    "<workspace>/CLAUDE.md", "claude rule"
                ),
                WorkspaceInstruction(
                    "<workspace>/AGENTS.md", "agent rule"
                ),
            ),
            fingerprint="fingerprint",
        )

    def test_loads_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            prompt = render_system_prompt(SYSTEM_TEMPLATE, workspace)

        self.assertTrue(prompt)
        self.assertIn("I am Nosis", prompt)
        self.assertIn(f"Current workspace: {workspace.path}", prompt)
        self.assertNotIn("{{workspace}}", prompt)
        self.assertNotIn(f"{{{{{workspace.path}}}}}", prompt)

    def test_system_prompt_includes_workspace_instruction_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            prompt = render_system_prompt(
                SYSTEM_TEMPLATE,
                workspace,
                instructions=self.instructions(root),
            )

        self.assertLess(prompt.index("global rule"), prompt.index("claude rule"))
        self.assertLess(prompt.index("claude rule"), prompt.index("agent rule"))
        self.assertIn("Earlier sources override later sources", prompt)

    def test_loads_subagent_prompt_with_workspace_and_role(self) -> None:
        role = SubagentRole(
            name="researcher",
            description="Read the workspace and report findings.",
            tools=("read_file",),
            provider=_TextOnlyProvider(),
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            prompt = render_subagent_prompt(
                SUBAGENT_TEMPLATE, workspace, role
            )

        self.assertTrue(prompt)
        self.assertIn(f"Current workspace: {workspace.path}", prompt)
        self.assertIn("researcher", prompt)
        self.assertIn(role.description, prompt)
        self.assertNotIn("{{workspace}}", prompt)
        self.assertNotIn("{{role}}", prompt)
        self.assertNotIn("{{role_description}}", prompt)

    def test_subagent_prompt_inherits_workspace_instructions(self) -> None:
        role = SubagentRole(
            name="researcher",
            description="Read the workspace and report findings.",
            tools=("read_file",),
            provider=_TextOnlyProvider(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = render_subagent_prompt(
                SUBAGENT_TEMPLATE,
                Workspace(root),
                role,
                instructions=self.instructions(root),
            )

        self.assertIn("global rule", prompt)
        self.assertIn("claude rule", prompt)
        self.assertIn("agent rule", prompt)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from agent_core import Workspace
from agent_runtime.instructions import load_workspace_instructions


class WorkspaceInstructionsTest(unittest.TestCase):
    def test_loads_only_config_and_workspace_root_files_in_priority_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            workspace_path = root / "workspace"
            nested = workspace_path / "nested"
            config.mkdir()
            nested.mkdir(parents=True)
            (config / "AGENTS.md").write_text("global", encoding="utf-8")
            (workspace_path / "CLAUDE.md").write_text("claude", encoding="utf-8")
            (workspace_path / "AGENTS.md").write_text("workspace", encoding="utf-8")
            (nested / "AGENTS.md").write_text("nested", encoding="utf-8")
            (root / "AGENTS.md").write_text("parent", encoding="utf-8")

            instructions = load_workspace_instructions(
                config,
                Workspace(workspace_path),
                ("CLAUDE.md", "AGENTS.md"),
            )

        self.assertEqual(
            [document.label for document in instructions.documents],
            [
                "~/.nosis/AGENTS.md",
                "<workspace>/CLAUDE.md",
                "<workspace>/AGENTS.md",
            ],
        )
        self.assertEqual(
            [document.content for document in instructions.documents],
            ["global", "claude", "workspace"],
        )

    def test_fingerprint_changes_with_content_presence_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            first_workspace = root / "first"
            second_workspace = root / "second"
            config.mkdir()
            first_workspace.mkdir()
            second_workspace.mkdir()
            first = load_workspace_instructions(
                config,
                Workspace(first_workspace),
                ("AGENTS.md",),
            )

            (first_workspace / "AGENTS.md").write_text("rule", encoding="utf-8")
            changed = load_workspace_instructions(
                config,
                Workspace(first_workspace),
                ("AGENTS.md",),
            )
            other = load_workspace_instructions(
                config,
                Workspace(second_workspace),
                ("AGENTS.md",),
            )

        self.assertNotEqual(first.fingerprint, changed.fingerprint)
        self.assertNotEqual(changed.fingerprint, other.fingerprint)

    def test_loads_configured_workspace_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            workspace_path = root / "workspace"
            config.mkdir()
            workspace_path.mkdir()
            (workspace_path / "PROJECT.md").write_text("project", encoding="utf-8")

            instructions = load_workspace_instructions(
                config,
                Workspace(workspace_path),
                ("PROJECT.md",),
            )

        self.assertEqual(
            [document.label for document in instructions.documents],
            ["~/.nosis/AGENTS.md", "<workspace>/PROJECT.md"],
        )
        self.assertEqual(instructions.documents[1].content, "project")


if __name__ == "__main__":
    unittest.main()

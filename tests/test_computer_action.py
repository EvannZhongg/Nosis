import tempfile
import unittest
from pathlib import Path

from agent_core import ComputerActionTool, ImagePart, Session, ToolExecutionContext, Workspace
from tests.test_media import png_bytes


class ComputerActionToolTest(unittest.TestCase):
    def test_definition_declares_coordinate_mapping_and_complete_actions(self) -> None:
        tool = ComputerActionTool()
        context = ToolExecutionContext(
            workspace=Workspace(Path.cwd()),
            session=Session(),
            vision_input=True,
            computer=lambda _arguments: Path("unused"),
        )

        definition = tool.definition(context)
        actions = definition.parameters["properties"]["action"]["enum"]

        self.assertIn("virtual_desktop_left", definition.description)
        self.assertEqual(
            set(actions),
            {
                "click",
                "move",
                "drag",
                "mouse_down",
                "mouse_up",
                "type",
                "key",
                "hotkey",
                "scroll",
                "wait",
            },
        )
        self.assertIn("to_x", definition.parameters["properties"])
        self.assertIn("seconds", definition.parameters["properties"])

    def test_action_is_followed_by_a_screenshot_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            shot = root / ".nosis" / "attachments" / "after.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(png_bytes(12, 8))
            calls = []

            def control(arguments):
                calls.append(arguments)
                return shot

            context = ToolExecutionContext(
                workspace=workspace,
                session=Session(),
                vision_input=True,
                computer=control,
            )
            result = ComputerActionTool().execute(
                {"action": "click", "x": 10, "y": 20}, context
            )

            self.assertEqual(calls, [{"action": "click", "x": 10, "y": 20}])
            self.assertEqual(result.attachments[0].path, ".nosis/attachments/after.png")
            self.assertIsInstance(result.attachments[0], ImagePart)

    def test_action_requires_vision_capability(self) -> None:
        tool = ComputerActionTool()
        context = ToolExecutionContext(
            workspace=Workspace(Path.cwd()),
            session=Session(),
            computer=lambda _arguments: Path("unused"),
        )
        self.assertFalse(tool.available(context))


if __name__ == "__main__":
    unittest.main()

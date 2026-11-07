import tempfile
import unittest
from pathlib import Path

from agent_core import ComputerActionTool, ImagePart, Session, ToolExecutionContext, Workspace
from agent_core.tools.context import DesktopResult, DesktopWindow
from tests.image_fixtures import png_bytes


class ComputerActionToolTest(unittest.TestCase):
    def test_definition_declares_coordinate_mapping_and_complete_actions(self) -> None:
        tool = ComputerActionTool()
        context = ToolExecutionContext(
            workspace=Workspace(Path.cwd()),
            session=Session(),
            vision_input=True,
            computer=lambda _arguments: DesktopResult(),
        )

        definition = tool.definition(context)
        actions = definition.parameters["properties"]["action"]["enum"]

        self.assertIn("negative screen origins", definition.description)
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

    def test_action_is_followed_by_a_screenshot_attachment_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            shot = root / ".nosis" / "attachments" / "after.png"
            shot.parent.mkdir(parents=True)
            shot.write_bytes(png_bytes(12, 8))
            calls = []

            def control(arguments):
                calls.append(arguments)
                return DesktopResult(screenshot=shot)

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

    def test_platform_schema_and_action_without_screenshot(self) -> None:
        tool = ComputerActionTool()
        window = DesktopWindow(1, 2, "Editor", "editor.exe")
        for supports_windows in (False, True):
            context = ToolExecutionContext(
                workspace=Workspace(Path.cwd()),
                session=Session(),
                vision_input=True,
                computer_windows=supports_windows,
                computer=lambda args: DesktopResult(window_at_point=window),
            )
            self.assertEqual("window" in tool.definition(context).parameters["properties"], supports_windows)
            result = tool.execute({"action": "click", "x": 2, "y": 3, "screenshot": False}, context)
            self.assertEqual(result.attachments, ())
            self.assertIsNone(result.output["screen_changed"])
            self.assertEqual("window_at_point" in result.output, supports_windows)
            if supports_windows:
                self.assertEqual(result.output["window_at_point"]["hwnd"], 1)

    def test_action_requires_vision_capability(self) -> None:
        tool = ComputerActionTool()
        context = ToolExecutionContext(
            workspace=Workspace(Path.cwd()),
            session=Session(),
            computer=lambda _arguments: DesktopResult(),
        )
        self.assertFalse(tool.available(context))


if __name__ == "__main__":
    unittest.main()

import base64
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

from agent_core import Session, ToolBatchExecutor, ToolCall, ToolExecutionContext, ToolMediaEvent, Workspace, builtin_catalog
from agent_core.content import FilePart
from agent_core.projection import project_context_units
from agent_core.session import message_from_dict, message_to_dict
from agent_core.media import MAX_IMAGE_BYTES
from agent_core.tools.builtin.present_file import PresentFileTool
from agent_core.mcp.tool import McpTool
from interfaces.bridge.protocol import event_to_message
from agent_core.tools.context import DesktopResult
from tests.image_fixtures import png_bytes


class PresentFileTest(unittest.TestCase):
    def test_mixed_screenshot_and_delivery_batch_keeps_delivery_intent_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            screenshot = workspace.resolve_path(".nosis/attachments/desktop.png")
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(png_bytes(3, 2))
            (workspace.path / "report.txt").write_text("report", encoding="utf-8")
            session = Session()
            turn_id = session.begin_turn("turn")
            browser = McpTool("browser", "browser_screenshot", "Screenshot", {"type": "object"})
            context = ToolExecutionContext(
                workspace=workspace, session=session, vision_input=True,
                screenshot=lambda _arguments: DesktopResult(screenshot=screenshot),
                mcp=SimpleNamespace(call_tool=lambda *args, **kwargs: {
                    "content": [{"type": "image", "data": base64.b64encode(png_bytes(2, 3)).decode(), "mime_type": "image/png"}],
                }),
            )
            tools = builtin_catalog().extend((browser,)).select(
                ("computer_screenshot", browser.name, "present_file"), context,
            )
            events = []

            class Normalizer:
                def normalize(self, result):
                    return result.to_content()

            executor = ToolBatchExecutor(tools, session, Normalizer(), now=lambda: datetime.now(timezone.utc))
            executor.execute((
                ToolCall("desktop", "computer_screenshot", {}),
                ToolCall("report", "present_file", {"path": "report.txt"}),
                ToolCall("browser", browser.name, {}),
            ), turn_id=turn_id, on_media=lambda parts, present: events.append(ToolMediaEvent(parts, present)))
            self.assertEqual([event.present_to_user for event in events], [False, True])
            self.assertEqual(len(events[0].attachments), 2)
            self.assertTrue(all(part.type == "image" for part in events[0].attachments))
            self.assertEqual([part.filename for part in events[1].attachments], ["report.txt"])
            # Observations still reach the model together with the tool results.
            self.assertEqual(session.items[-2].origin, "tool_media")
            self.assertEqual(len(session.items[-2].parts), 3)
            self.assertEqual(session.items[-1].origin, "tool_delivery")
            messages = [m for unit in project_context_units(session.items) for m in unit.messages]
            self.assertIn(session.items[-2], messages)
            self.assertNotIn(session.items[-1], messages)
            self.assertFalse(event_to_message(events[0], turn_id)["present_to_user"])
            self.assertTrue(event_to_message(events[1], turn_id)["present_to_user"])

    def test_image_deliverable_uses_its_decoded_type(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "image.bin").write_bytes(png_bytes(3, 2))
            context = ToolExecutionContext(workspace=workspace, session=Session())
            result = PresentFileTool().execute({"path": "image.bin"}, context)
            part = result.attachments[0]
            self.assertEqual((part.type, part.mime_type), ("image", "image/png"))
            self.assertTrue(part.path.endswith(".png"))

    def test_deliverable_is_snapshotted_journaled_and_sent_as_structured_media(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            original = workspace.path / "report.csv"
            original.write_bytes(b"a,b\n1,2")
            session = Session()
            turn_id = session.begin_turn("turn")
            context = ToolExecutionContext(workspace=workspace, session=session)
            tools = builtin_catalog().select(("present_file",), context)
            media = []

            class Normalizer:
                def normalize(self, result):
                    return result.to_content()

            executor = ToolBatchExecutor(tools, session, Normalizer(), now=lambda: datetime.now(timezone.utc))
            executor.execute(
                (ToolCall("deliver", "present_file", {"path": "report.csv"}),),
                turn_id=turn_id,
                on_media=lambda parts, present: media.append(ToolMediaEvent(parts, present)),
            )
            part = media[0].attachments[0]
            self.assertIsInstance(part, FilePart)
            self.assertEqual(part.filename, "report.csv")
            original.write_bytes(b"changed")
            self.assertEqual(workspace.resolve_path(part.path).read_bytes(), b"a,b\n1,2")
            self.assertEqual(session.items[-1].origin, "tool_delivery")
            self.assertIn(part, session.items[-1].content)
            restored = message_from_dict(message_to_dict(session.items[-1]))
            self.assertEqual(restored.origin, "tool_delivery")
            self.assertEqual(restored.parts, session.items[-1].parts)
            event = event_to_message(media[0], turn_id)
            self.assertTrue(event["present_to_user"])
            self.assertEqual(event["attachments"][0]["type"], "file")
            self.assertEqual(event["attachments"][0]["size_bytes"], 7)

    def test_large_image_delivery_never_enters_model_context(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            image = png_bytes(2, 3) + b"\0" * MAX_IMAGE_BYTES
            (workspace.path / "large.png").write_bytes(image)
            session = Session()
            turn_id = session.begin_turn("turn")
            call = ToolCall("deliver", "present_file", {"path": "large.png"})
            session.add_item("assistant", None, tool_calls=(call,))
            tools = builtin_catalog().select(("present_file",), ToolExecutionContext(workspace=workspace, session=session))

            class Normalizer:
                def normalize(self, result):
                    return result.to_content()

            ToolBatchExecutor(tools, session, Normalizer(), now=lambda: datetime.now(timezone.utc)).execute((call,), turn_id=turn_id)
            delivery = session.items[-1]
            self.assertEqual(delivery.origin, "tool_delivery")
            self.assertEqual(delivery.parts[-1].size_bytes, len(image))
            units = project_context_units(session.items)
            self.assertEqual(len(units), 1)
            self.assertEqual([m.role for m in units[0].messages], ["assistant", "tool"])

    def test_rejects_paths_outside_workspace_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            context = ToolExecutionContext(workspace=Workspace(Path(directory)), session=Session())
            (context.workspace.path / "folder").mkdir()
            for path, error in (
                ("../secret.txt", ValueError),
                (str(context.workspace.path / "secret.txt"), ValueError),
                ("folder/../secret.txt", ValueError),
                ("missing.txt", FileNotFoundError),
                ("folder", OSError),
                (".", ValueError),
            ):
                with self.subTest(path=path), self.assertRaises(error):
                    PresentFileTool().execute({"path": path}, context)
            self.assertFalse((Path(directory) / ".nosis").exists())

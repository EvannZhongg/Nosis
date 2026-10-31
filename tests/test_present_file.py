import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

from agent_core import Session, ToolBatchExecutor, ToolCall, ToolExecutionContext, ToolMediaEvent, Workspace, builtin_catalog
from agent_core.content import FilePart
from agent_core.tools.builtin.present_file import PresentFileTool
from interfaces.bridge.protocol import event_to_message
from tests.image_fixtures import png_bytes


class PresentFileTest(unittest.TestCase):
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
            executor.execute((ToolCall("deliver", "present_file", {"path": "report.csv"}),), turn_id=turn_id, on_media=media.append)
            part = media[0][0]
            self.assertIsInstance(part, FilePart)
            self.assertEqual(part.filename, "report.csv")
            original.write_bytes(b"changed")
            self.assertEqual(workspace.resolve_path(part.path).read_bytes(), b"a,b\n1,2")
            self.assertEqual(session.items[-1].origin, "tool_media")
            self.assertIn(part, session.items[-1].content)
            event = event_to_message(ToolMediaEvent(attachments=media[0]), turn_id)
            self.assertEqual(event["attachments"][0]["type"], "file")
            self.assertEqual(event["attachments"][0]["size_bytes"], 7)

    def test_rejects_paths_outside_workspace_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            context = ToolExecutionContext(workspace=Workspace(Path(directory)), session=Session())
            for path in ("../secret.txt", "missing.txt", "."):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    PresentFileTool().execute({"path": path}, context)
            self.assertFalse((Path(directory) / ".nosis").exists())

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import Workspace
from agent_runtime.attachments import parse_attachments, store_attachment
from tests.image_fixtures import png_bytes


class AttachmentStorageTest(unittest.TestCase):
    def test_upload_type_comes_from_bytes_and_names_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            image = store_attachment(io.BytesIO(png_bytes(3, 2)), workspace, "../image.bin", "text/plain")
            file = store_attachment(io.BytesIO(b"report"), workspace, "..\\report.txt", "image/png")
            self.assertEqual((image["type"], image["mime_type"], image["filename"]), ("image", "image/png", "image.bin"))
            self.assertTrue(image["path"].endswith(".png"))
            self.assertEqual((file["type"], file["filename"], file["mime_type"]), ("file", "report.txt", "text/plain"))
            self.assertEqual([part.type for part in parse_attachments([image, file], workspace)], ["image", "file"])

    def test_failed_copy_removes_partial_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))

            def fail(source, target):
                target.write(b"partial")
                raise OSError("copy failed")

            with patch("agent_runtime.attachments.shutil.copyfileobj", side_effect=fail):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    store_attachment(io.BytesIO(b"hello"), workspace, "file.txt")
            self.assertEqual(list(workspace.resolve_path(".nosis/attachments").iterdir()), [])

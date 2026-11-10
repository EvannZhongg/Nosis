"""Deterministic substitutions at the filesystem boundary on POSIX."""

import base64
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent_core import (
    ApplyPatchTool, EditFileTool, GeneratedImage, GenerateImageTool,
    ListDirectoryTool, ReadFileTool, SearchFilesTool, Session,
    ToolCall, ToolCatalog, ToolExecutionContext, Workspace, WriteFileTool,
)
from agent_core.files import bind_file, filesystem
from agent_core.media import encode_data_url
from agent_core.tools.builtin.present_file import PresentFileTool
from tests.file_write_fixtures import patch_text
from tests.image_fixtures import png_bytes
from tests.test_generate_image import StubImageGenerator


@unittest.skipUnless(os.name == "posix", "POSIX directory replacement semantics")
class FileBoundaryTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.outside = root / "outside"
        self.outside.mkdir()
        self.directory = self.workspace / "d"
        self.directory.mkdir()
        self.saved = self.workspace / "saved"
        (self.directory / "file.txt").write_bytes(b"old\n")
        (self.outside / "file.txt").write_bytes(b"OUTSIDE_SECRET\n")
        self.context = ToolExecutionContext(Workspace(self.workspace), Session())

    def swap(self):
        self.directory.rename(self.saved)
        self.directory.symlink_to(self.outside, target_is_directory=True)

    def call(self, tool, arguments):
        catalog = ToolCatalog([tool]).select([tool.name], self.context)
        return catalog.execute(ToolCall("test", tool.name, arguments))

    def test_all_text_tools_reject_a_directory_swapped_after_resolution(self):
        cases = (
            (ReadFileTool(), {"path": "d/file.txt"}),
            (WriteFileTool(), {"path": "d/file.txt", "content": "new", "overwrite": True}),
            (EditFileTool(), {"path": "d/file.txt", "old_text": "old", "new_text": "new"}),
            (ApplyPatchTool(), {"patch": patch_text("*** Update File: d/file.txt\n@@\n-old\n+new")}),
            (ApplyPatchTool(), {"patch": patch_text("*** Delete File: d/file.txt")}),
            (ApplyPatchTool(), {"patch": patch_text("*** Add File: d/new.txt\n+new")}),
            (PresentFileTool(), {"path": "d/file.txt"}),
        )
        resolve = Workspace.resolve_path
        for tool, arguments in cases:
            with self.subTest(tool=tool.name, arguments=arguments):
                def swap_after_check(workspace, path):
                    resolved = resolve(workspace, path)
                    self.swap()
                    return resolved

                with patch.object(Workspace, "resolve_path", swap_after_check):
                    result = self.call(tool, arguments)
                self.assertIsNotNone(result.error)
                self.assertEqual((self.outside / "file.txt").read_bytes(), b"OUTSIDE_SECRET\n")
                self.assertEqual([p.name for p in self.outside.iterdir()], ["file.txt"])
                self.directory.unlink()
                self.saved.rename(self.directory)

    def test_open_directory_stays_bound_after_its_name_is_replaced(self):
        with bind_file(self.workspace, Path("d/file.txt")) as file:
            self.swap()
            self.assertEqual(file.read_bytes(), b"old\n")
            file.write(b"new", replace=True, mode=file.mode())
            self.assertEqual((self.saved / "file.txt").read_bytes(), b"new")
            file.unlink()
        self.assertFalse((self.saved / "file.txt").exists())
        self.assertEqual((self.outside / "file.txt").read_bytes(), b"OUTSIDE_SECRET\n")

    def test_read_rejects_leaf_symlink_and_nonregular_file(self):
        target = self.directory / "file.txt"
        with bind_file(self.workspace, Path("d/file.txt")) as file:
            self.assertIsNotNone(file.mode())
            target.unlink()
            target.symlink_to(self.outside / "file.txt")
            with self.assertRaises(OSError):
                file.read_bytes()
            target.unlink()
            os.mkfifo(target)
            with self.assertRaises(OSError):
                file.read_bytes()

    def test_created_parent_is_checked_before_use(self):
        mkdir = filesystem.mkdir

        def replace_new_directory(parent, name):
            mkdir(parent, name)
            (self.directory / name).rmdir()
            (self.directory / name).symlink_to(self.outside, target_is_directory=True)

        with patch.object(filesystem, "mkdir", replace_new_directory):
            result = self.call(WriteFileTool(), {"path": "d/new/file.txt", "content": "new"})
        self.assertIsNotNone(result.error)
        self.assertEqual((self.outside / "file.txt").read_bytes(), b"OUTSIDE_SECRET\n")

    def test_search_rejects_directory_and_leaf_swaps_after_enumeration(self):
        for leaf in (False, True):
            with self.subTest(leaf=leaf):
                target = self.directory / "file.txt"

                def candidates(*args):
                    if leaf:
                        target.rename(self.directory / "saved.txt")
                        target.symlink_to(self.outside / "file.txt")
                    else:
                        self.swap()
                    yield target

                with patch("agent_core.tools.builtin.search_files._iter_files", candidates):
                    result = self.call(SearchFilesTool(), {"path": ".", "pattern": "OUTSIDE"})
                self.assertIsNone(result.error)
                self.assertEqual(result.output["matches"], [])
                self.assertEqual(result.output["skipped_files"], 1)
                if not leaf:
                    self.directory.unlink()
                    self.saved.rename(self.directory)

    def test_recursive_enumeration_does_not_follow_a_replaced_directory(self):
        (self.outside / "secret-name.txt").write_bytes(b"OUTSIDE_SECRET")
        scandir = os.scandir

        @contextmanager
        def swap_after_scandir(path):
            with scandir(path) as iterator:
                entries = list(iterator)
                for entry in entries:
                    entry.is_dir(follow_symlinks=False)
                    entry.is_file(follow_symlinks=False)
                if not self.saved.exists():
                    self.swap()
                yield iter(entries)

        for tool, arguments, key in (
            (ListDirectoryTool(), {"path": ".", "recursive": True, "glob": "**/*"}, "entries"),
            (SearchFilesTool(), {"path": ".", "pattern": "secret-name", "mode": "files"}, "matches"),
        ):
            with self.subTest(tool=tool.name):
                with patch("os.scandir", swap_after_scandir):
                    result = self.call(tool, arguments)
                self.assertIsNone(result.error)
                self.assertNotIn("secret-name", str(result.output[key]))
                self.directory.unlink()
                self.saved.rename(self.directory)

    def test_image_encoding_uses_the_file_it_validated(self):
        from PIL import Image

        inside = png_bytes(2, 3)
        (self.directory / "image.png").write_bytes(inside)
        (self.outside / "image.png").write_bytes(b"OUTSIDE_SECRET")
        image_open = Image.open

        def swap_after_image_open(*args, **kwargs):
            image = image_open(*args, **kwargs)
            if not self.saved.exists():
                self.swap()
            return image

        with patch.object(Image, "open", swap_after_image_open):
            url = encode_data_url(self.directory / "image.png")
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), inside)

    def test_reference_images_are_snapshots_before_provider_execution(self):
        inside = png_bytes(2, 3)
        (self.directory / "image.png").write_bytes(inside)
        (self.outside / "image.png").write_bytes(b"OUTSIDE_SECRET")
        generator = StubImageGenerator((GeneratedImage(png_bytes(3, 2)),))
        generate = generator.generate

        def swap_before_provider(**kwargs):
            self.swap()
            self.assertEqual(kwargs["reference_images"], (inside,))
            return generate(**kwargs)

        generator.generate = swap_before_provider
        context = ToolExecutionContext(Workspace(self.workspace), Session(), image_generator=generator)
        GenerateImageTool().execute({"prompt": "edit", "reference_images": ["d/image.png"]}, context)

    def test_attachment_writers_reject_destination_directory_substitution(self):
        attachments = self.workspace / ".nosis" / "attachments"
        attachments.mkdir(parents=True)
        resolve = Workspace.resolve_path
        generator = StubImageGenerator((GeneratedImage(png_bytes(3, 2)),))
        self.context = ToolExecutionContext(Workspace(self.workspace), Session(), image_generator=generator)
        for tool, arguments in (
            (PresentFileTool(), {"path": "d/file.txt"}),
            (GenerateImageTool(), {"prompt": "draw"}),
        ):
            with self.subTest(tool=tool.name):
                def swap_after_check(workspace, path):
                    resolved = resolve(workspace, path)
                    if path.startswith(".nosis/attachments"):
                        attachments.rmdir()
                        attachments.symlink_to(self.outside, target_is_directory=True)
                    return resolved

                with patch.object(Workspace, "resolve_path", swap_after_check):
                    result = self.call(tool, arguments)
                self.assertIsNotNone(result.error)
                self.assertEqual([p.name for p in self.outside.iterdir()], ["file.txt"])
                attachments.unlink()
                attachments.mkdir()

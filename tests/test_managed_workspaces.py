import tempfile
import unittest
from pathlib import Path

from interfaces.bridge.managed_workspaces import (
    create_scratch_workspace,
    delete_scratch_workspace,
    is_scratch_workspace,
)


class ManagedWorkspaceTest(unittest.TestCase):
    def test_creates_one_persistent_workspace_per_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "scratch"

            first = create_scratch_workspace(root, "session-1")
            reopened = create_scratch_workspace(root, "session-1")
            second = create_scratch_workspace(root, "session-2")

            self.assertEqual(first.path, reopened.path)
            self.assertNotEqual(first.path, second.path)
            self.assertTrue(first.path.is_dir())
            self.assertTrue(second.path.is_dir())
            self.assertTrue(is_scratch_workspace(first.path))

    def test_rejects_an_identifier_that_escapes_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "path segment"):
                create_scratch_workspace(Path(directory), "../outside")

    def test_rejects_an_existing_symlink_outside_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "scratch"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "session-1").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks are unavailable")

            with self.assertRaisesRegex(ValueError, "configured root"):
                create_scratch_workspace(root, "session-1")

    def test_deletes_a_managed_scratch_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = create_scratch_workspace(
                Path(directory) / "scratch", "session-1"
            )
            (workspace.path / "result.txt").write_text("done", encoding="utf-8")

            self.assertTrue(delete_scratch_workspace(workspace.path))
            self.assertFalse(workspace.path.exists())

    def test_does_not_delete_an_unmanaged_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()

            self.assertFalse(delete_scratch_workspace(workspace))
            self.assertTrue(workspace.is_dir())


if __name__ == "__main__":
    unittest.main()

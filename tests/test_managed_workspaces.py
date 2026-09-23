import tempfile
import unittest
from pathlib import Path

from interfaces.bridge.managed_workspaces import (
    create_scratch_workspace,
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
            self.assertTrue(is_scratch_workspace(root, first.path))

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


if __name__ == "__main__":
    unittest.main()

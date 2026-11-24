import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interfaces.bridge.managed_workspaces import (
    SCRATCH_WORKSPACE_MARKER,
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

    @unittest.skipUnless(os.name == "nt", "Windows read-only file deletion")
    def test_deletes_read_only_git_pack_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = create_scratch_workspace(Path(directory), "session-1").path
            pack = workspace / ".git" / "objects" / "pack"
            pack.mkdir(parents=True)
            for suffix in ("idx", "pack"):
                path = pack / f"pack-test.{suffix}"
                path.write_bytes(b"git object data")
                path.chmod(stat.S_IREAD)

            self.assertTrue(delete_scratch_workspace(workspace))
            self.assertFalse(workspace.exists())

    def test_failed_cleanup_preserves_workspace_ownership_for_another_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = create_scratch_workspace(Path(directory), "session-1").path
            marker = workspace / SCRATCH_WORKSPACE_MARKER
            blocked = workspace / "blocked.txt"
            blocked.write_text("remaining", encoding="utf-8")
            error = PermissionError("file is in use")

            def fail_cleanup(path, **kwargs):
                marker.unlink()
                raise error

            with patch(
                "interfaces.bridge.managed_workspaces.shutil.rmtree",
                side_effect=fail_cleanup,
            ):
                with self.assertRaises(PermissionError) as caught:
                    delete_scratch_workspace(workspace)

            self.assertIs(caught.exception, error)
            self.assertTrue(is_scratch_workspace(workspace))
            self.assertEqual(blocked.read_text(encoding="utf-8"), "remaining")
            self.assertTrue(delete_scratch_workspace(workspace))
            self.assertFalse(workspace.exists())

    def test_does_not_change_permissions_for_an_unrelated_deletion_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = create_scratch_workspace(Path(directory), "session-1").path
            blocked = workspace / "blocked.txt"
            blocked.write_text("remaining", encoding="utf-8")
            unlink = os.unlink
            error = PermissionError("file is in use")

            def fail_unlink(path, *args, **kwargs):
                if Path(path).name == blocked.name:
                    raise error
                return unlink(path, *args, **kwargs)

            with patch("os.unlink", side_effect=fail_unlink), patch("os.chmod") as chmod:
                with self.assertRaises(PermissionError) as caught:
                    delete_scratch_workspace(workspace)

            self.assertIs(caught.exception, error)
            chmod.assert_not_called()


if __name__ == "__main__":
    unittest.main()

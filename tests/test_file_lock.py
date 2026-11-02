import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.file_lock import acquire_file_lock


class FileLockTest(unittest.TestCase):
    def test_contention_and_release_when_the_handle_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "resource.lock"
            with acquire_file_lock(path):
                with self.assertRaises(BlockingIOError):
                    acquire_file_lock(path)
            self.assertTrue(path.is_file())
            with acquire_file_lock(path):
                pass

    def test_open_permission_error_is_not_reported_as_contention(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resource.lock"
            with patch.object(Path, "open", side_effect=PermissionError(errno.EACCES, "denied")):
                with self.assertRaises(PermissionError):
                    acquire_file_lock(path)

    def test_lock_io_error_propagates_and_closes_the_handle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resource.lock"
            handle = path.open("a+b")
            self.addCleanup(handle.close)
            native_lock = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
            with (
                patch.object(Path, "open", return_value=handle),
                patch(native_lock, side_effect=OSError(errno.EIO, "lock failed")),
            ):
                with self.assertRaises(OSError) as failure:
                    acquire_file_lock(path)
            self.assertEqual(failure.exception.errno, errno.EIO)
            self.assertTrue(handle.closed)

    def test_blocking_failure_is_not_reported_as_nonblocking_contention(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resource.lock"
            handle = path.open("a+b")
            self.addCleanup(handle.close)
            error = PermissionError(errno.EACCES, "blocking lock failed")
            native_lock = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
            with (
                patch.object(Path, "open", return_value=handle),
                patch(native_lock, side_effect=error),
            ):
                with self.assertRaises(PermissionError) as failure:
                    acquire_file_lock(path, blocking=True)
            self.assertIs(failure.exception, error)
            self.assertTrue(handle.closed)

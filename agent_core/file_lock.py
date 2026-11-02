"""Nonblocking exclusive file locks shared by process resource owners."""

import errno
import os
from pathlib import Path
from typing import BinaryIO


def acquire_file_lock(path: Path) -> BinaryIO:
    """Return a locked handle; raise BlockingIOError if another owner holds it.

    Close the handle to release the lock. Keep the file in place: unlinking
    it would allow another process to lock a different inode at the same path.
    Other filesystem errors propagate without being treated as contention.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise BlockingIOError(errno.EAGAIN, "File is already locked", str(path)) from error
            raise
    except BaseException:
        handle.close()
        raise
    return handle

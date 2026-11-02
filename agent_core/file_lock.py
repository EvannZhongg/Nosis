"""Exclusive file locks shared by process resource owners."""

import errno
import os
from pathlib import Path
from typing import BinaryIO


def acquire_file_lock(path: Path, *, blocking: bool = False) -> BinaryIO:
    """Return a locked handle, waiting for its owner only in blocking mode.

    Close the handle to release the lock. Keep the file in place: unlinking
    it would allow another process to lock a different inode at the same path.
    Nonblocking contention raises BlockingIOError; other errors propagate.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        # Windows locks also prevent reads of the locked byte. Inspect its
        # size without reading before attempting either lock mode.
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(handle.fileno(), mode, 1)
            else:
                import fcntl

                mode = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(handle.fileno(), mode)
        except OSError as error:
            if not blocking and error.errno in {errno.EACCES, errno.EAGAIN}:
                raise BlockingIOError(errno.EAGAIN, "File is already locked", str(path)) from error
            raise
    except BaseException:
        handle.close()
        raise
    return handle

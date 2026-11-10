"""Descriptor-relative filesystem operations for the file tools."""

import errno
import os
import shutil
import stat
from typing import BinaryIO
from uuid import uuid4


_DIRECTORY = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW


def open_root(anchor: str) -> int:
    return os.open(anchor, _DIRECTORY)


def close(directory: int) -> None:
    os.close(directory)


def open_directory(parent: int, name: str) -> int:
    return os.open(name, _DIRECTORY, dir_fd=parent)


def mkdir(parent: int, name: str) -> None:
    os.mkdir(name, dir_fd=parent)


def mode(parent: int, name: str) -> int:
    result = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
    if stat.S_ISLNK(result):
        raise OSError(errno.ELOOP, "file path changed to a symbolic link", name)
    return result


def open_read(parent: int, name: str) -> int:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "path must be a regular file", name)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def write(
    parent: int, name: str, data: bytes | BinaryIO, *, replace: bool, permissions: int | None
) -> None:
    temporary = f".nosis-{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as file:
            if isinstance(data, bytes):
                file.write(data)
            else:
                shutil.copyfileobj(data, file)
            file.flush()
            if permissions is not None:
                os.fchmod(file.fileno(), permissions)
            os.fsync(file.fileno())
        if replace:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            temporary = None
        else:
            os.link(
                temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                follow_symlinks=False,
            )
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def unlink(parent: int, name: str) -> None:
    os.unlink(name, dir_fd=parent)

"""Bind file and media I/O to directories opened beneath a configured root."""

import os
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from .path_utils import path_for_comparison

if os.name == "nt":
    from . import _windows_files as filesystem
else:
    from . import _posix_files as filesystem


@dataclass(frozen=True)
class BoundFile:
    parent: int
    name: str
    created_parents: tuple[str, ...]

    def mode(self) -> int | None:
        try:
            return filesystem.mode(self.parent, self.name)
        except FileNotFoundError:
            return None

    def open_read(self) -> int:
        return filesystem.open_read(self.parent, self.name)

    def read_bytes(self) -> bytes:
        with os.fdopen(self.open_read(), "rb") as file:
            return file.read()

    def write(self, data: bytes | BinaryIO, *, replace: bool, mode: int | None) -> None:
        if mode is not None and not stat.S_ISREG(mode):
            raise ValueError("path must be a regular file")
        filesystem.write(
            self.parent, self.name, data, replace=replace,
            permissions=stat.S_IMODE(mode) if mode is not None else None,
        )

    def unlink(self) -> None:
        filesystem.unlink(self.parent, self.name)


@contextmanager
def bind_file(
    root: Path, relative: Path, *, create_parents: bool = False
) -> Iterator[BoundFile]:
    _validate_relative(relative)
    if not relative.parts:
        raise ValueError("file path must name a file")
    with _bind_directory(root, relative.parent, create_parents=create_parents) as (parent, created):
        yield BoundFile(parent, relative.name, created)


@contextmanager
def scandir_directory(path: Path) -> Iterator[Iterator[os.DirEntry]]:
    """Enumerate a pinned directory without following a replaced component."""
    with _bind_directory(path, Path()) as (directory, _created):
        if os.name == "nt":
            # All ancestors remain open without delete sharing on Windows.
            with os.scandir(path) as entries:
                yield entries
        else:
            # Linux traversal handles use O_PATH; scandir needs read access.
            descriptor = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=directory)
            try:
                with os.scandir(descriptor) as entries:
                    yield entries
            finally:
                os.close(descriptor)


def _validate_relative(relative: Path) -> None:
    if relative.anchor or any(
        part == ".." or (os.name == "nt" and ":" in part) for part in relative.parts
    ):
        raise ValueError("file path must stay within its root")


@contextmanager
def _bind_directory(
    root: Path, relative: Path, *, create_parents: bool = False
) -> Iterator[tuple[int, tuple[str, ...]]]:
    # Resolution selects a canonical spelling. Opening every component without
    # following links prevents a later name substitution from granting access.
    _validate_relative(relative)
    root = path_for_comparison(root)
    created: list[str] = []
    with ExitStack() as directories:
        parent = filesystem.open_root(root.anchor)
        directories.callback(filesystem.close, parent)
        for part in root.parts[1:]:
            parent = filesystem.open_directory(parent, part)
            directories.callback(filesystem.close, parent)
        for index, part in enumerate(relative.parts):
            try:
                child = filesystem.open_directory(parent, part)
            except FileNotFoundError:
                if not create_parents:
                    raise
                try:
                    filesystem.mkdir(parent, part)
                except FileExistsError:
                    pass
                else:
                    created.append(Path(*relative.parts[:index + 1]).as_posix())
                child = filesystem.open_directory(parent, part)
            parent = child
            directories.callback(filesystem.close, parent)
        yield parent, tuple(created)

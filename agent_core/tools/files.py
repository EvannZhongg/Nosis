"""Bind file-tool I/O to directories opened beneath a configured root."""

import os
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..path_utils import path_for_comparison

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

    def write(self, data: bytes, *, replace: bool, mode: int | None) -> None:
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
    # Resolution selects a canonical spelling. Each component is opened
    # independently without following links, so that spelling grants no authority.
    if relative.anchor or not relative.parts or any(
        part == ".." or (os.name == "nt" and ":" in part) for part in relative.parts
    ):
        raise ValueError("file path must stay within its root and name a file")

    root = path_for_comparison(root)
    created: list[str] = []
    with ExitStack() as directories:
        parent = filesystem.open_root(root.anchor)
        directories.callback(filesystem.close, parent)
        for part in root.parts[1:]:
            parent = filesystem.open_directory(parent, part)
            directories.callback(filesystem.close, parent)
        for index, part in enumerate(relative.parts[:-1]):
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
        yield BoundFile(parent, relative.name, tuple(created))

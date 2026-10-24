"""Filesystem operations shared by workspace writing tools."""

import errno
import os
import stat
from pathlib import Path

from ..atomic import atomic_write_bytes
from ..path_utils import path_for_comparison


def write_file_bytes(path: Path, data: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    atomic_write_bytes(path, data, mode=mode)


def missing_parent_directories(path: Path, workspace_path: Path) -> list[Path]:
    """Validate the complete parent chain before returning directories to create."""
    workspace_comparison = path_for_comparison(workspace_path)
    missing: list[Path] = []
    current = path.parent

    while current != workspace_path:
        try:
            path_for_comparison(current).relative_to(workspace_comparison)
        except ValueError as error:
            raise ValueError(
                "workspace path must stay within the workspace"
            ) from error

        if current.exists():
            if not current.is_dir():
                raise NotADirectoryError(
                    errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(current),
                )
            break
        missing.append(current)
        current = current.parent

    missing.reverse()
    return missing

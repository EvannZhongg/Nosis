"""Persistent workspaces created and owned by the interface layer."""

import os
import shutil
import stat
from pathlib import Path
from types import TracebackType
from typing import Callable
from uuid import uuid4

from agent_core import Workspace
from agent_core.path_utils import is_path_within


SCRATCH_WORKSPACE_MARKER = ".nosis-scratch-workspace"


def create_scratch_workspace(root: Path, workspace_id: str | None = None) -> Workspace:
    identifier = workspace_id or uuid4().hex
    if (
        not identifier
        or identifier in {".", ".."}
        or "/" in identifier
        or "\\" in identifier
    ):
        raise ValueError("scratch workspace id must be a path segment")
    resolved_root = root.expanduser().resolve()
    path = resolved_root / identifier
    path.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(path)
    if not is_path_within(workspace.path, resolved_root):
        raise ValueError("scratch workspace must stay within its configured root")
    (workspace.path / SCRATCH_WORKSPACE_MARKER).touch(exist_ok=True)
    return workspace


def is_scratch_workspace(workspace: Path | str) -> bool:
    resolved_workspace = Path(workspace).expanduser().resolve()
    return (resolved_workspace / SCRATCH_WORKSPACE_MARKER).is_file()


def delete_scratch_workspace(workspace: Path | str) -> bool:
    resolved_workspace = Path(workspace).expanduser().resolve()
    if not is_scratch_workspace(resolved_workspace):
        return False
    try:
        shutil.rmtree(resolved_workspace, onerror=_remove_readonly)
    except OSError:
        # rmtree may remove the marker before encountering a blocked file.
        # Keep ownership discoverable so a later deletion can finish cleanup.
        marker = resolved_workspace / SCRATCH_WORKSPACE_MARKER
        if not marker.exists():
            marker.touch()
        raise
    return True


def _remove_readonly(
    function: Callable[[str], object],
    path: str,
    exc_info: tuple[type[BaseException], BaseException, TracebackType],
) -> None:
    error = exc_info[1]
    if (
        os.name != "nt"
        or function not in (os.unlink, os.rmdir)
        or not isinstance(error, PermissionError)
    ):
        raise error
    mode = os.stat(path).st_mode
    if mode & stat.S_IWRITE:
        raise error
    os.chmod(path, mode | stat.S_IWRITE)
    function(path)


__all__ = [
    "SCRATCH_WORKSPACE_MARKER",
    "create_scratch_workspace",
    "delete_scratch_workspace",
    "is_scratch_workspace",
]

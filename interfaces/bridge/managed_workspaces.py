"""Persistent workspaces created and owned by the interface layer."""

import shutil
from pathlib import Path
from uuid import uuid4

from agent_core import Workspace


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
    if not workspace.path.is_relative_to(resolved_root):
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
    shutil.rmtree(resolved_workspace)
    return True


__all__ = [
    "SCRATCH_WORKSPACE_MARKER",
    "create_scratch_workspace",
    "delete_scratch_workspace",
    "is_scratch_workspace",
]

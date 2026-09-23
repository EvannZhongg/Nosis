"""Persistent workspaces created and owned by the interface layer."""

from pathlib import Path
from uuid import uuid4

from agent_core import Workspace


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
    return workspace


def is_scratch_workspace(root: Path, workspace: Path | str) -> bool:
    resolved_root = root.expanduser().resolve()
    resolved_workspace = Path(workspace).expanduser().resolve()
    return resolved_workspace != resolved_root and resolved_workspace.is_relative_to(
        resolved_root
    )


__all__ = ["create_scratch_workspace", "is_scratch_workspace"]

"""Workspace metadata persisted alongside grouped Sessions."""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .atomic import atomic_write_text
from .file_lock import acquire_file_lock
from .session_paths import workspace_from_key
from .time_utils import format_utc, parse_utc

WORKSPACE_METADATA_FILENAME = "workspace.json"


def workspace_name(path: Path) -> str:
    return path.name or path.anchor


def new_workspace_metadata(
    directory: Path, *, created: datetime, updated: datetime,
) -> dict[str, str]:
    path = Path(workspace_from_key(directory.name))
    return {
        "workspace_id": directory.name,
        "path": str(path),
        "name": workspace_name(path),
        "created": format_utc(created),
        "updated": format_utc(updated),
    }


def write_workspace_metadata(directory: Path, metadata: dict[str, str]) -> None:
    atomic_write_text(
        directory / WORKSPACE_METADATA_FILENAME,
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
    )


@contextmanager
def _locked_workspace(directory: Path) -> Iterator[None]:
    # Different Sessions can write to the same Workspace in separate processes.
    lock = directory.parent / ".locks" / "workspaces" / f"{directory.name}.lock"
    with acquire_file_lock(lock, blocking=True):
        yield


def read_workspace_metadata(directory: Path) -> dict[str, str]:
    path = directory / WORKSPACE_METADATA_FILENAME
    if not path.is_file():
        raise FileNotFoundError(
            f"missing workspace metadata {path}; "
            "run scripts/backfill_metadata.py first"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_workspace_metadata(directory: Path) -> None:
    """Register a new Workspace without refreshing an existing record."""
    if (directory / WORKSPACE_METADATA_FILENAME).is_file():
        return
    with _locked_workspace(directory):
        if directory.exists():
            read_workspace_metadata(directory)
            return
        now = datetime.now(timezone.utc)
        metadata = new_workspace_metadata(directory, created=now, updated=now)
        write_workspace_metadata(directory, metadata)


def touch_workspace_metadata(directory: Path) -> None:
    """Record activity for an already registered Workspace."""
    with _locked_workspace(directory):
        metadata = read_workspace_metadata(directory)
        now = datetime.now(timezone.utc)
        metadata["updated"] = format_utc(max(parse_utc(metadata["updated"]), now))
        write_workspace_metadata(directory, metadata)

"""Workspace metadata persisted alongside grouped Sessions."""

import json
from datetime import datetime, timezone
from pathlib import Path

from .atomic import atomic_write_text
from .file_lock import acquire_file_lock
from .session_paths import workspace_from_key
from .time_utils import format_utc, parse_utc

WORKSPACE_METADATA_FILENAME = "workspace.json"


def new_workspace_metadata(
    directory: Path, *, created: datetime, updated: datetime,
) -> dict[str, str]:
    path = Path(workspace_from_key(directory.name))
    return {
        "workspace_id": directory.name,
        "path": str(path),
        "name": path.name,
        "created": format_utc(created),
        "updated": format_utc(updated),
    }


def write_workspace_metadata(directory: Path, metadata: dict[str, str]) -> None:
    atomic_write_text(
        directory / WORKSPACE_METADATA_FILENAME,
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
    )


def touch_workspace_metadata(directory: Path) -> None:
    # Different Sessions can write to the same Workspace in separate processes.
    lock = directory.parent / ".locks" / "workspaces" / f"{directory.name}.lock"
    with acquire_file_lock(lock, blocking=True):
        now = datetime.now(timezone.utc)
        if directory.exists():
            path = directory / WORKSPACE_METADATA_FILENAME
            if not path.is_file():
                raise FileNotFoundError(
                    f"missing workspace metadata {path}; "
                    "run scripts/backfill_workspaces.py first"
                )
            metadata = json.loads(path.read_text(encoding="utf-8"))
            metadata["updated"] = format_utc(max(parse_utc(metadata["updated"]), now))
        else:
            metadata = new_workspace_metadata(directory, created=now, updated=now)
        write_workspace_metadata(directory, metadata)

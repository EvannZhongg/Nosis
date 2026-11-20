"""Populate Workspace and Session metadata after stopping Nosis."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.session_paths import default_sessions_directory
from agent_core.session_store import _session_identity_metadata, _write_session_metadata
from agent_core.time_utils import parse_utc
from agent_core.workspace_metadata import (
    WORKSPACE_METADATA_FILENAME,
    new_workspace_metadata,
    workspace_name,
    write_workspace_metadata,
)


def session_history_times(directory: Path) -> tuple[datetime, datetime] | None:
    journal = directory / f"{directory.name}.jsonl"
    files = [path for path in (journal, directory / "session.json") if path.is_file()]
    if not files:
        return None
    stat = files[0].stat()
    created = datetime.fromtimestamp(getattr(stat, "st_birthtime", stat.st_mtime), timezone.utc)
    updated = max(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) for path in files)
    if journal.is_file():
        with journal.open(encoding="utf-8") as file:
            first = next((line for line in file if line.strip()), None)
        if first is not None:
            created = parse_utc(json.loads(first)["timestamp_utc"])
    return created, updated


def backfill_workspace(directory: Path) -> bool:
    metadata_path = directory / WORKSPACE_METADATA_FILENAME
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata["name"]:
            return False
        metadata["name"] = workspace_name(Path(metadata["path"]))
        write_workspace_metadata(directory, metadata)
        return True
    created: list[datetime] = []
    updated: list[datetime] = []
    for session in directory.iterdir():
        if not session.is_dir():
            continue
        times = session_history_times(session)
        if times is not None:
            created.append(times[0])
            updated.append(times[1])
    if not created:
        return False
    earliest = min(created)
    metadata = new_workspace_metadata(
        directory, created=earliest, updated=max(updated + [earliest]),
    )
    write_workspace_metadata(directory, metadata)
    return True


def backfill_session(directory: Path, workspace_id: str) -> bool:
    path = directory / "session.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    fields = {"session_id", "workspace_id", "created", "updated"}
    if fields <= metadata.keys():
        return False
    times = session_history_times(directory)
    assert times is not None  # The metadata file was read above.
    created, updated = times
    if "created" in metadata:
        created = parse_utc(metadata["created"])
    additions = _session_identity_metadata(
        directory.name, workspace_id, created=created, updated=max(created, updated),
    )
    metadata.update({key: value for key, value in additions.items() if key not in metadata})
    _write_session_metadata(directory, metadata)
    return True


def session_directories(parent: Path) -> Iterator[Path]:
    for directory in sorted(parent.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        if (directory / "session.json").is_file():
            yield directory
        children = directory / "subagents"
        if children.is_dir():
            yield from session_directories(children)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions-dir", type=Path, default=default_sessions_directory(),
        help="Session root (default: ~/.nosis/sessions)",
    )
    args = parser.parse_args()
    root = args.sessions_dir.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"Session directory does not exist: {root}")
    workspaces = 0
    sessions = 0
    for directory in sorted(root.iterdir()):
        if directory.is_dir() and not directory.name.startswith("."):
            if backfill_workspace(directory):
                workspaces += 1
            for session in session_directories(directory):
                if backfill_session(session, directory.name):
                    sessions += 1
    print(f"Updated metadata for {workspaces} Workspace(s).")
    print(f"Updated metadata for {sessions} Session(s).")


if __name__ == "__main__":
    main()

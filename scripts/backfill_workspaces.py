"""Populate missing Workspace metadata and empty names after stopping Nosis."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.session_paths import default_sessions_directory
from agent_core.time_utils import parse_utc
from agent_core.workspace_metadata import (
    WORKSPACE_METADATA_FILENAME,
    new_workspace_metadata,
    workspace_name,
    write_workspace_metadata,
)


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
        journal = session / f"{session.name}.jsonl"
        metadata = session / "session.json"
        files = [path for path in (journal, metadata) if path.is_file()]
        if not files:
            continue
        first = None
        if journal.is_file():
            with journal.open(encoding="utf-8") as file:
                first = next((line for line in file if line.strip()), None)
        if first is not None:
            created.append(parse_utc(json.loads(first)["timestamp_utc"]))
        else:
            stat = files[0].stat()
            created.append(datetime.fromtimestamp(
                getattr(stat, "st_birthtime", stat.st_mtime), timezone.utc,
            ))
        updated.extend(
            datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) for path in files
        )
    if not created:
        return False
    earliest = min(created)
    metadata = new_workspace_metadata(
        directory, created=earliest, updated=max(updated + [earliest]),
    )
    write_workspace_metadata(directory, metadata)
    return True


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
    count = 0
    for directory in sorted(root.iterdir()):
        if directory.is_dir() and not directory.name.startswith("."):
            if backfill_workspace(directory):
                count += 1
    print(f"Updated metadata for {count} Workspace(s).")


if __name__ == "__main__":
    main()

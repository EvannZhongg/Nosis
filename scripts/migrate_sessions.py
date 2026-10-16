"""Migrate V1 Session journals to the adjacent V2 Job event format.

Run with Python 3.11+ on Windows, macOS, or Linux after stopping Nosis:
    python scripts/migrate_sessions.py
    python scripts/migrate_sessions.py --sessions-dir /path/to/sessions

This script is only for the V1 -> V2 transition. Future releases must provide
their own adjacent-version migration; never use this script to skip a version.
"""

import argparse
import json
import os
import tempfile
from pathlib import Path


FINISHED_JOB_EVENTS = {
    "job_completed",
    "job_failed",
    "job_cancelled",
    "job_interrupted",
}


def migrate_journal(path: Path) -> bool:
    original = path.read_bytes()
    lines = original.decode("utf-8").splitlines(keepends=True)
    migrated: list[str] = []
    changed = False

    for number, line in enumerate(lines, 1):
        if not line.strip():
            migrated.append(line)
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                raise ValueError("event and payload must be objects")
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{path}:{number}: invalid Journal event") from exc

        payload = event["payload"]
        event_type = event.get("event_type")
        updated = False
        if event_type == "job_submitted" and "details" not in payload:
            # V1 did not record Job details; an empty object means unknown.
            payload["details"] = {}
            updated = True
        elif event_type in FINISHED_JOB_EVENTS:
            if "elapsed_seconds" not in payload:
                # A historical duration cannot be reconstructed from timestamps.
                payload["elapsed_seconds"] = None
                updated = True
            if "result_summary" not in payload:
                payload["result_summary"] = None
                updated = True
            if "progress" not in payload:
                # V1 kept no progress; V2 carries it only in the terminal event.
                payload["progress"] = None
                updated = True

        if updated:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            migrated.append(json.dumps(event, ensure_ascii=False) + ending)
            changed = True
        else:
            migrated.append(line)

    if not changed:
        return False

    backup = path.with_name(path.name + ".v1.bak")
    # Reruns overwrite the backup: a journal migrated by an older revision of
    # this script can still need changes, and the file on disk is the only
    # remaining pre-migration copy at that point.
    with backup.open("wb") as file:
        file.write(original)
        file.flush()
        os.fsync(file.fileno())

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as file:
            temporary = Path(file.name)
            file.write("".join(migrated).encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=Path.home() / ".nosis" / "sessions",
        help="Session root (default: ~/.nosis/sessions)",
    )
    args = parser.parse_args()
    root = args.sessions_dir.expanduser()
    if not root.is_dir():
        parser.error(f"Session directory does not exist: {root}")

    migrated = 0
    skipped = 0
    for path in sorted(root.rglob("*.jsonl")):
        if path.stem != path.parent.name:
            continue
        if migrate_journal(path):
            print(f"Migrated: {path}")
            migrated += 1
        else:
            skipped += 1
    print(f"Migrated {migrated} Session journal(s), skipped {skipped} already up to date.")


if __name__ == "__main__":
    main()

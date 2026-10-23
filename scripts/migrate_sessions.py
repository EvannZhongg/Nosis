"""Migrate Session journals and populate their listing/title metadata.

Run with Python 3.11+ on Windows, macOS, or Linux after stopping Nosis:
    python scripts/migrate_sessions.py
    python scripts/migrate_sessions.py --sessions-dir /path/to/sessions

Missing historical cache counts remain unknown (null), not cache misses (0).
"""

import argparse
import json
import os
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.atomic import atomic_write_text
from agent_core.session import message_from_dict
from agent_core.session_title import initial_session_title


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
        if event_type == "model_completed":
            usage = payload["usage"]
            if usage is not None:
                if not isinstance(usage, dict):
                    raise ValueError(f"{path}:{number}: model_completed usage must be an object or null")
                for field in ("cache_read_tokens", "cache_write_tokens"):
                    if field not in usage:
                        usage[field] = None
                        updated = True
        elif event_type == "job_submitted" and "details" not in payload:
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

    backup = path.with_name(path.name + ".bak")
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


def migrate_metadata(path: Path) -> bool:
    metadata_path = path.parent / "session.json"
    original = metadata_path.read_bytes() if metadata_path.exists() else None
    metadata = json.loads(original) if original is not None else {}
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    updated = dict(metadata)
    if "title" not in updated:
        message = next((
            record["payload"]["message"] for record in records
            if record["event_type"] == "message_appended"
            and record["payload"].get("message", {}).get("role") == "user"
        ), None)
        updated["title"] = (
            initial_session_title(message_from_dict(message), path.stem)
            if message is not None else path.stem
        )
    if "title_source" not in updated:
        updated["title_source"] = "manual" if metadata.get("title") else "initial"
    updated["has_activity"] = any(
        record["event_type"] not in {"permission_preset_changed", "plan_updated"}
        for record in records
    )
    if updated == metadata:
        return False
    if original is not None:
        atomic_write_text(metadata_path.with_name("session.json.bak"), original.decode("utf-8"))
    atomic_write_text(metadata_path, json.dumps(updated, ensure_ascii=False))
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
    metadata_updated = 0
    for path in sorted(root.rglob("*.jsonl")):
        if path.stem != path.parent.name:
            continue
        if migrate_journal(path):
            print(f"Migrated: {path}")
            migrated += 1
        else:
            skipped += 1
        if migrate_metadata(path):
            metadata_updated += 1
    print(f"Migrated {migrated} Session journal(s), skipped {skipped} already up to date.")
    print(f"Updated metadata for {metadata_updated} Session(s).")


if __name__ == "__main__":
    main()

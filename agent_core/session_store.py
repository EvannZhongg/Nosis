"""JSONL append-only runtime journal storage."""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .permissions import PermissionPreset
from .session import JournalEvent, Session
from .session_paths import (
    _validate_session_id,
    default_sessions_directory,
    session_directory,
    session_log_path,
    workspace_directory,
    workspace_from_key,
)

SESSION_METADATA_FILENAME = "session.json"


class JsonlSessionStore:
    """Persist one durable JSON record for every runtime journal event."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        group_by_workspace: bool = True,
    ) -> None:
        self._directory = (
            directory.expanduser().resolve()
            if directory is not None
            else default_sessions_directory()
        )
        self._group_by_workspace = group_by_workspace

    @property
    def directory(self) -> Path:
        return self._directory

    def has_journal(self, session_id: str) -> bool:
        path = self._session_path(session_id)
        return path is not None and path.is_file()

    def delete_session(self, session_id: str) -> bool:
        _validate_session_id(session_id)
        if self._group_by_workspace:
            directory = self._find_session_directory(session_id)
        else:
            directory = self._directory / session_id
        if (
            directory is None
            or not (directory / f"{session_id}.jsonl").is_file()
        ):
            return False
        shutil.rmtree(directory)
        return True

    def list_sessions(self) -> list[dict[str, object]]:
        if not self._directory.is_dir():
            return []
        groups: list[dict[str, object]] = []
        for workspace_dir in self._directory.iterdir():
            if not workspace_dir.is_dir():
                continue
            sessions = []
            for session_dir in workspace_dir.iterdir():
                path = session_dir / f"{session_dir.name}.jsonl"
                if path.is_file():
                    sessions.append(
                        (path.stat().st_mtime_ns, session_dir.name, path)
                    )
            if not sessions:
                continue
            sessions.sort(
                key=lambda entry: (entry[0], entry[1]), reverse=True
            )
            groups.append(
                {
                    "workspace": (
                        workspace_from_key(workspace_dir.name)
                    ),
                    "sessions": [
                        {
                            "session_id": session_id,
                            "title": _session_title(path, session_id),
                        }
                        for _, session_id, path in sessions
                    ],
                    "updated": max(entry[0] for entry in sessions),
                }
            )
        groups.sort(key=lambda group: int(group["updated"]), reverse=True)
        for group in groups:
            group.pop("updated", None)
        return groups

    def load(
        self,
        session_id: str,
        workspace: Path | str | None = None,
        *,
        recover: bool = True,
    ) -> Session:
        path = self._session_path(session_id, workspace)
        session = Session(
            session_id=session_id,
            workspace=self.workspace_for(session_id),
        )
        if path is None or not path.is_file():
            return session
        with path.open(encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # A process can die between writing a record and its
                    # newline.  Earlier fsynced records remain authoritative.
                    if not line.endswith("\n"):
                        break
                    raise
                event = _event_from_dict(record)
                session.apply_event(event)
                session.journal.append(event)
        session.permission_preset = self.permission_preset_for(session_id)
        if recover:
            # Read projections expose stale running work as unknown.  A
            # Runtime attaches a sink first and persists these recovery facts.
            session.recover()
        return session

    def bind_workspace(self, session_id: str, workspace: Path) -> None:
        resolved = workspace.expanduser().resolve()
        target_group = workspace_directory(self._directory, resolved)
        target_group.mkdir(parents=True, exist_ok=True)
        current = self._find_session_directory(session_id)
        target = target_group / session_id
        if current is not None and current != target:
            if target.exists():
                raise ValueError(
                    f"session already exists in workspace: {session_id!r}"
                )
            shutil.move(str(current), str(target))

    def workspace_for(self, session_id: str) -> str | None:
        _validate_session_id(session_id)
        if not self._group_by_workspace:
            # Sessions lie directly in the root, so their parent directory
            # is that root and not an encoded Workspace path.
            return None
        current = self._find_session_directory(session_id)
        if current is None:
            return None
        return workspace_from_key(current.parent.name)

    def permission_preset_for(self, session_id: str) -> PermissionPreset:
        _validate_session_id(session_id)
        if not self._group_by_workspace:
            return PermissionPreset.ASK_FOR_APPROVAL
        current = self._find_session_directory(session_id)
        if current is None:
            return PermissionPreset.ASK_FOR_APPROVAL
        value = _session_metadata(current).get("permission_preset")
        if value is None:
            return PermissionPreset.ASK_FOR_APPROVAL
        return PermissionPreset(str(value))

    def set_permission_preset(
        self,
        session_id: str,
        preset: PermissionPreset,
        workspace: Path | str,
    ) -> None:
        directory = session_directory(self._directory, workspace, session_id)
        directory.mkdir(parents=True, exist_ok=True)
        metadata = _session_metadata(directory)
        metadata["permission_preset"] = preset.value
        _write_session_metadata(directory, metadata)

    def append_events(
        self,
        session_id: str,
        events: Iterable[JournalEvent],
        workspace: Path | str | None = None,
    ) -> None:
        events = tuple(events)
        if not events:
            return
        if workspace is None:
            workspace = self.workspace_for(session_id)
        if workspace is None and self._group_by_workspace:
            raise ValueError(
                f"workspace is required for a new session: {session_id!r}"
            )
        path = self._session_path(session_id, workspace)
        if path is None:
            raise ValueError(f"session has no journal path: {session_id!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            for event in events:
                file.write(
                    json.dumps(_event_to_dict(event), ensure_ascii=False)
                    + "\n"
                )
            file.flush()
            os.fsync(file.fileno())

    def _find_session_directory(self, session_id: str) -> Path | None:
        if not self._group_by_workspace:
            candidate = self._directory / session_id
            return (
                candidate
                if (candidate / f"{session_id}.jsonl").is_file()
                else None
            )
        if not self._directory.is_dir():
            return None
        for group in self._directory.iterdir():
            candidate = group / session_id
            if (
                (candidate / f"{session_id}.jsonl").is_file()
                or (candidate / SESSION_METADATA_FILENAME).is_file()
            ):
                return candidate
        return None

    def _session_path(
        self,
        session_id: str,
        workspace: Path | str | None = None,
    ) -> Path | None:
        if not self._group_by_workspace:
            return self._directory / session_id / f"{session_id}.jsonl"
        if workspace is not None:
            return session_log_path(
                self._directory, workspace, session_id
            )
        bound = self.workspace_for(session_id)
        if bound is None:
            return None
        return session_log_path(self._directory, bound, session_id)


def _event_to_dict(event: JournalEvent) -> dict[str, object]:
    return {
        "seq": event.seq,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "turn_id": event.turn_id,
        "timestamp_utc": _format_utc(event.timestamp_utc),
        "tool_call_id": event.tool_call_id,
        "payload": event.payload,
    }


def _event_from_dict(data: dict[str, object]) -> JournalEvent:
    return JournalEvent(
        seq=int(data["seq"]),
        event_id=str(data["event_id"]),
        event_type=str(data["event_type"]),
        turn_id=(
            data.get("turn_id")
            if isinstance(data.get("turn_id"), str)
            else None
        ),
        timestamp_utc=_parse_utc(str(data["timestamp_utc"])),
        tool_call_id=(
            data.get("tool_call_id")
            if isinstance(data.get("tool_call_id"), str)
            else None
        ),
        payload=dict(data.get("payload") or {}),
    )


def _session_metadata(directory: Path) -> dict[str, object]:
    try:
        data = json.loads(
            (directory / SESSION_METADATA_FILENAME).read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("session workspace metadata must be an object")
    return data


def _write_session_metadata(
    directory: Path,
    metadata: dict[str, object],
) -> None:
    with (directory / SESSION_METADATA_FILENAME).open(
        "w", encoding="utf-8"
    ) as file:
        json.dump(metadata, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())


def _session_title(path: Path, session_id: str) -> str:
    try:
        with path.open(encoding="utf-8") as file:
            for line in file:
                event = _event_from_dict(json.loads(line))
                message = event.payload.get("message")
                if (
                    event.event_type == "message_appended"
                    and isinstance(message, dict)
                    and message.get("role") == "user"
                ):
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        return content
                    return session_id
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return session_id


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )

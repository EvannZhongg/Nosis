"""JSONL append-only runtime journal storage."""

import json
import os
import re
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator, TextIO

from .atomic import atomic_write_text
from .content import text_content
from .permissions import PermissionPreset
from .session import JournalEvent, JournalSink, Session, message_from_dict
from .session_title import initial_session_title
from .session_paths import (
    _validate_session_id,
    default_sessions_directory,
    session_directory,
    session_log_path,
    workspace_directory,
    workspace_from_key,
)
from .time_utils import format_utc as _format_utc, parse_utc as _parse_utc

SESSION_METADATA_FILENAME = "session.json"


class SessionInUseError(RuntimeError):
    """Another writer owns the Session journal."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session '{session_id}' is already in use by another runtime.")


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
        with self._locked_session(session_id):
            path = self._session_path(session_id)
            return path is not None and path.is_file()

    def delete_session(self, session_id: str) -> bool:
        with self._locked_session(session_id):
            if self._group_by_workspace:
                directory = self._find_session_directory(session_id)
            else:
                directory = self._directory / session_id
            if directory is None:
                return False
            shutil.rmtree(directory)
            return True

    def editable_user_messages(
        self,
        session: Session | str,
    ) -> list[dict[str, object]]:
        """Project the latest user-authored turn into a stable fork target."""
        if isinstance(session, str):
            session = self.load(session, recover=False)
        return _editable_user_messages(session)

    def fork_session(
        self,
        source_session_id: str,
        new_session_id: str,
        anchor_id: str,
    ) -> Session:
        """Create a Session from the Journal before its latest user turn."""
        _validate_session_id(source_session_id)
        _validate_session_id(new_session_id)
        if new_session_id == source_session_id:
            raise ValueError(f"session already exists: {new_session_id!r}")
        with self._locked_session(source_session_id):
            source_directory = self._find_session_directory(source_session_id)
            source_path = self._session_path(source_session_id)
            if (
                source_directory is None
                or source_path is None
                or not source_path.is_file()
            ):
                raise ValueError(f"session does not exist: {source_session_id!r}")
            source = self._load_unlocked(source_session_id, recover=False)
            targets = _editable_user_messages(source)
            if not targets or targets[0]["anchor_id"] != anchor_id:
                raise ValueError("editable user message does not exist")
            anchor_event = next(
                event for event in source.journal if event.event_id == anchor_id
            )
            turn_start = next(
                (
                    index
                    for index, event in enumerate(source.journal)
                    if event.event_type == "turn_started"
                    and event.turn_id == anchor_event.turn_id
                ),
                None,
            )
            if turn_start is None:
                raise ValueError("editable user message has no turn boundary")

            workspace = self._workspace_for_unlocked(source_session_id)
            metadata = _session_metadata(source_directory)
            metadata.update(title="", title_source="initial", has_activity=False)
            metadata["forked_from"] = source_session_id
            metadata["fork_anchor_id"] = anchor_id
            prefix = tuple(source.journal[:turn_start])

            with self._locked_session(new_session_id):
                if self._find_session_directory(new_session_id) is not None:
                    raise ValueError(
                        f"session already exists: {new_session_id!r}"
                    )
                if self._group_by_workspace:
                    if workspace is None:
                        raise ValueError("source session has no workspace")
                    target_directory = session_directory(
                        self._directory,
                        workspace,
                        new_session_id,
                    )
                else:
                    target_directory = self._directory / new_session_id
                target_directory.mkdir(parents=True, exist_ok=False)
                try:
                    _write_session_metadata(target_directory, metadata)
                    self._append_events_unlocked(
                        new_session_id,
                        prefix,
                        workspace,
                    )
                    return self._load_unlocked(
                        new_session_id,
                        recover=False,
                    )
                except BaseException:
                    shutil.rmtree(target_directory)
                    raise

    def list_sessions(
        self, *, should_cancel: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        if should_cancel is not None and should_cancel():
            return []
        if not self._directory.is_dir():
            return []
        groups: list[dict[str, object]] = []
        for workspace_dir in self._directory.iterdir():
            if should_cancel is not None and should_cancel():
                return []
            if not workspace_dir.is_dir():
                continue
            sessions = _scan_sessions(workspace_dir, should_cancel=should_cancel)
            if not sessions:
                continue
            groups.append(
                {
                    "workspace": (
                        workspace_from_key(workspace_dir.name)
                    ),
                    "sessions": [
                        summary for _, _, summary in sessions
                    ],
                    "updated": max(entry[0] for entry in sessions),
                }
            )
        groups.sort(key=lambda group: int(group["updated"]), reverse=True)
        for group in groups:
            group.pop("updated", None)
        return groups

    def list_workspace_sessions(
        self, workspace: Path | str
    ) -> list[dict[str, object]]:
        """List one Workspace's Sessions, most recently written first."""
        return [
            summary for _, _, summary in _scan_sessions(
                workspace_directory(self._directory, workspace)
            )
        ]

    def search_messages(
        self, query: str, *, limit: int, per_session_limit: int,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        """Stream conversation text from Journals, discarding cancelled searches."""
        query = query.strip()
        if not query or limit <= 0 or per_session_limit <= 0:
            return []
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        matches: list[dict[str, object]] = []
        for group in self.list_sessions(should_cancel=should_cancel):
            for summary in group["sessions"]:
                if should_cancel is not None and should_cancel():
                    return []
                session_id = summary["session_id"]
                path = session_log_path(self._directory, group["workspace"], session_id)
                session_matches: list[dict[str, object]] = []
                with self._locked_session(session_id):
                    if not path.is_file():
                        continue
                    with path.open(encoding="utf-8") as file:
                        item_index = -1
                        for record in _read_journal_records(file, should_cancel=should_cancel):
                            if record["event_type"] != "message_appended":
                                continue
                            data = record["payload"].get("message")
                            if not isinstance(data, dict):
                                continue
                            item_index += 1
                            if data["role"] not in {"user", "assistant"}:
                                continue
                            message = message_from_dict(data)
                            if message.origin != "conversation":
                                continue
                            text = text_content(message.content)
                            match = pattern.search(text) if text else None
                            if match is None:
                                continue
                            if len(session_matches) == per_session_limit:
                                for result in session_matches:
                                    result["has_more_in_session"] = True
                                break
                            session_matches.append({
                                "session_id": session_id,
                                "title": summary["title"],
                                "workspace": group["workspace"],
                                "item_index": item_index,
                                "role": message.role,
                                "has_more_in_session": False,
                                "text": text,
                                "match_start": match.start(),
                                "match_end": match.end(),
                                "timestamp_utc": (
                                    _format_utc(message.timestamp_utc)
                                    if message.timestamp_utc is not None else None
                                ),
                            })
                if should_cancel is not None and should_cancel():
                    return []
                matches.extend(session_matches)
                if len(matches) >= limit:
                    return matches[:limit]
        return matches

    def load(
        self,
        session_id: str,
        workspace: Path | str | None = None,
        *,
        recover: bool = True,
    ) -> Session:
        with self._locked_session(session_id):
            return self._load_unlocked(
                session_id,
                workspace,
                recover=recover,
            )

    def _load_unlocked(
        self,
        session_id: str,
        workspace: Path | str | None = None,
        *,
        recover: bool,
    ) -> Session:
        path = self._session_path(session_id, workspace)
        current = self._find_session_directory(session_id)
        session = Session(
            session_id=session_id,
            workspace=self._workspace_for_unlocked(session_id),
        )
        if current is not None:
            session.permission_preset = _permission_preset(current)
        if path is None or not path.is_file():
            return session
        with path.open(encoding="utf-8") as file:
            for record in _read_journal_records(file):
                event = _event_from_dict(record)
                session.apply_event(event)
                session.journal.append(event)
        if recover:
            # Read projections expose stale running work as unknown.  A
            # Runtime attaches a sink first and persists these recovery facts.
            session.recover()
        return session

    def bind_workspace(self, session_id: str, workspace: Path) -> None:
        with self._locked_session(session_id):
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
            target.mkdir(parents=True, exist_ok=True)
            metadata_path = target / SESSION_METADATA_FILENAME
            if not metadata_path.exists():
                _write_session_metadata(target, _new_session_metadata())

    def workspace_for(self, session_id: str) -> str | None:
        _validate_session_id(session_id)
        with self._locked_session(session_id):
            return self._workspace_for_unlocked(session_id)

    def _workspace_for_unlocked(self, session_id: str) -> str | None:
        if not self._group_by_workspace:
            # Sessions lie directly in the root, so their parent directory
            # is that root and not an encoded Workspace path.
            return None
        current = self._find_session_directory(session_id)
        if current is None:
            return None
        return workspace_from_key(current.parent.name)

    def set_permission_preset(
        self,
        session_id: str,
        preset: PermissionPreset,
        workspace: Path | str,
    ) -> None:
        with self._locked_session(session_id):
            directory = session_directory(self._directory, workspace, session_id)
            directory.mkdir(parents=True, exist_ok=True)
            metadata = _session_metadata(directory)
            metadata["permission_preset"] = preset.value
            _write_session_metadata(directory, metadata)

    def provider_for(self, session_id: str) -> str | None:
        _validate_session_id(session_id)
        with self._locked_session(session_id):
            if not self._group_by_workspace:
                return None
            current = self._find_session_directory(session_id)
            if current is None:
                return None
            value = _session_metadata(current).get("provider")
            return value if isinstance(value, str) and value else None

    def set_provider(
        self,
        session_id: str,
        provider: str,
        workspace: Path | str,
    ) -> None:
        with self._locked_session(session_id):
            directory = session_directory(self._directory, workspace, session_id)
            directory.mkdir(parents=True, exist_ok=True)
            metadata = _session_metadata(directory)
            metadata["provider"] = provider
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
        with self._locked_session(session_id):
            self._append_events_unlocked(session_id, events, workspace)

    def title_source_for(self, session_id: str) -> str:
        with self._locked_session(session_id):
            directory = self._find_session_directory(session_id)
            if directory is None:
                raise ValueError(f"session does not exist: {session_id!r}")
            return str(_session_metadata(directory)["title_source"])

    def set_title(
        self, session_id: str, title: str, source: str, *,
        expected_source: str | None = None,
    ) -> bool:
        if source not in {"initial", "generated", "manual"} or not title.strip():
            raise ValueError("invalid Session title or source")
        with self._locked_session(session_id):
            directory = self._find_session_directory(session_id)
            if directory is None:
                raise ValueError(f"session does not exist: {session_id!r}")
            metadata = _session_metadata(directory)
            if expected_source is not None and metadata["title_source"] != expected_source:
                return False
            metadata.update(title=title, title_source=source)
            _write_session_metadata(directory, metadata)
            return True

    @contextmanager
    def open_journal(
        self, session_id: str, workspace: Path | str | None = None,
    ) -> Iterator[JournalSink]:
        """Own the active journal until its Runtime releases the writer.

        Windows sharing rules prevent even same-user child processes from
        deleting or overwriting this file while the writer is open. This is
        not a security boundary against a process that can kill the Runtime.
        """
        with self._locked_session(session_id):
            path = self._session_path(session_id, workspace)
            if path is None:
                raise ValueError(f"session has no journal path: {session_id!r}")
            path.parent.mkdir(parents=True, exist_ok=True)
            if not (path.parent / SESSION_METADATA_FILENAME).exists():
                _write_session_metadata(path.parent, _new_session_metadata())
            file = _open_journal_file(path)
        try:
            with self._locked_session(session_id):
                _prepare_journal_append(file)

            def append(events: tuple[JournalEvent, ...]) -> None:
                with self._locked_session(session_id):
                    _write_journal_events(file, events)
                    _update_session_metadata(path.parent, session_id, events)

            yield append
        finally:
            file.close()

    def _append_events_unlocked(
        self,
        session_id: str,
        events: Iterable[JournalEvent],
        workspace: Path | str | None,
    ) -> None:
        if workspace is None:
            workspace = self._workspace_for_unlocked(session_id)
        if workspace is None and self._group_by_workspace:
            raise ValueError(
                f"workspace is required for a new session: {session_id!r}"
            )
        path = self._session_path(session_id, workspace)
        if path is None:
            raise ValueError(f"session has no journal path: {session_id!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not (path.parent / SESSION_METADATA_FILENAME).exists():
            _write_session_metadata(path.parent, _new_session_metadata())
        events = tuple(events)
        with _open_journal_file(path) as file:
            _prepare_journal_append(file)
            _write_journal_events(file, events)
        _update_session_metadata(path.parent, session_id, events)

    @contextmanager
    def _locked_session(self, session_id: str) -> Iterator[None]:
        _validate_session_id(session_id)
        lock_path = self._directory / ".locks" / f"{session_id}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            _lock_file(handle)
            try:
                yield
            finally:
                _unlock_file(handle)

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
        bound = self._workspace_for_unlocked(session_id)
        if bound is None:
            return None
        return session_log_path(self._directory, bound, session_id)


def _open_journal_file(path: Path) -> BinaryIO:
    if os.name != "nt":
        return path.open("a+b")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    # GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ, OPEN_ALWAYS.
    # Do not share writes/deletes or allow the handle to be inherited.
    handle = create_file(str(path), 0xC0000000, 0x1, None, 4, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        if error.winerror == 32:  # ERROR_SHARING_VIOLATION
            raise SessionInUseError(path.stem) from error
        raise error
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDWR | os.O_BINARY | os.O_APPEND | os.O_NOINHERIT,
        )
    except BaseException:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(handle)
        raise
    try:
        return os.fdopen(descriptor, "a+b")
    except BaseException:
        os.close(descriptor)
        raise


def _write_journal_events(file: BinaryIO, events: Iterable[JournalEvent]) -> None:
    for event in events:
        record = json.dumps(_event_to_dict(event), ensure_ascii=False)
        file.write(record.encode("utf-8") + b"\n")
    file.flush()
    os.fsync(file.fileno())


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


def _prepare_journal_append(file: BinaryIO) -> None:
    end = file.seek(0, os.SEEK_END)
    if not end:
        return
    file.seek(end - 1)
    if file.read(1) == b"\n":
        return

    cursor = end
    start = 0
    while cursor:
        chunk_start = max(0, cursor - 4096)
        file.seek(chunk_start)
        chunk = file.read(cursor - chunk_start)
        newline = chunk.rfind(b"\n")
        if newline != -1:
            start = chunk_start + newline + 1
            break
        cursor = chunk_start

    file.seek(start)
    tail = file.read(end - start)
    try:
        json.loads(tail)
    except json.JSONDecodeError:
        # Only an unfinished final record can be discarded during replay.
        file.truncate(start)
    else:
        file.write(b"\n")
    file.seek(0, os.SEEK_END)


def _editable_user_messages(session: Session) -> list[dict[str, object]]:
    message_events = [
        event
        for event in session.journal
        if event.event_type == "message_appended"
    ]
    for anchor in reversed(session.user_anchors):
        if anchor.source != "user_input":
            continue
        event = message_events[anchor.item_index]
        if event.turn_id != anchor.turn_id:
            raise ValueError("user anchor does not match its Journal event")
        return [{"anchor_id": event.event_id, "item_index": anchor.item_index}]
    return []


def _read_journal_records(
    file: TextIO, *, should_cancel: Callable[[], bool] | None = None,
) -> Iterator[dict[str, object]]:
    for line in file:
        if should_cancel is not None and should_cancel():
            return
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # An interrupted append can leave an incomplete final record.
            if not line.endswith("\n"):
                return
            raise
        yield record


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


def _permission_preset(directory: Path) -> PermissionPreset:
    value = _session_metadata(directory).get("permission_preset")
    return (
        PermissionPreset(str(value))
        if value is not None
        else PermissionPreset.ASK_FOR_APPROVAL
    )


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        # Windows byte-range locks are mandatory, so another handle cannot even
        # read the locked byte: size the file without reading it.
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_session_metadata(
    directory: Path,
    metadata: dict[str, object],
) -> None:
    atomic_write_text(
        directory / SESSION_METADATA_FILENAME,
        json.dumps(metadata, ensure_ascii=False),
    )


def _new_session_metadata() -> dict[str, object]:
    return {"title": "", "title_source": "initial", "has_activity": False}


def _update_session_metadata(
    directory: Path, session_id: str, events: tuple[JournalEvent, ...],
) -> None:
    metadata = _session_metadata(directory)
    changed = False
    for event in events:
        if not metadata["has_activity"] and event.event_type not in {
            "permission_preset_changed", "plan_updated",
        }:
            metadata["has_activity"] = True
            changed = True
        message = event.payload.get("message")
        if (
            not metadata["title"] and metadata["title_source"] == "initial"
            and event.event_type == "message_appended"
            and isinstance(message, dict) and message.get("role") == "user"
        ):
            metadata["title"] = initial_session_title(message_from_dict(message), session_id)
            changed = True
    if changed:
        _write_session_metadata(directory, metadata)


def _scan_sessions(
    directory: Path, *, should_cancel: Callable[[], bool] | None = None,
) -> list[tuple[int, str, dict[str, object]]]:
    """List summaries from metadata; journal contents are never opened."""
    if not directory.is_dir():
        return []
    found = []
    for session_dir in directory.iterdir():
        if should_cancel is not None and should_cancel():
            return []
        path = session_dir / f"{session_dir.name}.jsonl"
        if not path.is_file():
            continue
        metadata = _session_metadata(session_dir)
        if metadata["has_activity"]:
            found.append((
                path.stat().st_mtime_ns, session_dir.name,
                _session_summary(session_dir.name, metadata),
            ))
    found.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return found


def _session_summary(session_id: str, metadata: dict[str, object]) -> dict[str, object]:
    return {
        "session_id": session_id,
        "title": metadata["title"] or session_id,
        **(
            {"forked_from": metadata["forked_from"]}
            if isinstance(metadata.get("forked_from"), str)
            else {}
        ),
    }

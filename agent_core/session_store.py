import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .llm import LLMRequest, LLMResponse
from .session import Message, Session
from .content import ImagePart, TextPart
from .session_paths import default_sessions_directory, session_log_path, workspace_directory, _validate_session_id
from .tools import ToolCall


class JsonlSessionStore:
    """Store session transcripts as newline-delimited JSON records.

    Sessions are grouped by workspace, so one root can hold the sessions of
    several workspaces: ``<root>/<WORKSPACE_KEY>/<SESSION_ID>/<SESSION_ID>.jsonl``.
    A store rooted inside a single session, which holds that session's
    sub-agent transcripts, passes ``group_by_workspace=False``: its root
    already belongs to one workspace, so grouping would only repeat the
    workspace key in every path.
    """

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

    def has_transcript(self, session_id: str) -> bool:
        path = self._session_path(session_id)
        return path is not None and path.is_file()

    def delete_session(self, session_id: str) -> bool:
        """Delete a persisted session and return whether it existed."""
        _validate_session_id(session_id)
        if self._group_by_workspace:
            session_dir = self._find_session_directory(session_id)
        else:
            session_dir = self._directory / session_id
            if not (session_dir / f"{session_id}.jsonl").is_file():
                session_dir = None
        if session_dir is None or not session_dir.is_dir():
            return False
        shutil.rmtree(session_dir)
        return True

    def list_sessions(self) -> list[dict[str, object]]:
        """Summarize sessions grouped by their workspace."""
        if not self._directory.is_dir():
            return []
        groups: list[dict[str, object]] = []
        for workspace_dir in self._directory.iterdir():
            if not workspace_dir.is_dir():
                continue
            sessions = []
            for session_dir in workspace_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                path = session_dir / f"{session_dir.name}.jsonl"
                if path.is_file():
                    sessions.append((path.stat().st_mtime_ns, session_dir.name, path))
            if not sessions:
                continue
            sessions.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
            workspace = _workspace_from_metadata(sessions[0][2].parent.parent / "workspace.json")
            groups.append({
                "workspace": workspace or workspace_dir.name,
                "sessions": [{"session_id": sid, "title": _session_title(path, sid)} for _, sid, path in sessions],
                "updated": max(item[0] for item in sessions),
            })
        groups.sort(key=lambda group: int(group["updated"]), reverse=True)
        for group in groups:
            group.pop("updated", None)
        return groups

    def load(self, session_id: str, workspace: Path | str | None = None) -> Session:
        path = self._session_path(session_id, workspace)
        if path is None or not path.is_file():
            return Session(session_id=session_id)
        workspace_value = self.workspace_for(session_id)

        items = []
        archived_summary = None
        archived_item_count = 0
        with path.open(encoding="utf-8") as file:
            for line in file:
                record = json.loads(line)
                items.extend(
                    _message_from_dict(item)
                    for item in record["items"]
                )
                context = record.get("context")
                if isinstance(context, dict):
                    value = context.get("archived_summary")
                    if isinstance(value, str):
                        archived_summary = value
                    count = context.get("archived_item_count")
                    if isinstance(count, int) and not isinstance(count, bool):
                        archived_item_count = count

        return Session(
            session_id=session_id,
            items=items,
            workspace=workspace_value,
            archived_summary=archived_summary,
            archived_item_count=min(max(archived_item_count, 0), len(items)),
        )

    def bind_workspace(self, session_id: str, workspace: Path) -> None:
        """Bind a session to a workspace, moving it between workspace groups."""
        resolved = workspace.expanduser().resolve()
        target_group = workspace_directory(self._directory, resolved)
        target_group.mkdir(parents=True, exist_ok=True)
        current_dir = self._find_session_directory(session_id)
        target_dir = target_group / session_id
        if current_dir is not None and current_dir != target_dir:
            if target_dir.exists():
                raise ValueError(f"session already exists in workspace: {session_id!r}")
            shutil.move(str(current_dir), str(target_dir))
        (target_group / "workspace.json").write_text(
            json.dumps({"workspace": str(resolved)}, ensure_ascii=False), encoding="utf-8"
        )

    def workspace_for(self, session_id: str) -> str | None:
        _validate_session_id(session_id)
        current = self._find_session_directory(session_id)
        if current is not None:
            return _workspace_from_metadata(current.parent / "workspace.json")
        return None

    def _find_session_directory(self, session_id: str) -> Path | None:
        """Return the directory holding a session's transcript, if it has one."""
        if not self._directory.is_dir():
            return None
        for workspace_dir in self._directory.iterdir():
            candidate = workspace_dir / session_id
            if (candidate / f"{session_id}.jsonl").is_file():
                return candidate
        return None

    def append_items(
        self,
        session_id: str,
        items: Sequence[Message],
        archived_summary: str | None = None,
        archived_item_count: int | None = None,
        workspace: Path | str | None = None,
    ) -> None:
        """Append items from a turn that ended without a model response."""
        self._append(
            session_id,
            {
                "session_id": session_id,
                "items": [_message_to_dict(item) for item in items],
            },
            archived_summary,
            archived_item_count,
            workspace,
        )

    def append_turn(
        self,
        session_id: str,
        request: LLMRequest,
        response: LLMResponse,
        items: tuple[Message, ...],
        archived_summary: str | None = None,
        archived_item_count: int | None = None,
        workspace: Path | str | None = None,
    ) -> None:
        record = {
            "session_id": session_id,
            "items": [_message_to_dict(item) for item in items],
            "request": {
                "system_prompt": request.system_prompt,
                "messages": [
                    _message_to_dict(message)
                    for message in request.messages
                ],
            },
            "response": {
                "content": response.content,
                "usage": (
                    {
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        "total_tokens": response.usage.total_tokens,
                    }
                    if response.usage is not None
                    else None
                ),
            },
        }
        if response.reasoning is not None:
            record["response"]["reasoning"] = response.reasoning
        if response.tool_calls:
            record["response"]["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                }
                for tool_call in response.tool_calls
            ]
        if request.max_generation_tokens is not None:
            record["request"]["max_generation_tokens"] = (
                request.max_generation_tokens
            )
        if request.tools:
            record["request"]["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in request.tools
            ]
        self._append(
            session_id,
            record,
            archived_summary,
            archived_item_count,
            workspace,
        )

    def _append(
        self,
        session_id: str,
        record: dict[str, object],
        archived_summary: str | None,
        archived_item_count: int | None,
        workspace: Path | str | None,
    ) -> None:
        if archived_summary is not None or archived_item_count is not None:
            record["context"] = {
                "archived_summary": archived_summary,
                "archived_item_count": archived_item_count or 0,
            }
        if workspace is None:
            workspace = self.workspace_for(session_id)
        if workspace is None and self._group_by_workspace:
            raise ValueError(f"workspace is required for a new session: {session_id!r}")
        path = self._session_path(session_id, workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _session_path(self, session_id: str, workspace: Path | str | None = None) -> Path | None:
        if not self._group_by_workspace:
            return self._directory / session_id / f"{session_id}.jsonl"
        if workspace is not None:
            return session_log_path(self._directory, workspace, session_id)
        bound = self.workspace_for(session_id)
        if bound is None:
            return None
        return session_log_path(self._directory, bound, session_id)


def _workspace_from_metadata(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("workspace") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def _session_title(path: Path, session_id: str) -> str:
    """Use the session's first user message as its title."""
    with path.open(encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            for item in json.loads(line)["items"]:
                if item["role"] == "user" and item.get("content"):
                    content = item["content"]
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        text = "".join(
                            str(part.get("text", ""))
                            for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                        )
                        if text:
                            return text
    return session_id


def _message_to_dict(message: Message) -> dict[str, object]:
    data: dict[str, object] = {
        "role": message.role,
        "content": _content_to_dict(message),
    }
    if message.timestamp_utc is not None:
        data["timestamp_utc"] = _format_utc(message.timestamp_utc)
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": tool_call.arguments,
            }
            for tool_call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    if message.reasoning is not None:
        data["reasoning"] = message.reasoning
    if message.origin != "conversation":
        data["origin"] = message.origin
    return data


def _content_to_dict(message: Message) -> object:
    parts = message.parts
    if not parts:
        return None
    if all(isinstance(part, TextPart) for part in parts):
        return "".join(part.text for part in parts)
    return [
        ({"type": "text", "text": part.text}
         if isinstance(part, TextPart)
         else {"type": "image", "path": part.path, "mime_type": part.mime_type})
        for part in parts
    ]


def _message_from_dict(data: dict[str, object]) -> Message:
    timestamp_utc = data.get("timestamp_utc")
    tool_calls = data.get("tool_calls", [])
    content = data.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                raise ValueError("message content part must be an object")
            if part["type"] == "text":
                parts.append(TextPart(text=str(part.get("text", ""))))
            elif part["type"] == "image":
                parts.append(ImagePart(path=str(part.get("path", "")), mime_type=str(part.get("mime_type", "image/png"))))
            else:
                raise ValueError(f"unknown content part type: {part['type']}")
        content = tuple(parts)
    return Message(
        role=data["role"],
        content=content,
        timestamp_utc=(
            _parse_utc(timestamp_utc)
            if isinstance(timestamp_utc, str)
            else None
        ),
        tool_calls=tuple(
            ToolCall(
                id=tool_call["id"],
                name=tool_call["name"],
                arguments=tool_call["arguments"],
            )
            for tool_call in tool_calls
        ),
        tool_call_id=data.get("tool_call_id"),
        reasoning=data.get("reasoning") if isinstance(data.get("reasoning"), str) else None,
        origin=(
            "tool_media" if data.get("origin") == "tool_media" else "conversation"
        ),
    )


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )

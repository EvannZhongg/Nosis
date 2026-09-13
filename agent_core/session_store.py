import json
from datetime import datetime, timezone
from pathlib import Path

from .llm import LLMRequest, LLMResponse
from .session import Message, Session
from .content import ImagePart, TextPart
from .session_paths import default_sessions_directory, session_directory, session_log_path
from .tools import ToolCall


class JsonlSessionStore:
    def __init__(self, directory: Path | None = None) -> None:
        self._directory = (
            directory.expanduser().resolve()
            if directory is not None
            else default_sessions_directory()
        )

    @property
    def directory(self) -> Path:
        return self._directory

    def list_sessions(self) -> list[dict[str, str]]:
        """Summarize stored sessions, most recently updated first."""
        if not self._directory.is_dir():
            return []

        entries = []
        for directory in self._directory.iterdir():
            if not directory.is_dir():
                continue
            path = session_log_path(self._directory, directory.name)
            if not path.is_file():
                continue
            entries.append((path.stat().st_mtime_ns, directory.name, path))

        entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return [
            {"session_id": session_id, "title": _session_title(path, session_id)}
            for _, session_id, path in entries
        ]

    def load(self, session_id: str) -> Session:
        path = self._session_path(session_id)
        workspace = self.workspace_for(session_id)
        if not path.exists():
            return Session(session_id=session_id, workspace=workspace)

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
            workspace=workspace,
            archived_summary=archived_summary,
            archived_item_count=min(max(archived_item_count, 0), len(items)),
        )

    def bind_workspace(self, session_id: str, workspace: Path) -> None:
        """Persist the workspace associated with a session."""
        path = self._metadata_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        resolved = workspace.expanduser().resolve()
        path.write_text(
            json.dumps({"session_id": session_id, "workspace": str(resolved)}, ensure_ascii=False),
            encoding="utf-8",
        )

    def workspace_for(self, session_id: str) -> str | None:
        path = self._metadata_path(session_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid session metadata: {session_id!r}") from error
        value = data.get("workspace") if isinstance(data, dict) else None
        return value if isinstance(value, str) and value else None

    def append_turn(
        self,
        session_id: str,
        request: LLMRequest,
        response: LLMResponse,
        items: tuple[Message, ...],
        archived_summary: str | None = None,
        archived_item_count: int | None = None,
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
        if request.max_output_tokens is not None:
            record["request"]["max_output_tokens"] = (
                request.max_output_tokens
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
        if archived_summary is not None or archived_item_count is not None:
            record["context"] = {
                "archived_summary": archived_summary,
                "archived_item_count": archived_item_count or 0,
            }
        path = self._session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _session_path(self, session_id: str) -> Path:
        return session_log_path(self._directory, session_id)

    def _metadata_path(self, session_id: str) -> Path:
        return session_directory(self._directory, session_id) / "session.json"


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
    )


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )

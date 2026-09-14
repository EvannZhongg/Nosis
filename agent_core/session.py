from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import uuid4

from .tools import ToolCall
from .content import Content, ImagePart, content_parts


MessageRole = Literal["system", "user", "assistant", "tool"]

#: Where a transcript item came from.  Nearly every item is part of the
#: conversation itself; ``tool_media`` marks a user-role message the
#: runtime synthesized to carry images a tool produced.  Images have to
#: arrive in a user message because a tool-role message cannot hold
#: them, so the origin is what distinguishes them from something the
#: person typed -- it keeps them out of the UI's conversation and out of
#: what a sub-agent inherits.
MessageOrigin = Literal["conversation", "tool_media"]


@dataclass(frozen=True)
class Message:
    role: MessageRole
    content: Content
    timestamp_utc: datetime | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning: str | None = None
    origin: MessageOrigin = "conversation"

    @property
    def parts(self):
        return content_parts(self.content)

    @property
    def is_tool_media(self) -> bool:
        return self.origin == "tool_media"


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: str(uuid4()))
    items: list[Message] = field(default_factory=list)
    # Absolute workspace directory bound to this session.  Kept as metadata
    # separate from transcript items so switching sessions never changes the
    # runtime's working directory.
    workspace: str | None = None
    # Number of transcript items represented by ``archived_summary``.
    # Keeping the complete transcript here lets UIs render history while the
    # agent sends only the unarchived tail to the model.
    archived_summary: str | None = None
    archived_item_count: int = 0

    @property
    def recent_items(self) -> list[Message]:
        return self.items[self.archived_item_count :]

    def set_archived_summary(
        self,
        summary: str,
        item_count: int | None = None,
    ) -> None:
        self.archived_summary = summary
        self.archived_item_count = (
            len(self.items)
            if item_count is None
            else max(0, min(item_count, len(self.items)))
        )

    def add_item(
        self,
        role: MessageRole,
        content: Content,
        timestamp_utc: datetime | None = None,
        tool_calls: tuple[ToolCall, ...] = (),
        tool_call_id: str | None = None,
        reasoning: str | None = None,
        attachments: tuple[ImagePart, ...] = (),
        origin: MessageOrigin = "conversation",
    ) -> None:
        if attachments:
            parts = list(content_parts(content))
            parts.extend(attachments)
            content = tuple(parts)
        self.items.append(
            Message(
                role=role,
                content=content,
                timestamp_utc=timestamp_utc,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
                reasoning=reasoning,
                origin=origin,
            )
        )

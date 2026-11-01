"""Channel-neutral cards and the transport boundary used by the mobile UI."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Button:
    label: str
    token: str


@dataclass(frozen=True)
class Card:
    title: str
    text: str
    buttons: list[Button] = field(default_factory=list)


class Transport(Protocol):
    async def send(self, chat_id: str, card: Card) -> str: ...
    async def update(self, message_id: str, card: Card) -> None: ...
    async def react(self, message_id: str, emoji: str) -> str: ...
    async def unreact(self, message_id: str, reaction_id: str) -> None: ...
    async def create_streaming_card(self, chat_id: str, text: str) -> object: ...
    async def stream_text(self, handle: object, text: str) -> None: ...
    async def close_stream(self, handle: object) -> None: ...
    async def download_attachment(self, message_id: str, attachment: dict, path: Path) -> None: ...
    async def send_attachment(self, chat_id: str, attachment: dict, path: Path) -> str: ...

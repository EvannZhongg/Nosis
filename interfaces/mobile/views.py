"""Channel-neutral cards and the transport boundary used by the mobile UI."""

from dataclasses import dataclass, field
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
    async def send_markdown(self, chat_id: str, text: str) -> str: ...
    async def update_markdown(self, message_id: str, text: str) -> None: ...

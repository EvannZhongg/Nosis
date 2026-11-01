"""Coalesce assistant deltas into streaming cards."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from .views import Transport


# Leave room for the card envelope under Feishu's 30 KB card limit.
MAX_TEXT_BYTES = 24000
UPDATE_INTERVAL_SECONDS = 1.0
log = logging.getLogger(__name__)


class TextStream:
    def __init__(self, transport: Transport, chat_id: str,
                 on_started: Callable[[], Awaitable[None]] | None = None) -> None:
        self.transport = transport
        self.chat_id = chat_id
        self.content = ""
        self._cards: list[tuple[object, str]] = []
        self._on_started = on_started
        self._closed = False
        self._changed = asyncio.Event()
        self._finished = asyncio.Event()
        self._task: asyncio.Task | None = None

    def append(self, text: str) -> None:
        if not text:
            return
        self.content += text
        if self.content.strip():
            self._wake()

    def _wake(self) -> None:
        self._changed.set()
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def finish(self, content: str | None = None) -> None:
        if self._closed:
            return
        if content is not None:
            self.content = content
        self._finished.set()
        if self.content.strip() or self._task is not None:
            self._wake()
        if self._task is not None:
            await self._task
        await self._close()

    async def abort(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._close()

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for handle, _ in self._cards:
            try:
                await self.transport.close_stream(handle)
            except Exception:
                log.exception("Mobile stream closing failed: %s", self.chat_id)

    async def _run(self) -> None:
        while True:
            await self._changed.wait()
            self._changed.clear()
            try:
                await self._flush()
            except Exception:
                log.exception("Mobile text delivery failed: %s", self.chat_id)
            if self._finished.is_set():
                if self._changed.is_set():
                    continue
                return
            try:
                await asyncio.wait_for(self._finished.wait(), UPDATE_INTERVAL_SECONDS)
            except TimeoutError:
                pass

    async def _flush(self) -> None:
        remaining = self.content
        index = 0
        while remaining or index < len(self._cards):
            # Count escaped UTF-8 bytes rather than characters (CJK, emoji,
            # quotes and code blocks have different serialized sizes).
            low, high = 0, min(len(remaining), MAX_TEXT_BYTES)
            while low < high:
                middle = (low + high + 1) // 2
                size = len(json.dumps(remaining[:middle], ensure_ascii=False).encode("utf-8"))
                if size <= MAX_TEXT_BYTES:
                    low = middle
                else:
                    high = middle - 1
            text, remaining = remaining[:low] or " ", remaining[low:]
            if index == len(self._cards):
                handle = await self.transport.create_streaming_card(self.chat_id, text)
                self._cards.append((handle, text))
                if self._on_started is not None:
                    await self._on_started()
                    self._on_started = None
            else:
                handle, sent = self._cards[index]
                if text != sent:
                    await self.transport.stream_text(handle, text)
                    self._cards[index] = (handle, text)
            index += 1

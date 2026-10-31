"""Coalesce assistant deltas into editable body messages."""

import asyncio

from .views import Transport


MAX_TEXT_CHARACTERS = 6000
MAX_MESSAGE_EDITS = 20
UPDATE_INTERVAL_SECONDS = 1.0


class TextStream:
    def __init__(self, transport: Transport, chat_id: str) -> None:
        self.transport = transport
        self.chat_id = chat_id
        self.content = ""
        self._offset = 0
        self._message_id: str | None = None
        self._sent = ""
        self._edits = 0
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
        if content is not None:
            self.content = content
        self._finished.set()
        if self.content.strip() or self._task is not None:
            self._wake()
        if self._task is not None:
            await self._task

    async def abort(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await self._changed.wait()
            self._changed.clear()
            await self._flush()
            if self._finished.is_set():
                if self._changed.is_set():
                    continue
                return
            try:
                await asyncio.wait_for(self._finished.wait(), UPDATE_INTERVAL_SECONDS)
            except TimeoutError:
                pass

    async def _flush(self) -> None:
        while self._offset < len(self.content):
            text = self.content[self._offset:self._offset + MAX_TEXT_CHARACTERS]
            if self._message_id is None:
                self._message_id = await self.transport.send_text(self.chat_id, text)
                self._sent = text
                self._edits = 0
            elif text != self._sent:
                # Feishu allows at most 20 edits per ordinary message. Freeze
                # the published prefix and continue in a new message.
                if self._edits == MAX_MESSAGE_EDITS:
                    self._offset += len(self._sent)
                    self._message_id = None
                    self._sent = ""
                    continue
                await self.transport.update_text(self._message_id, text)
                self._sent = text
                self._edits += 1
            if len(text) == MAX_TEXT_CHARACTERS:
                self._offset += len(text)
                self._message_id = None
                self._sent = ""
                continue
            return

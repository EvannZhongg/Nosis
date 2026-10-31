"""Feishu card rendering and the official Node SDK transport process."""

import asyncio
import json
import logging
import shutil
from pathlib import Path
from uuid import uuid4

from .config import ChannelConfig
from .views import Card


log = logging.getLogger(__name__)


def render_card(card: Card) -> dict:
    elements = [{"tag": "markdown", "content": card.text or " "}]
    for offset in range(0, len(card.buttons), 3):
        elements.append({"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": button.label[:100]},
             "type": "default", "value": {"token": button.token}}
            for button in card.buttons[offset:offset + 3]
        ]})
    return {"config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": card.title}},
            "elements": elements}


class FeishuTransport:
    def __init__(self, config: ChannelConfig) -> None:
        self.config = config
        self.process = None
        self.reader = None
        self.dispatcher = None
        self.controller = None
        self.pending: dict[str, asyncio.Future] = {}
        self.inbound: asyncio.Queue = asyncio.Queue()

    async def start(self, controller) -> None:
        node = shutil.which("node")
        bundle = Path(__file__).parent / "dist" / "feishu.cjs"
        if node is None or not bundle.is_file():
            raise RuntimeError("Feishu requires Node.js 22+ and its bundle. Run npm install && npm run build --workspace @nosis/mobile.")
        self.controller = controller
        self.process = await asyncio.create_subprocess_exec(
            node, str(bundle), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
        self.reader = asyncio.create_task(self._read())
        self.dispatcher = asyncio.create_task(self._dispatch())
        try:
            await self._request("start", app_id=self.config.app_id, app_secret=self.config.app_secret)
        except BaseException:
            await self.close()
            raise

    async def _request(self, op: str, *, timeout: float = 30, **fields) -> dict:
        if self.process is None or self.process.returncode is not None:
            raise RuntimeError("Feishu transport is not running")
        request_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self.process.stdin.write((json.dumps({"id": request_id, "op": op, **fields}, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def _read(self) -> None:
        failure = RuntimeError("Feishu transport exited")
        try:
            buffer = bytearray()
            while chunk := await self.process.stdout.read(65536):
                buffer.extend(chunk)
                while (newline := buffer.find(b"\n")) >= 0:
                    event = json.loads(bytes(buffer[:newline]))
                    del buffer[:newline + 1]
                    request_id = event.get("id")
                    if request_id is not None:
                        future = self.pending.get(request_id)
                        if future is not None and not future.done():
                            if "error" in event:
                                future.set_exception(RuntimeError(event["error"]))
                            else:
                                future.set_result(event["result"])
                    else:
                        self.inbound.put_nowait(event)
        except Exception as error:
            failure = error
            log.exception("Feishu transport reader failed")
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)
            self.inbound.put_nowait(None)

    async def _dispatch(self) -> None:
        while (event := await self.inbound.get()) is not None:
            try:
                if event["type"] == "message":
                    if event["text"] is None:
                        if self.controller.authorized(event["user_id"]):
                            await self.send(event["chat_id"], Card("暂不支持此消息", "请发送文字指令或文字回复。"))
                    else:
                        await self.controller.receive_text(event["chat_id"], event["user_id"], event["text"], event["message_id"])
                elif event["type"] == "action" and isinstance(event.get("token"), str):
                    await self.controller.receive_action(event["chat_id"], event["user_id"], event["message_id"], event["token"])
                elif event["type"] == "connection_error":
                    log.error("%s: %s", self.config.id, event["error"])
            except Exception:
                log.exception("Feishu inbound delivery failed")

    async def send(self, chat_id: str, card: Card) -> str:
        result = await self._request("send", chat_id=chat_id, card=render_card(card))
        return result["message_id"]

    async def update(self, message_id: str, card: Card) -> None:
        await self._request("update", message_id=message_id, card=render_card(card))

    async def send_text(self, chat_id: str, text: str) -> str:
        result = await self._request("send_text", chat_id=chat_id, text=text)
        return result["message_id"]

    async def update_text(self, message_id: str, text: str) -> None:
        await self._request("update_text", message_id=message_id, text=text)

    async def close(self) -> None:
        if self.dispatcher is not None:
            self.dispatcher.cancel()
            await asyncio.gather(self.dispatcher, return_exceptions=True)
            self.dispatcher = None
        if self.process is not None:
            if self.process.returncode is None:
                try:
                    if self.reader is not None and self.reader.done():
                        self.process.stdin.close()
                    else:
                        await self._request("close", timeout=5)
                except (RuntimeError, BrokenPipeError, ConnectionResetError, TimeoutError):
                    if self.process.returncode is None:
                        self.process.kill()
                await self.process.wait()
            if self.reader is not None:
                await self.reader
            self.process = self.reader = None

import asyncio
import unittest
from unittest.mock import patch

from interfaces.mobile.streaming import TextStream


class TextTransport:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.changed = asyncio.Event()

    async def send_markdown(self, chat_id, text):
        self.messages.append(text)
        self.edits.append(0)
        self.changed.set()
        return len(self.messages) - 1

    async def update_markdown(self, message_id, text):
        self.messages[message_id] = text
        self.edits[message_id] += 1
        self.changed.set()


class StreamingTest(unittest.IsolatedAsyncioTestCase):
    async def test_leading_whitespace_waits_for_visible_text(self):
        transport = TextTransport()
        stream = TextStream(transport, "chat")
        stream.append("\n\n")
        await asyncio.sleep(0)
        self.assertEqual(transport.messages, [])
        stream.append("正文")
        await stream.finish()
        self.assertEqual(transport.messages, ["\n\n正文"])

    async def test_partial_text_is_visible_before_completion_and_deltas_are_coalesced(self):
        transport = TextTransport()
        stream = TextStream(transport, "chat")
        self.addAsyncCleanup(stream.abort)
        stream.append("开头")
        await asyncio.wait_for(transport.changed.wait(), 1)
        self.assertEqual(transport.messages, ["开头"])
        for _ in range(100):
            stream.append("增量")
        await stream.finish("开头" + "增量" * 100)
        self.assertEqual(transport.messages, ["开头" + "增量" * 100])
        self.assertEqual(transport.edits, [1])

    async def test_edit_limit_continues_in_another_message_without_repeating_text(self):
        transport = TextTransport()
        stream = TextStream(transport, "chat")
        self.addAsyncCleanup(stream.abort)
        with patch("interfaces.mobile.streaming.UPDATE_INTERVAL_SECONDS", 0):
            for _ in range(45):
                transport.changed.clear()
                stream.append("字")
                await asyncio.wait_for(transport.changed.wait(), 1)
            await stream.finish()
        self.assertEqual("".join(transport.messages), "字" * 45)
        self.assertEqual(len(transport.messages), 3)
        self.assertLessEqual(max(transport.edits), 20)

    async def test_long_unicode_text_is_split_without_loss(self):
        transport = TextTransport()
        stream = TextStream(transport, "chat")
        text = "中文🙂" * 5000
        await stream.finish(text)
        self.assertEqual("".join(transport.messages), text)
        self.assertTrue(all(len(message) <= 6000 for message in transport.messages))

    async def test_cancel_flushes_partial_text_and_detach_stops_updates(self):
        transport = TextTransport()
        stream = TextStream(transport, "chat")
        stream.append("部分输出")
        await stream.finish()
        self.assertEqual(transport.messages, ["部分输出"])
        second = TextStream(transport, "chat")
        transport.changed.clear()
        second.append("已发送")
        await asyncio.wait_for(transport.changed.wait(), 1)
        second.append("未发送")
        await second.abort()
        self.assertEqual(transport.messages, ["部分输出", "已发送"])

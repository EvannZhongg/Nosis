import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from interfaces.mobile.streaming import MAX_TEXT_BYTES, TextStream


class TextTransport:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.closed = []
        self.changed = asyncio.Event()

    async def create_streaming_card(self, chat_id, text):
        self.messages.append(text)
        self.edits.append(0)
        self.changed.set()
        return len(self.messages) - 1

    async def stream_text(self, message_id, text):
        self.messages[message_id] = text
        self.edits[message_id] += 1
        self.changed.set()

    async def close_stream(self, handle):
        self.closed.append(handle)


class StreamingTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_update_keeps_the_same_card(self):
        transport = TextTransport()
        stream = TextStream(transport, "chat")
        stream.append("partial")
        await asyncio.wait_for(transport.changed.wait(), 1)
        transport.stream_text = AsyncMock(side_effect=RuntimeError("edit failed"))
        with self.assertLogs("interfaces.mobile.streaming", level="ERROR"):
            await stream.finish("complete")
        self.assertEqual(transport.messages, ["partial"])
        self.assertEqual(transport.closed, [0])

    async def test_failed_send_does_not_escape_finish_or_leave_unobserved_task(self):
        transport = TextTransport()
        transport.create_streaming_card = AsyncMock(side_effect=RuntimeError("offline"))
        stream = TextStream(transport, "chat")
        with self.assertLogs("interfaces.mobile.streaming", level="ERROR"):
            await stream.finish("reply")
        await stream.abort()

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

    async def test_more_than_twenty_updates_stay_in_one_card(self):
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
        self.assertEqual(len(transport.messages), 1)
        self.assertEqual(transport.edits, [44])
        self.assertEqual(transport.closed, [0])

    async def test_long_unicode_text_is_split_without_loss(self):
        transport = TextTransport()
        stream = TextStream(transport, "chat")
        text = ('中文🙂"\n\\' * 5000)
        await stream.finish(text)
        self.assertEqual("".join(transport.messages), text)
        self.assertTrue(all(len(json.dumps(message, ensure_ascii=False).encode("utf-8")) <= MAX_TEXT_BYTES for message in transport.messages))
        self.assertEqual(len(transport.closed), len(transport.messages))

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

    async def test_final_content_can_correct_earlier_segments(self):
        transport = TextTransport()
        stream = TextStream(transport, "chat")
        stream.append("a" * 30000)
        await asyncio.wait_for(transport.changed.wait(), 1)
        await stream.finish("corrected")
        self.assertEqual(transport.messages, ["corrected", " "])
        self.assertEqual(transport.closed, [0, 1])
        await stream.finish()
        await stream.abort()
        self.assertEqual(transport.closed, [0, 1])

    async def test_started_callback_waits_for_successful_publication(self):
        transport = TextTransport()
        started = AsyncMock()
        stream = TextStream(transport, "chat", started)
        stream.append(" ")
        started.assert_not_called()
        await stream.finish("reply")
        started.assert_awaited_once()
        self.assertEqual(transport.messages, ["reply"])

    async def test_abort_closes_without_publishing_pending_text(self):
        transport = TextTransport()
        stream = TextStream(transport, "chat")
        stream.append("visible")
        await asyncio.wait_for(transport.changed.wait(), 1)
        stream.append("pending")
        await stream.abort()
        self.assertEqual(transport.messages, ["visible"])
        self.assertEqual(transport.closed, [0])

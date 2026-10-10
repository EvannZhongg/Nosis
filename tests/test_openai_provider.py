import json
import tempfile
import threading
import unittest
import warnings
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from agent_core import (
    AgentCancelled, ImagePart, JsonlSessionStore, LLMRequest, Message,
    ProviderProtocolError, ProviderStreamError, Session, TextPart,
    TokenUsage, ToolCall, ToolDefinition,
)
from agent_core.providers import OpenAIProvider
from agent_core.session import message_to_dict
from tests.test_media import png_bytes


TOOL = ToolDefinition(
    name="probe", description="Return a test value.",
    parameters={"type": "object", "properties": {"value": {"type": "string"}},
                "required": ["value"]},
)


def output_message(text="OK"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def function_call(arguments='{"value":"ping"}', call_id="call_1"):
    return {"id": "fc_1", "type": "function_call", "status": "completed",
            "call_id": call_id, "name": "probe", "arguments": arguments}


def response_event(output=None, event_type="response.completed", status="completed", **fields):
    return {"type": event_type, "sequence_number": 1, "response": {
        "id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-6-astra",
        "status": status, "output": output if output is not None else [output_message()],
        "usage": {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 2}},
        **fields,
    }}


def sse(events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()


def chat_chunk(delta=None, finish=None, usage=None):
    return {"id": "chat_1", "object": "chat.completion.chunk", "created": 1,
            "model": "test-model", "choices": [] if usage else [
                {"index": 0, "delta": delta or {}, "finish_reason": finish}],
            "usage": usage}


class OpenAIProviderTest(unittest.TestCase):
    def setUp(self):
        metadata = patch("agent_core.providers.base.get_model_info", return_value={
            "max_input_tokens": 922000, "max_output_tokens": 128000, "supports_vision": True,
        })
        metadata.start()
        self.addCleanup(metadata.stop)

    def provider(self, api="responses", **kwargs):
        return OpenAIProvider(model="gpt-6-astra", base_url="https://relay.example/v1",
                              api_key="test-key", api=api, max_retries=0, **kwargs)

    def request(self, **kwargs):
        return LLMRequest(system_prompt="Test instructions", messages=(Message("user", "ping"),),
                          tools=(TOOL,), **kwargs)

    @contextmanager
    def transport(self, *streams):
        requests = []
        remaining = iter(streams)

        def handle(request):
            requests.append(request)
            body = next(remaining)
            options = {"content": sse(body)} if isinstance(body, list) else {"stream": body}
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, **options)

        def client(**kwargs):
            return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

        with patch("agent_core.providers.openai_provider.OpenAI", side_effect=client):
            yield requests

    def test_final_response_supplies_tools_and_typed_usage_without_deltas(self):
        output = [function_call(), function_call("{}", "call_2")]
        with self.transport([response_event(output)]) as requests, warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            result = self.provider(reasoning_effort="high").stream(
                self.request(max_generation_tokens=100), lambda _: None,
            )
        self.assertEqual(result.tool_calls, (ToolCall("call_1", "probe", {"value": "ping"}),
                                            ToolCall("call_2", "probe", {})))
        self.assertEqual(result.usage, TokenUsage(12, 5, 17))
        self.assertFalse(seen)
        self.assertEqual(str(requests[0].url), "https://relay.example/v1/responses")
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "gpt-6-astra")
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["max_output_tokens"], 100)
        self.assertFalse(body["store"])
        self.assertFalse(body["tools"][0]["strict"])
        self.assertNotIn("tool_choice", body)
        self.assertNotIn("stream_options", body)

    def test_streams_text_and_reasoning_but_uses_completed_content(self):
        events = [
            {"type": "response.reasoning_summary_text.delta", "delta": "Thinking"},
            {"type": "response.output_text.delta", "delta": "O"},
            {"type": "response.output_text.delta", "delta": "K"},
            response_event(),
        ]
        text, reasoning = [], []
        with self.transport(events):
            result = self.provider().stream(self.request(), text.append, reasoning.append)
        self.assertEqual(text, ["O", "K"])
        self.assertEqual(reasoning, ["Thinking"])
        self.assertEqual(result.content, "OK")
        self.assertEqual(result.reasoning, "Thinking")

    def test_encrypted_reasoning_and_tool_outputs_survive_journal_reload(self):
        output = [{"id": "rs_1", "type": "reasoning", "summary": [],
                   "encrypted_content": "encrypted-state"}, function_call()]
        with self.transport([response_event(output)], [response_event()]) as requests:
            provider = self.provider()
            result = provider.stream(self.request(), lambda _: None)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = JsonlSessionStore(root / "sessions")
                session = Session("test-session")
                session.begin_turn("turn-1")
                session.add_item("user", "ping")
                session.add_item("assistant", None, tool_calls=result.tool_calls,
                                 provider_data=result.provider_data)
                session.add_item("tool", "pong", tool_call_id="call_1")
                session.finish_turn("completed")
                store.bind_workspace(session.session_id, root)
                store.append_events(session.session_id, session.journal, workspace=root)
                restored = store.load(session.session_id)
                self.assertEqual(restored.items[1].provider_data, result.provider_data)
                self.assertNotIn("provider_data", message_to_dict(restored.items[1], include_provider_data=False))
                provider.stream(LLMRequest("Test instructions", tuple(restored.items), (TOOL,)), lambda _: None)
        inputs = json.loads(requests[1].content)["input"]
        self.assertEqual(inputs[1:3], output)
        self.assertEqual(inputs[3], {"type": "function_call_output", "call_id": "call_1", "output": "pong"})

    def test_conversation_from_other_protocol_is_serialized_as_native_input(self):
        messages = (Message("assistant", "calling", tool_calls=(ToolCall("call_1", "probe", {}),)),
                    Message("tool", "pong", tool_call_id="call_1"))
        with self.transport([response_event()]) as requests:
            self.provider().stream(LLMRequest("Test", messages), lambda _: None)
        inputs = json.loads(requests[0].content)["input"]
        self.assertEqual(inputs[0], {"role": "assistant", "content": "calling"})
        self.assertEqual(inputs[1], {"type": "function_call", "call_id": "call_1",
                                     "name": "probe", "arguments": "{}"})

    def test_responses_images_use_native_content_types(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image.png").write_bytes(png_bytes(32, 32))
            request = LLMRequest("Test", (Message("user", (TextPart("inspect"), ImagePart("image.png"))),),
                                 media_root=root)
            with self.transport([response_event()]) as requests:
                self.provider().stream(request, lambda _: None)
        parts = json.loads(requests[0].content)["input"][0]["content"]
        self.assertEqual(parts[0], {"type": "input_text", "text": "inspect"})
        self.assertEqual(parts[1]["type"], "input_image")
        self.assertTrue(parts[1]["image_url"].startswith("data:image/png;base64,"))

    def test_does_not_accept_truncated_or_failed_responses(self):
        cases = [
            ([{"type": "response.output_text.delta", "delta": "partial"}], "missing_completed_event"),
            ([response_event(event_type="response.incomplete", status="incomplete",
                             incomplete_details={"reason": "max_output_tokens"})], "response.incomplete"),
            ([response_event(event_type="response.failed", status="failed",
                             error={"code": "server_error", "message": "failed"})], "response.failed"),
            ([response_event(status="incomplete")], "incomplete_response"),
            ([{"type": "error", "code": "server_error", "message": "failed", "param": None}], "api_error"),
        ]
        for events, reason in cases:
            with self.subTest(reason=reason), self.transport(events) as requests:
                with self.assertRaises(ProviderStreamError) as raised:
                    self.provider().stream(self.request(), lambda _: None)
                self.assertEqual(raised.exception.details["reason"], reason)
                self.assertEqual(len(requests), 1)

    def test_rejects_invalid_tool_arguments_without_repeating_request(self):
        for arguments in ("", "{", "[]", "null"):
            with self.subTest(arguments=arguments), self.transport([response_event([function_call(arguments)])]) as requests:
                with self.assertRaises(ProviderProtocolError):
                    self.provider().stream(self.request(), lambda _: None)
                self.assertEqual(len(requests), 1)

    def test_rejects_tool_without_identity(self):
        call = function_call()
        call["call_id"] = ""
        with self.transport([response_event([call])]):
            with self.assertRaises(ProviderProtocolError):
                self.provider().stream(self.request(), lambda _: None)

    def test_streams_refusal(self):
        output = output_message()
        output["content"] = [{"type": "refusal", "refusal": "Declined"}]
        text = []
        with self.transport([{"type": "response.refusal.delta", "delta": "Declined"}, response_event([output])]):
            result = self.provider().stream(self.request(), text.append)
        self.assertEqual(result.content, "Declined")
        self.assertEqual(text, ["Declined"])

    def test_chat_protocol_assembles_fragments_and_requests_usage(self):
        events = [chat_chunk({"content": "Calling", "reasoning_content": "Thinking", "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "probe", "arguments": '{"value":'}}]}),
            chat_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"ping"}'}}]}, "tool_calls"),
            chat_chunk(usage={"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17})]
        text, reasoning = [], []
        with self.transport(events) as requests:
            result = self.provider(api="chat_completions", reasoning_effort="high", reasoning_parameters={
                "thinking": {"type": "enabled"}, "extra_body": {"enable_thinking": True},
            }).stream(self.request(max_generation_tokens=100), text.append, reasoning.append)
        self.assertEqual(result.tool_calls, (ToolCall("call_1", "probe", {"value": "ping"}),))
        self.assertEqual(result.usage, TokenUsage(12, 5, 17))
        self.assertEqual(text, ["Calling"])
        self.assertEqual(reasoning, ["Thinking"])
        self.assertEqual(str(requests[0].url), "https://relay.example/v1/chat/completions")
        body = json.loads(requests[0].content)
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertTrue(body["enable_thinking"])
        self.assertEqual(body["max_completion_tokens"], 100)
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_chat_requires_successful_finish(self):
        for finish in (None, "length", "content_filter"):
            with self.subTest(finish=finish), self.transport([chat_chunk({"content": "partial"}, finish)]):
                with self.assertRaises(ProviderStreamError):
                    self.provider(api="chat_completions").stream(self.request(), lambda _: None)

    def test_midstream_transport_failure_is_not_retried(self):
        class BrokenStream(httpx.SyncByteStream):
            closed = False

            def __iter__(self):
                yield sse([{"type": "response.output_text.delta", "delta": "partial"}])
                raise httpx.ReadError("disconnected")

            def close(self):
                self.closed = True

        stream, text = BrokenStream(), []
        with self.transport(stream) as requests:
            with self.assertRaises(ProviderStreamError):
                self.provider().stream(self.request(), text.append)
        self.assertTrue(stream.closed)
        self.assertEqual(text, ["partial"])
        self.assertEqual(len(requests), 1)

    def test_completed_response_finishes_before_connection_disconnects(self):
        class CompletedStream(httpx.SyncByteStream):
            closed = False

            def __iter__(self):
                yield sse([response_event()])
                raise httpx.ReadError("disconnected after completion")

            def close(self):
                self.closed = True

        stream = CompletedStream()
        with self.transport(stream) as requests:
            result = self.provider().stream(self.request(), lambda _: None)
        self.assertEqual(result.content, "OK")
        self.assertEqual(result.usage, TokenUsage(12, 5, 17))
        self.assertTrue(stream.closed)
        self.assertEqual(len(requests), 1)

    def test_cancellation_closes_blocked_http_stream(self):
        entered, closed = threading.Event(), threading.Event()

        class BlockingStream(httpx.SyncByteStream):
            def __iter__(self):
                entered.set()
                closed.wait(2)
                return
                yield

            def close(self):
                closed.set()

        def cancel():
            if entered.is_set():
                raise AgentCancelled("cancelled")

        with self.transport(BlockingStream()):
            with self.assertRaises(AgentCancelled):
                self.provider().stream_cancellable(self.request(), lambda _: None, None, cancel)
        self.assertTrue(closed.is_set())

    def test_keeps_live_model_metadata_and_token_estimation(self):
        provider = self.provider()
        self.assertEqual(provider.max_context_tokens, 922000)
        self.assertEqual(provider.max_output_tokens, 128000)
        self.assertIn("image", provider.capabilities.input_modalities)
        with patch("agent_core.providers.base.token_counter", return_value=10) as counter:
            self.assertEqual(provider.count_input_tokens(self.request()), 10)
        self.assertEqual(counter.call_args.kwargs["model"], "gpt-6-astra")

    def test_counts_native_reasoning_tokens_in_retained_context(self):
        with self.transport([response_event([{"id": "rs_1", "type": "reasoning", "summary": [],
                                             "encrypted_content": "encrypted-state"}, output_message()])]):
            result = self.provider().stream(self.request(), lambda _: None)
        message = Message("assistant", result.content, provider_data=result.provider_data)
        request = LLMRequest("Test", (message,))
        with patch("agent_core.providers.base.token_counter", return_value=10):
            self.assertEqual(self.provider().count_input_tokens(request), 12)
            self.assertEqual(self.provider(api="chat_completions").count_input_tokens(request), 10)

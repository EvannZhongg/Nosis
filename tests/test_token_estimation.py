import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from agent_core import JsonlSessionStore, LLMRequest, Message, Session, TokenUsage, ToolDefinition
from agent_core.context_manager import ContextManager
from agent_core.providers import OpenAIProvider
from tests.agent_fixtures import AGENT_CONFIG, make_agent


TOOL = ToolDefinition("probe", "Read a file.", {"type": "object", "properties": {"path": {"type": "string"}}})


class InputAnchorTest(unittest.TestCase):
    def setUp(self):
        self.provider = OpenAIProvider("gpt-5", base_url="https://one.example/v1")
        self.session = Session()
        self.context = self.context_for(self.provider, self.session)
        self.request = LLMRequest("Instructions", (Message("user", "hello"),), (TOOL,))

    def context_for(self, provider, session):
        return ContextManager(provider, session, "Instructions", "Summarize", AGENT_CONFIG)

    def anchor(self, actual=500):
        estimate = self.context.measure_input_tokens(self.request)
        usage = TokenUsage(actual, 900, actual + 900, 200, None)
        self.session.model_completed(1, usage=usage, has_tool_calls=True,
                                     input_token_anchor=estimate.anchor(usage))
        return estimate

    def test_input_only_anchor_and_signed_message_changes(self):
        initial = self.anchor()
        self.assertEqual(self.context.measure_input_tokens(self.request).tokens, 500)
        following = replace(self.request, messages=(*self.request.messages,
                            Message("assistant", "output", reasoning="reasoning"),
                            Message("user", "next input " * 100)))
        measured = self.context.measure_input_tokens(following)
        self.assertEqual(measured.tokens, 500 + measured.local_tokens - initial.local_tokens)
        shorter = replace(self.request, messages=())
        measured = self.context.measure_input_tokens(shorter)
        self.assertEqual(measured.tokens, max(0, 500 + measured.local_tokens - initial.local_tokens))

    def test_anchor_replays_and_request_envelope_changes_invalidate_it(self):
        self.anchor()
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path
            store = JsonlSessionStore(Path(directory))
            store.append_events(self.session.session_id, self.session.journal, workspace=Path(directory))
            restored = store.load(self.session.session_id, recover=False)
        context = self.context_for(OpenAIProvider("gpt-5", base_url="https://one.example/v1"), restored)
        self.assertEqual(context.measure_input_tokens(self.request).tokens, 500)
        for request in (replace(self.request, system_prompt="changed"), replace(self.request, tools=()),
                        replace(self.request, tools=(replace(TOOL, description="changed"),)),
                        replace(self.request, media_root=Path(directory))):
            measured = context.measure_input_tokens(request)
            self.assertEqual(measured.tokens, measured.local_tokens)
        for provider in (OpenAIProvider("gpt-5", base_url="https://two.example/v1"),
                         OpenAIProvider("gpt-5", base_url="https://one.example/v1", api="chat_completions"),
                         OpenAIProvider("gpt-5-mini", base_url="https://one.example/v1")):
            measured = self.context_for(provider, restored).measure_input_tokens(self.request)
            self.assertEqual(measured.tokens, measured.local_tokens)
        restored.set_archived_summary("Summary", 1)
        self.assertIsNone(restored.input_token_anchor)
        measured = context.measure_input_tokens(self.request)
        self.assertEqual(measured.tokens, measured.local_tokens)

    def test_missing_usage_clears_anchor_and_zero_input_is_valid(self):
        self.anchor(0)
        self.assertEqual(self.context.measure_input_tokens(self.request).tokens, 0)
        self.session.model_completed(2, usage=None, has_tool_calls=False)
        self.assertIsNone(self.session.input_token_anchor)

    def test_archive_measures_its_changed_request_with_the_matching_usage_offset(self):
        from agent_core import ContextCompressionConfig, ContextWindowExceededError
        from tests.agent_fixtures import MockProvider
        for local, exceeds in ((901, False), (951, True)):
            with self.subTest(local=local):
                session = Session(items=[Message("user", "old"), Message("assistant", "retained")])
                provider = MockProvider(["- [ephemeral] Test context."], input_tokens=[10, 4, 100, local])
                context = ContextManager(provider, session, "Instructions", "Summarize", replace(
                    AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=1)))
                request = context.build_request()
                estimate = context.measure_input_tokens(request)
                usage = TokenUsage(50, 1, 51, 0, None)
                session.model_completed(1, usage=usage, has_tool_calls=False,
                                        input_token_anchor=estimate.anchor(usage))
                anchor = session.input_token_anchor
                if exceeds:
                    with self.assertRaises(ContextWindowExceededError) as raised:
                        context.archive(request)
                    self.assertEqual(raised.exception.input_tokens, 901)
                    self.assertEqual(provider.requests, [])
                    self.assertEqual(session.input_token_anchor, anchor)
                else:
                    observed = []
                    context.archive(request, on_call=lambda _: observed.append(session.input_token_anchor))
                    self.assertEqual(observed, [anchor])
                    self.assertEqual(len(provider.requests), 1)
                    self.assertIsNone(session.input_token_anchor)
                self.assertNotEqual(provider.counted_requests[-1].messages, request.messages)
                self.assertEqual(provider.counted_requests[-1].messages[-1].content, "Summarize")

    def test_failed_call_keeps_last_successful_anchor_and_corrected_count_drives_limits(self):
        from agent_core import ProviderStreamError
        from tests.agent_fixtures import MockProvider

        class FailingProvider(MockProvider):
            def stream(self, request, on_text_delta, on_reasoning_delta=None):
                raise ProviderStreamError("incomplete", details={})

        provider = FailingProvider([])
        session = Session()
        context = self.context_for(provider, session)
        request = LLMRequest("Instructions", ())
        measurement = context.measure_input_tokens(request)
        session.model_completed(1, usage=None, has_tool_calls=False,
                                input_token_anchor=measurement.anchor(TokenUsage(899, 1, 900, 0, None)))
        previous = session.input_token_anchor
        self.assertEqual(context.measure_input_tokens(request).tokens, 899)
        self.assertTrue(context.limits.compression_threshold < 899)
        with self.assertRaises(ProviderStreamError):
            make_agent(provider, session, system_prompt="Instructions").run("hello")
        self.assertEqual(session.input_token_anchor, previous)

    def test_agent_records_anchor_without_an_extra_count(self):
        from tests.agent_fixtures import MockProvider
        from agent_core import LLMResponse
        usage = TokenUsage(100, 1, 101, 0, None)
        provider = MockProvider([LLMResponse("OK", usage=usage)])
        agent = make_agent(provider, self.session)
        agent.run("hello")
        self.assertEqual(self.session.input_token_anchor.input_tokens, 100)
        self.assertEqual(len(provider.counted_requests), 3)  # Two fixed-prompt counts and the input.


class DeepSeekNativeTest(unittest.TestCase):
    def test_explicit_responses_omits_encrypted_reasoning_include(self):
        from tests.test_openai_provider import OpenAIProviderTest, response_event
        fixture = OpenAIProviderTest()
        provider = OpenAIProvider("deepseek/deepseek-flash", api="responses", api_key="test-key")
        with fixture.transport([response_event()]) as requests:
            result = provider.stream(LLMRequest("Instructions", (Message("user", "hello"),)), lambda _: None)
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertNotIn("include", body)
        self.assertEqual(result.content, "OK")

    def test_metadata_and_counter_work_with_litellm_import_forbidden(self):
        code = '''
import sys, json
import importlib.metadata as metadata
distribution = metadata.distribution
def without_litellm(name):
    if name == 'litellm': raise metadata.PackageNotFoundError(name)
    return distribution(name)
metadata.distribution = without_litellm
class Block:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'litellm': raise AssertionError('LiteLLM imported')
sys.meta_path.insert(0, Block())
from agent_core import LLMRequest
from agent_core.providers import OpenAIProvider
from agent_runtime.config import ModelConfig
for model in ('deepseek/deepseek-flash', 'deepseek-flash', 'deepseek/deepseek-v4-pro'):
    p = ModelConfig(model, None, 'test').provider_class(model)
    assert p.max_context_tokens == 1000000
    assert p.max_output_tokens == 393216
    assert p.count_input_tokens(LLMRequest('hello', ())) > 0
    assert p.token_counting_identity
from agent_core.providers import OpenAIProvider
p = OpenAIProvider('gpt-5')
assert p.count_input_tokens(LLMRequest('hello', ())) > 0
assert p.token_counting_identity
assert 'litellm' not in sys.modules
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reasoning_count_depends_on_tools(self):
        provider = OpenAIProvider("deepseek/deepseek-flash")
        message = Message("assistant", "answer", reasoning="reasoning " * 100)
        plain = LLMRequest("Instructions", (message,))
        self.assertEqual(provider.count_input_tokens(plain),
                         provider.count_input_tokens(replace(plain, messages=(replace(message, reasoning=None),))))
        with_tools = replace(plain, tools=(TOOL,))
        self.assertGreater(provider.count_input_tokens(with_tools),
                           provider.count_input_tokens(replace(with_tools, messages=(replace(message, reasoning=None),))))

    def test_reasoning_count_ignores_display_summary_over_responses(self):
        provider = OpenAIProvider("deepseek/deepseek-flash", api="responses")
        message = Message("assistant", "answer", reasoning="reasoning " * 100)
        for tools in ((), (TOOL,)):
            with self.subTest(tools=bool(tools)):
                request = LLMRequest("Instructions", (message,), tools)
                self.assertEqual(
                    provider.count_input_tokens(request),
                    provider.count_input_tokens(
                        replace(request, messages=(replace(message, reasoning=None),))
                    ),
                )

    def test_stream_preserves_reasoning_tools_usage_and_deepseek_parameters(self):
        from tests.test_openai_provider import OpenAIProviderTest, chat_chunk
        fixture = OpenAIProviderTest()
        usage = {"prompt_tokens": 400, "completion_tokens": 12, "total_tokens": 412,
                 "prompt_tokens_details": {"cached_tokens": 200}}
        events = [chat_chunk({"reasoning_content": "think"}),
                  chat_chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                             "function": {"name": "probe", "arguments": "{}"}}]}),
                  chat_chunk(finish="tool_calls"), chat_chunk(usage=usage)]
        provider = OpenAIProvider("deepseek/deepseek-flash", reasoning_effort="none", api_key="test-key")
        with fixture.transport(events) as requests:
            response = provider.stream(LLMRequest("Instructions", (Message("user", "hello"),),
                                                  (TOOL,), max_generation_tokens=100), lambda _: None)
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["max_tokens"], 100)
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(response.reasoning, "think")
        self.assertEqual(response.tool_calls[0].name, "probe")
        self.assertEqual(response.usage.input_tokens, 400)
        self.assertEqual(response.usage.cache_read_tokens, 200)

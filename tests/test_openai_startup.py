"""Native model startup and local estimates must work without LiteLLM or network."""

import json
import gzip
import hashlib
import subprocess
import sys
import unittest
from importlib.resources import files
from unittest.mock import patch

from agent_core import LLMRequest, Message, ToolCall, ToolDefinition
from agent_core.providers import OpenAIProvider
from agent_core.providers.openai_models import model_info
from agent_core.providers.openai_tokens import _encoding


class OpenAIStartupTest(unittest.TestCase):
    def test_native_runtime_reaches_first_request_with_litellm_import_forbidden(self):
        result = subprocess.run(
            [sys.executable, "-m", "tests.openai_startup_probe", "--block-litellm"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["litellm_loaded"])
        self.assertFalse(report["pyautogui_loaded"])
        self.assertTrue(all(value > 0 for value in report["sample_estimates"]))

    def test_deepseek_runtime_reaches_request_with_litellm_import_forbidden(self):
        result = subprocess.run(
            [sys.executable, "-m", "tests.openai_startup_probe", "--block-litellm", "--deepseek"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["litellm_loaded"])

    def test_bundled_encodings_match_known_vectors_without_downloads(self):
        _encoding.cache_clear()
        self.addCleanup(_encoding.cache_clear)
        resources = files("agent_core.providers").joinpath("data", "tokenizers")
        for name, expected in (
            ("cl100k_base", "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"),
            ("o200k_base", "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"),
        ):
            vocabulary = gzip.decompress(resources.joinpath(name + ".tiktoken.gz").read_bytes())
            self.assertEqual(hashlib.sha256(vocabulary).hexdigest(), expected)
        with patch("socket.socket.connect", side_effect=AssertionError("network access")):
            self.assertEqual(_encoding("cl100k_base").encode("hello world"), [15339, 1917])
            self.assertEqual(_encoding("o200k_base").encode("hello world"), [24912, 2375])
            provider = OpenAIProvider("gpt-5.2")
            self.assertGreater(provider.count_input_tokens(LLMRequest("中文", ())), 0)
            request = LLMRequest("你好", ())
            self.assertGreater(provider.count_input_tokens(request), 0)

    def test_local_limits_and_vision_do_not_depend_on_endpoint_hostname(self):
        for model in ("gpt-6-astra", "openai/gpt-6-astra", "gpt-6.1-sol"):
            provider = OpenAIProvider(model, base_url="https://relay.example/v1")
            self.assertEqual(provider.max_context_tokens, 922000)
            self.assertEqual(provider.max_output_tokens, 128000)
            self.assertIn("image", provider.capabilities.input_modalities)
        self.assertEqual(model_info("gpt-4o-2024-05-13")["max_output_tokens"], 4096)
        self.assertEqual(model_info("gpt-4o")["max_output_tokens"], 16384)

    def test_unknown_alias_requires_context_and_remains_text_only(self):
        with self.assertRaisesRegex(ValueError, "configure 'max_context_tokens'"):
            OpenAIProvider("custom-alias")
        provider = OpenAIProvider("custom-alias", max_context_tokens=32000)
        self.assertEqual(provider.max_context_tokens, 32000)
        self.assertIsNone(provider.max_output_tokens)
        self.assertEqual(provider.capabilities.input_modalities, frozenset({"text"}))
        self.assertGreater(provider.count_input_tokens(LLMRequest("Hello", ())), 0)

    def test_nested_schema_and_tool_call_arguments_contribute_to_estimate(self):
        provider = OpenAIProvider("gpt-5")
        messages = (Message("user", "分析这个工作区。 <|endoftext|>"),)
        plain = LLMRequest("Instructions", messages)
        schema = {"type": "object", "properties": {
            "options": {"type": "object", "properties": {
                "mode": {"type": "string", "enum": ["search", "read"]},
            }},
        }}
        tools = (ToolDefinition("probe", "Inspect files", schema),)
        with_tools = LLMRequest("Instructions", messages, tools)
        self.assertGreater(provider.count_input_tokens(with_tools), provider.count_input_tokens(plain))
        call = Message("assistant", None, tool_calls=(ToolCall("call_1", "probe", {"query": "long value " * 100}),))
        self.assertGreater(
            provider.count_input_tokens(LLMRequest("Instructions", (*messages, call), tools)),
            provider.count_input_tokens(with_tools),
        )

    def test_namespaced_compatible_models_keep_their_existing_metadata_and_counter(self):
        with (
            patch("agent_core.providers.litellm_provider.get_model_info", return_value={
                "max_input_tokens": 100000, "supports_vision": True,
            }),
            patch("agent_core.providers.litellm_provider.token_counter", return_value=17),
        ):
            provider = OpenAIProvider("anthropic/claude", api="chat_completions")
            self.assertIn("image", provider.capabilities.input_modalities)
            self.assertEqual(provider.count_input_tokens(LLMRequest("Test", ())), 17)

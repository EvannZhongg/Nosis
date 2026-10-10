import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import AgentConfig, ToolConfig, Workspace
from agent_core.providers import LiteLLMProvider, OpenAIProvider
from agent_runtime.config import ModelConfig
from agent_runtime.plane_manager import ExecutionPlaneManager


class ProviderSelectionTest(unittest.TestCase):
    def select(self, model, url, api=None):
        manager = ExecutionPlaneManager.__new__(ExecutionPlaneManager)
        return manager._provider_for(
            ModelConfig(model, url, "test-key", 100000, reasoning_effort="high", api=api),
            Workspace(Path(__file__).parent), AgentConfig(
                max_same_tool_calls=5, output_reserve_tokens=100,
                tools=ToolConfig(enabled=()), workspace_instruction_files=(),
            ),
        )

    def test_openai_model_uses_responses_independent_of_hostname(self):
        for url in (None, "https://relay-one.example/v1", "http://localhost:8888/v1"):
            for model in ("gpt-6-astra", "openai/gpt-6-astra"):
                with self.subTest(url=url, model=model):
                    provider = self.select(model, url)
                    self.assertIsInstance(provider, OpenAIProvider)
                    self.assertEqual(provider._api, "responses")
                    self.assertEqual(provider._wire_model, "gpt-6-astra")
                    self.assertEqual(provider._base_url, url)

    def test_explicit_protocol_supports_arbitrary_model_ids_without_inference(self):
        with patch("litellm.get_llm_provider") as infer:
            for api in ("responses", "chat_completions"):
                for model in ("custom-alias", "anthropic/claude", "openai/gpt-6-astra"):
                    provider = self.select(model, "https://relay.example/v1", api)
                    self.assertIsInstance(provider, OpenAIProvider)
                    self.assertEqual(provider._api, api)
                    self.assertEqual(provider._wire_model, model)
            infer.assert_not_called()

    def test_unprefixed_model_does_not_require_a_catalog_entry_to_select_protocol(self):
        with patch("agent_core.providers.base.get_model_info", side_effect=ValueError("unknown model")):
            provider = self.select("custom-alias", "https://relay.example/v1")
        self.assertIsInstance(provider, OpenAIProvider)
        self.assertEqual(provider._api, "responses")
        self.assertEqual(provider._wire_model, "custom-alias")

    def test_other_provider_routes_still_use_existing_adapter(self):
        for model in ("anthropic/claude-sonnet-4-5", "gemini/gemini-2.5-flash", "deepseek/deepseek-flash"):
            with self.subTest(model=model):
                self.assertIsInstance(self.select(model, None), LiteLLMProvider)

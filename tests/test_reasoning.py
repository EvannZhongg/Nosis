import unittest

from agent_core.providers.reasoning import (
    normalize_semantic_effort,
    reasoning_arguments,
)


class ReasoningAdapterTest(unittest.TestCase):
    def test_normalizes_effort(self) -> None:
        self.assertEqual(normalize_semantic_effort(" MINIMUM "), "minimal")

    def test_rejects_unknown_effort(self) -> None:
        with self.assertRaises(ValueError):
            normalize_semantic_effort("balanced")

    def test_openai_effort_is_native(self) -> None:
        self.assertEqual(
            reasoning_arguments("openai/gpt-5", "high"),
            {"reasoning_effort": "high"},
        )

    def test_mistral_remaps_semantic_effort(self) -> None:
        self.assertEqual(
            reasoning_arguments("mistral/magistral", "low"),
            {"reasoning_effort": "none"},
        )
        self.assertEqual(
            reasoning_arguments("mistral/magistral", "medium"),
            {"reasoning_effort": "high"},
        )

    def test_openrouter_uses_gateway_reasoning_body(self) -> None:
        self.assertEqual(
            reasoning_arguments("openrouter/openai/gpt-5", "high"),
            {"extra_body": {"reasoning": {"effort": "high"}}},
        )

    def test_custom_parameters_are_preserved(self) -> None:
        self.assertEqual(
            reasoning_arguments(
                "custom/model",
                None,
                {"extra_body": {"enable_thinking": True}},
            ),
            {"extra_body": {"enable_thinking": True}},
        )

    def test_kimi_k3_only_uses_max(self) -> None:
        self.assertEqual(
            reasoning_arguments("moonshot/kimi-k3", "medium"),
            {"reasoning_effort": "max"},
        )


if __name__ == "__main__":
    unittest.main()

import unittest

from agent_core.providers.reasoning import (
    normalize_semantic_effort,
    reasoning_arguments,
    validate_reasoning_parameters,
)


class ReasoningAdapterTest(unittest.TestCase):
    def test_normalizes_effort(self) -> None:
        self.assertEqual(normalize_semantic_effort(" MINIMAL "), "minimal")

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

    def test_rejects_unknown_custom_parameter(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported field.*typo"):
            validate_reasoning_parameters({"typo": True})

    def test_custom_parameter_types_are_validated(self) -> None:
        for parameters in ({"thinking": True}, {"reasoning": "high"},
                           {"extra_body": []}, {"reasoning_effort": False},
                           {"thinking_level": ""}, {"extra_body": {"reasoning": "high"}}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                validate_reasoning_parameters(parameters)

    def test_mapping_does_not_mutate_custom_parameters(self) -> None:
        original = {"extra_body": {"reasoning": {"exclude": True}}}
        high = reasoning_arguments("openrouter/openai/gpt-5", "high", original)
        low = reasoning_arguments("openrouter/openai/gpt-5", "low", original)
        self.assertEqual(high["extra_body"]["reasoning"]["effort"], "high")
        self.assertEqual(low["extra_body"]["reasoning"]["effort"], "low")
        self.assertEqual(original, {"extra_body": {"reasoning": {"exclude": True}}})

    def test_xhigh_is_a_distinct_effort(self) -> None:
        self.assertEqual(reasoning_arguments("openai/gpt-6-astra", "xhigh"), {"reasoning_effort": "xhigh"})

    def test_kimi_k3_only_uses_max(self) -> None:
        self.assertEqual(
            reasoning_arguments("moonshot/kimi-k3", "medium"),
            {"reasoning_effort": "max"},
        )


if __name__ == "__main__":
    unittest.main()

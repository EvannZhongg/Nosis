import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import ProviderCapabilities
from agent_core.providers import LiteLLMProvider
from interfaces.bridge.config import (
    ModelConfig,
    configured_role_names,
    default_config_directory,
    initialize_default_configs,
    load_config,
    load_model_options,
    load_vision_config,
)


class ConfigTest(unittest.TestCase):
    def test_requires_main_agent_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "providers": {"first": {"model": "openai/first"}},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "main_agent"):
                load_config(path)

    def test_empty_subagent_provider_reuses_selected_main_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "first"},
                        "subagent": {"provider": ""},
                        "providers": {
                            "first": {"model": "openai/first"},
                            "second": {"model": "openai/second"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(load_config(path, provider="second").model, "openai/second")
            self.assertEqual(
                load_config(path, provider="second", role="researcher").model,
                "openai/second",
            )

    def test_lists_models_without_resolving_keys_and_loads_explicit_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            content = json.dumps({
                "main_agent": {"provider": "first"},
                "subagent": {"provider": ""},
                "providers": {
                    "first": {"model": "openai/first", "key": "${FIRST_KEY}"},
                    "second": {"model": "openai/second", "key": "${SECOND_KEY}"},
                },
            })
            path.write_text(content, encoding="utf-8")
            with patch.dict("os.environ", {"SECOND_KEY": "secret"}, clear=True):
                self.assertEqual(load_model_options(path), (
                    "first", {"first": "openai/first", "second": "openai/second"},
                ))
                selected = load_config(path, provider="second")
                self.assertEqual(selected.model, "openai/second")
                self.assertEqual(selected.key, "secret")
                with self.assertRaisesRegex(ValueError, "FIRST_KEY"):
                    load_config(path)
                with self.assertRaisesRegex(ValueError, "not configured"):
                    load_config(path, provider="unknown")
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_uses_home_for_default_directory(self) -> None:
        with patch("interfaces.bridge.config.Path.home") as home:
            home.return_value = Path("/home/test")

            self.assertEqual(
                default_config_directory(),
                Path("/home/test/.nosis"),
            )

    def test_initializes_packaged_default_configs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_directory = Path(directory) / "nosis"

            created = initialize_default_configs(config_directory)

            self.assertEqual(
                created,
                (
                    config_directory / "provider_config.json",
                    config_directory / "agent_config.json",
                ),
            )
            provider_config = json.loads(created[0].read_text(encoding="utf-8"))
            agent_config = json.loads(created[1].read_text(encoding="utf-8"))
            self.assertEqual(
                provider_config["main_agent"]["provider"],
                "openai",
            )
            self.assertEqual(provider_config["subagent"]["provider"], "")
            self.assertIn("openai", provider_config["providers"])
            self.assertEqual(
                provider_config["providers"]["qwen"],
                {
                    "model": "dashscope/qwen-plus",
                    "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "key": "${DASHSCOPE_API_KEY}",
                },
            )
            self.assertEqual(agent_config["max_same_tool_calls"], 5)
            self.assertEqual(
                agent_config["context"]["compression"]["keep_recent_units"],
                6,
            )
            self.assertTrue((config_directory / "skills").is_dir())
            self.assertNotIn("shell_timeout_seconds", agent_config)
            self.assertTrue(agent_config["main_agent"]["tools"]["read_file"])
            # Every shipped role carries an explicit enable switch.
            for name, role in agent_config["subagent_roles"].items():
                self.assertIsInstance(role["enabled"], bool, name)

    def test_initialization_does_not_overwrite_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_directory = Path(directory) / "nosis"
            config_directory.mkdir()
            provider_path = config_directory / "provider_config.json"
            provider_path.write_text("custom", encoding="utf-8")

            created = initialize_default_configs(config_directory)

            self.assertEqual(
                created,
                (config_directory / "agent_config.json",),
            )
            self.assertEqual(
                provider_path.read_text(encoding="utf-8"),
                "custom",
            )
            self.assertTrue((config_directory / "skills").is_dir())

    def test_loads_selected_provider_from_json_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "deepseek"},
                        "providers": {
                            "openai": {
                                "model": "openai/test-model",
                                "key": "${OPENAI_KEY}",
                            },
                            "deepseek": {
                                "model": "deepseek/test-model",
                                "url": "https://example.com",
                                "key": "${DEEPSEEK_KEY}",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {"DEEPSEEK_KEY": "secret"},
                clear=True,
            ):
                self.assertEqual(
                    load_config(path),
                    ModelConfig(
                        model="deepseek/test-model",
                        url="https://example.com",
                        key="secret",
                        max_context_tokens=None,
                    ),
                )

    def test_strips_whitespace_from_provider_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": " test "},
                        "providers": {
                            "test": {
                                "model": " openai/test-model ",
                                "url": " https://example.com/v1 ",
                                "key": " secret ",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_config(path),
                ModelConfig(
                    model="openai/test-model",
                    url="https://example.com/v1",
                    key="secret",
                    max_context_tokens=None,
                ),
            )

    def test_allows_inline_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "local"},
                        "providers": {
                            "local": {
                                "model": "openai/local-model",
                                "url": "http://localhost:8000/v1",
                                "key": "local-key",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_config(path),
                ModelConfig(
                    model="openai/local-model",
                    url="http://localhost:8000/v1",
                    key="local-key",
                    max_context_tokens=None,
                ),
            )

    def test_loads_optional_provider_context_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "test"},
                        "providers": {
                            "test": {
                                "model": "openai/test-model",
                                "max_context_tokens": 128000,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_config(path),
                ModelConfig(
                    model="openai/test-model",
                    url=None,
                    key=None,
                    max_context_tokens=128000,
                ),
            )

    def test_rejects_boolean_provider_context_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "test"},
                        "providers": {
                            "test": {
                                "model": "openai/test-model",
                                "max_context_tokens": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_config(path)

    def test_rejects_unknown_selected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "missing"},
                        "providers": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_config(path)

    def test_rejects_missing_key_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "test"},
                        "providers": {
                            "test": {
                                "model": "test/model",
                                "key": "${TEST_KEY}",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ValueError):
                    load_config(path)


VISION_MODEL = "vision/model"
TEXT_MODEL = "text/model"


def _capabilities(model: str, base_url: str | None = None):
    """Report only VISION_MODEL as image-capable, without touching LiteLLM."""
    modalities = {"text"}
    if model == VISION_MODEL:
        modalities.add("image")
    return ProviderCapabilities(frozenset(modalities))


# capabilities_for_model is a classmethod, so the patch must absorb `cls`.
_CAPABILITIES_PATCH = classmethod(
    lambda cls, model, base_url=None: _capabilities(model, base_url)
)


class ProviderResolutionTest(unittest.TestCase):
    """`provider` and `vision_provider` both walk role → subagent → main."""

    def write(self, directory: str, **blocks: object) -> Path:
        path = Path(directory) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "main_agent": {"provider": "main"},
                    "providers": {
                        "main": {"model": "openai/main"},
                        "shared": {"model": "openai/shared"},
                        "special": {"model": "openai/special"},
                        "seeing": {"model": VISION_MODEL},
                        "blind": {"model": TEXT_MODEL},
                        **blocks.pop("providers", {}),
                    },
                    **blocks,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_role_override_wins_over_subagent_and_main(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                subagent={"provider": "shared"},
                subagent_roles={"coder": {"provider": "special"}},
            )

            self.assertEqual(
                load_config(path, role="coder").model, "openai/special"
            )
            self.assertEqual(
                load_config(path, role="researcher").model, "openai/shared"
            )
            self.assertEqual(load_config(path).model, "openai/main")

    def test_empty_role_override_falls_through_the_chain(self) -> None:
        """"" means inherit, so the chain keeps walking past it."""
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                subagent={"provider": "shared"},
                subagent_roles={"coder": {"provider": ""}},
            )

            self.assertEqual(
                load_config(path, role="coder").model, "openai/shared"
            )

    def test_a_role_overriding_only_vision_still_inherits_its_model(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                subagent={"provider": "shared"},
                subagent_roles={"coder": {"vision_provider": "seeing"}},
            )

            with patch.object(
                LiteLLMProvider, "capabilities_for_model", _CAPABILITIES_PATCH
            ):
                self.assertEqual(
                    load_config(path, role="coder").model, "openai/shared"
                )
                vision = load_vision_config(path, role="coder")

            self.assertIsNotNone(vision)
            self.assertEqual(vision.model, VISION_MODEL)

    def test_a_role_overriding_only_its_model_still_inherits_vision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                main_agent={"provider": "main", "vision_provider": "seeing"},
                subagent_roles={"coder": {"provider": "special"}},
            )

            with patch.object(
                LiteLLMProvider, "capabilities_for_model", _CAPABILITIES_PATCH
            ):
                self.assertEqual(
                    load_config(path, role="coder").model, "openai/special"
                )
                vision = load_vision_config(path, role="coder")

            self.assertEqual(vision.model, VISION_MODEL)

    def test_no_vision_provider_resolves_to_none_without_scanning(self) -> None:
        """The capability scan is gone: an unset vision provider is None.

        'seeing' is configured and image-capable, so the deleted fallback
        would have selected it. Nothing may route images implicitly.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory)

            with patch.object(
                LiteLLMProvider, "capabilities_for_model", _CAPABILITIES_PATCH
            ):
                self.assertIsNone(load_vision_config(path))
                self.assertIsNone(load_vision_config(path, role="coder"))

    def test_rejects_a_vision_provider_that_cannot_see(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                main_agent={"provider": "main", "vision_provider": "blind"},
            )

            with patch.object(
                LiteLLMProvider, "capabilities_for_model", _CAPABILITIES_PATCH
            ):
                with self.assertRaisesRegex(
                    ValueError, "does not support image input"
                ):
                    load_vision_config(path)

    def test_rejects_a_blank_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                subagent_roles={"coder": {"provider": "   "}},
            )

            with self.assertRaisesRegex(
                ValueError, "subagent_roles.coder.provider"
            ):
                load_config(path, role="coder")

    def test_rejects_a_non_object_role_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, subagent_roles={"coder": "anthropic"})

            with self.assertRaisesRegex(
                ValueError, "subagent_roles.coder.*object"
            ):
                load_config(path)

    def test_reports_the_roles_that_override_a_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                subagent_roles={
                    "coder": {"provider": "special"},
                    "writer": {"vision_provider": "seeing"},
                },
            )

            self.assertEqual(
                configured_role_names(path), frozenset({"coder", "writer"})
            )

    def test_reports_no_roles_when_the_block_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                configured_role_names(self.write(directory)), frozenset()
            )


if __name__ == "__main__":
    unittest.main()

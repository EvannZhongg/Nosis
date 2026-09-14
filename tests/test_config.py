import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interfaces.bridge.config import (
    ModelConfig,
    default_config_directory,
    initialize_default_configs,
    load_config,
    load_model_options,
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
                load_config(path, provider="second", subagent=True).model,
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
            self.assertEqual(agent_config["max_same_tool_calls"], 5)
            self.assertEqual(agent_config["shell_timeout_seconds"], 60)
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


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.provider_fixtures import capabilities_stub, TEXT_MODEL, VISION_MODEL

from agent_core import DirectorySkillSource, SkillLoader, load_agent_config
from agent_core.providers import ModelProvider
from agent_runtime.config import (
    DEFAULT_CONFIG_FILENAMES,
    ModelConfig,
    load_tool_settings,
    configured_role_names,
    default_config_directory,
    initialize_config_directory,
    load_config,
    load_fast_config,
    load_image_generation_config,
    load_model_options,
    load_prompt_templates,
    load_scratch_workspace_root,
    load_vision_config,
    memory_store,
)
from agent_runtime.plugins import PluginManager


class ConfigTest(unittest.TestCase):
    def test_existing_agent_config_can_omit_session_title_generation_switch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_directory = Path(directory)
            initialize_config_directory(config_directory)
            path = config_directory / "agent_config.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            del document["generate_session_title"]
            path.write_text(json.dumps(document), encoding="utf-8")
            preserved = path.read_bytes()

            initialize_config_directory(config_directory)

            self.assertEqual(path.read_bytes(), preserved)
            self.assertTrue(load_agent_config(path).generate_session_title)

    def test_fast_model_selection_and_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            document = {
                "main_agent": {"provider": "first"},
                "fast_model": {"provider": ""},
                "providers": {
                    "first": {"model": "openai/first"},
                    "second": {"model": "openai/second"},
                },
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertIsNone(load_fast_config(path))
            document["fast_model"]["provider"] = None
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertIsNone(load_fast_config(path))
            document["fast_model"]["provider"] = "second"
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(load_fast_config(path).model, "openai/second")
            document["fast_model"]["provider"] = "missing"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not configured"):
                load_fast_config(path)

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
        with patch("agent_runtime.config.Path.home") as home:
            home.return_value = Path("/home/test")

            self.assertEqual(
                default_config_directory(),
                Path("/home/test/.nosis"),
            )

    def test_memory_paths_are_derived_in_one_place(self) -> None:
        store = memory_store(Path("/config"))

        self.assertEqual(store.global_path, Path("/config/MEMORY.md"))
        self.assertEqual(
            store.sessions_directory,
            Path("/config/sessions"),
        )

    def test_loads_and_expands_scratch_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps({"scratch_workspace_root": "~/scratch"}),
                encoding="utf-8",
            )

            self.assertEqual(
                load_scratch_workspace_root(path),
                Path("~/scratch").expanduser().resolve(),
            )

    def test_missing_scratch_workspace_root_has_an_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "missing required config field 'scratch_workspace_root'.*add an absolute path",
            ):
                load_scratch_workspace_root(path)

    def test_loads_image_generation_from_a_configured_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "main"},
                        "image_generation": {
                            "provider": "images",
                            "model": "openrouter/example/image",
                            "default_aspect_ratio": "16:9",
                            "default_image_size": "2K",
                            "extra": {"enabled": False},
                        },
                        "providers": {
                            "main": {"model": "openai/main"},
                            "images": {
                                "model": "openrouter/unused-chat-model",
                                "url": "https://example.test/v1",
                                "key": "secret",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = load_image_generation_config(path)

        self.assertIsNotNone(config)
        self.assertEqual(config.provider, "images")
        self.assertEqual(config.model, "openrouter/example/image")
        self.assertEqual(config.url, "https://example.test/v1")
        self.assertEqual(config.key, "secret")
        self.assertEqual(config.default_aspect_ratio, "16:9")
        self.assertEqual(config.default_image_size, "2K")

    def test_initializes_packaged_default_configs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_directory = Path(directory) / "nosis"

            created = initialize_config_directory(config_directory)

            self.assertEqual(
                created,
                (
                    config_directory / "provider_config.json",
                    config_directory / "agent_config.json",
                    config_directory / "tool_config.json",
                    config_directory / "im_channels.json",
                    config_directory / "AGENTS.md",
                    config_directory / "MEMORY.md",
                    config_directory / "prompts" / "Soul.md",
                    config_directory / "prompts" / "SubAgent.md",
                    config_directory / "prompts" / "Consolidator.md",
                    config_directory / "prompts" / "GlobalMemory.md",
                    config_directory / "prompts" / "WorkspaceMemory.md",
                    config_directory / "prompts" / "SessionTitle.md",
                    config_directory / "skills" / "plugin-creator",
                    config_directory / "skills" / "skill-creator",
                    config_directory / "plugins" / "playwright",
                ),
            )
            provider_config = json.loads(created[0].read_text(encoding="utf-8"))
            agent_config = json.loads(created[1].read_text(encoding="utf-8"))
            self.assertEqual(
                provider_config["main_agent"]["provider"],
                "openai",
            )
            self.assertEqual(provider_config["subagent"]["provider"], "")
            self.assertEqual(provider_config["fast_model"]["provider"], "")
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
                agent_config["provider"],
                {"request_timeout_seconds": 300, "max_retries": 2},
            )
            self.assertEqual(
                agent_config["scratch_workspace_root"],
                "~/.nosis/workspaces/scratch",
            )
            # The built-in MCP plugin only loads while MCP is switched on.
            self.assertTrue(agent_config["mcp"]["enabled"])
            self.assertEqual(
                agent_config["workspace_instruction_files"],
                ["CLAUDE.md", "AGENTS.md"],
            )
            self.assertEqual(
                agent_config["skills"],
                {
                    "global_paths": ["~/.nosis/skills"],
                    "workspace_paths": [
                        "skills",
                        ".agents/skills",
                        ".nosis/skills",
                    ],
                },
            )
            self.assertEqual(
                (config_directory / "AGENTS.md").read_text(encoding="utf-8"),
                "",
            )
            self.assertTrue((config_directory / "MEMORY.md").is_file())
            self.assertFalse((config_directory / "workspaces").exists())
            self.assertEqual(
                agent_config["context"]["compression"]["keep_recent_units"],
                8,
            )
            self.assertEqual(
                agent_config["context"]["compression"]["user_anchor_token_budget"],
                2000,
            )
            self.assertTrue((config_directory / "skills").is_dir())
            self.assertTrue((config_directory / "plugins").is_dir())
            prompts = load_prompt_templates(config_directory / "prompts")
            self.assertIn("I am Nosis", prompts.system)
            self.assertIn("{{role}}", prompts.subagent)
            self.assertIn("compact replacement checkpoint", prompts.consolidator)
            self.assertIn("global long-term memory", prompts.global_memory)
            self.assertIn("currently bound workspace", prompts.workspace_memory)
            skill_directory = config_directory / "skills" / "skill-creator"
            self.assertTrue((skill_directory / "SKILL.md").is_file())
            self.assertTrue((skill_directory / "license.txt").is_file())
            self.assertTrue((skill_directory / "scripts" / "init_skill.py").is_file())
            self.assertTrue((skill_directory / "scripts" / "quick_validate.py").is_file())
            self.assertEqual(
                SkillLoader().load(
                    (DirectorySkillSource(config_directory / "skills"),)
                ).names,
                ("plugin-creator", "skill-creator"),
            )
            plugins = PluginManager.discover(config_directory / "plugins")
            self.assertEqual(plugins.warnings, ())
            self.assertEqual(
                [plugin.name for plugin in plugins.plugins],
                ["playwright"],
            )
            servers, warnings = plugins.load_mcp_servers()
            self.assertEqual(warnings, ())
            self.assertEqual(
                [server.identifier for server in servers],
                ["playwright:browser"],
            )
            self.assertIn("--output-dir", servers[0].args)
            self.assertTrue(agent_config["main_agent"]["tools"]["read_file"])
            self.assertTrue(agent_config["main_agent"]["tools"]["job_control"])
            # Every shipped role carries an explicit enable switch.
            for name, role in agent_config["subagent_roles"].items():
                self.assertIsInstance(role["enabled"], bool, name)

    def test_initialization_does_not_overwrite_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_directory = Path(directory) / "nosis"
            config_directory.mkdir()
            provider_path = config_directory / "provider_config.json"
            provider_path.write_text("custom", encoding="utf-8")

            existing_skill = config_directory / "skills" / "skill-creator"
            existing_skill.mkdir(parents=True)
            skill_path = existing_skill / "SKILL.md"
            skill_path.write_text("custom skill", encoding="utf-8")
            existing_plugin = config_directory / "plugins" / "playwright"
            existing_plugin.mkdir(parents=True)
            plugin_path = existing_plugin / "plugin.json"
            plugin_path.write_text("custom plugin", encoding="utf-8")
            prompts_directory = config_directory / "prompts"
            prompts_directory.mkdir()
            soul_path = prompts_directory / "Soul.md"
            soul_path.write_text("custom prompt", encoding="utf-8")
            instructions_path = config_directory / "AGENTS.md"
            instructions_path.write_text("custom instructions", encoding="utf-8")

            created = initialize_config_directory(config_directory)

            self.assertEqual(
                created,
                (
                    config_directory / "agent_config.json",
                    config_directory / "tool_config.json",
                    config_directory / "im_channels.json",
                    config_directory / "MEMORY.md",
                    config_directory / "prompts" / "SubAgent.md",
                    config_directory / "prompts" / "Consolidator.md",
                    config_directory / "prompts" / "GlobalMemory.md",
                    config_directory / "prompts" / "WorkspaceMemory.md",
                    config_directory / "prompts" / "SessionTitle.md",
                    config_directory / "skills" / "plugin-creator",
                ),
            )
            self.assertEqual(
                provider_path.read_text(encoding="utf-8"),
                "custom",
            )
            self.assertTrue((config_directory / "skills").is_dir())
            self.assertEqual(skill_path.read_text(encoding="utf-8"), "custom skill")
            self.assertEqual(
                plugin_path.read_text(encoding="utf-8"),
                "custom plugin",
            )
            self.assertFalse((existing_plugin / ".mcp.json").exists())
            self.assertEqual(soul_path.read_text(encoding="utf-8"), "custom prompt")
            self.assertEqual(
                instructions_path.read_text(encoding="utf-8"),
                "custom instructions",
            )
            self.assertFalse((existing_skill / "scripts").exists())

    def test_initializes_only_first_level_skill_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            defaults = root / "defaults"
            defaults.mkdir()
            for filename in DEFAULT_CONFIG_FILENAMES:
                (defaults / filename).write_text("{}", encoding="utf-8")
            prompts = defaults / "prompts_template"
            prompts.mkdir()
            for filename in (
                "Soul.md", "SubAgent.md", "Consolidator.md",
                "GlobalMemory.md", "WorkspaceMemory.md", "SessionTitle.md",
            ):
                (prompts / filename).write_text(filename, encoding="utf-8")
            (defaults / "plugins").mkdir()

            direct = defaults / "skills" / "direct"
            direct.mkdir(parents=True)
            (direct / "SKILL.md").write_text("direct", encoding="utf-8")
            nested = defaults / "skills" / "group" / "nested"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text("nested", encoding="utf-8")
            without_entrypoint = defaults / "skills" / "not-a-skill"
            without_entrypoint.mkdir()
            (without_entrypoint / "resource.txt").write_text(
                "resource", encoding="utf-8"
            )

            config_directory = root / "nosis"
            with patch(
                "agent_runtime.config.files",
                return_value=defaults,
            ):
                initialize_config_directory(config_directory)

            self.assertTrue(
                (config_directory / "skills" / "direct" / "SKILL.md").is_file()
            )
            self.assertFalse((config_directory / "skills" / "group").exists())
            self.assertFalse(
                (config_directory / "skills" / "not-a-skill").exists()
            )

    def test_installs_only_plugin_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            defaults = root / "defaults"
            defaults.mkdir()
            for filename in DEFAULT_CONFIG_FILENAMES:
                (defaults / filename).write_text("{}", encoding="utf-8")
            prompts = defaults / "prompts_template"
            prompts.mkdir()
            for filename in (
                "Soul.md", "SubAgent.md", "Consolidator.md",
                "GlobalMemory.md", "WorkspaceMemory.md", "SessionTitle.md",
            ):
                (prompts / filename).write_text(filename, encoding="utf-8")
            (defaults / "skills").mkdir()

            packaged = defaults / "plugins" / "demo"
            packaged.mkdir(parents=True)
            (packaged / "plugin.json").write_text("{}", encoding="utf-8")
            (packaged / ".mcp.json").write_text("{}", encoding="utf-8")
            nested = defaults / "plugins" / "group" / "nested"
            nested.mkdir(parents=True)
            (nested / "plugin.json").write_text("{}", encoding="utf-8")
            without_manifest = defaults / "plugins" / "not-a-plugin"
            without_manifest.mkdir()
            (without_manifest / "resource.txt").write_text("x", encoding="utf-8")

            config_directory = root / "nosis"
            with patch(
                "agent_runtime.config.files",
                return_value=defaults,
            ):
                initialize_config_directory(config_directory)

            installed = config_directory / "plugins" / "demo"
            self.assertTrue((installed / "plugin.json").is_file())
            self.assertTrue((installed / ".mcp.json").is_file())
            self.assertFalse((config_directory / "plugins" / "group").exists())
            self.assertFalse(
                (config_directory / "plugins" / "not-a-plugin").exists()
            )

    def test_installs_a_skill_without_caches_or_finder_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            defaults = root / "defaults"
            defaults.mkdir()
            for filename in DEFAULT_CONFIG_FILENAMES:
                (defaults / filename).write_text("{}", encoding="utf-8")
            prompts = defaults / "prompts_template"
            prompts.mkdir()
            for filename in (
                "Soul.md", "SubAgent.md", "Consolidator.md",
                "GlobalMemory.md", "WorkspaceMemory.md", "SessionTitle.md",
            ):
                (prompts / filename).write_text(filename, encoding="utf-8")
            (defaults / "plugins").mkdir()

            packaged = defaults / "skills" / "demo"
            scripts = packaged / "scripts"
            cache = scripts / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "helper.cpython-312.pyc").write_bytes(b"cache")
            (scripts / "helper.py").write_text("print()", encoding="utf-8")
            (packaged / "SKILL.md").write_text("demo", encoding="utf-8")
            (packaged / ".DS_Store").write_bytes(b"finder")

            config_directory = root / "nosis"
            with patch(
                "agent_runtime.config.files",
                return_value=defaults,
            ):
                initialize_config_directory(config_directory)

            installed = config_directory / "skills" / "demo"
            self.assertTrue((installed / "scripts" / "helper.py").is_file())
            self.assertFalse((installed / "scripts" / "__pycache__").exists())
            self.assertFalse((installed / ".DS_Store").exists())

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
                                "key": "${DEEPSEEK_API_KEY}",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {"DEEPSEEK_API_KEY": "secret"},
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

    def test_loads_reasoning_policy_from_selected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "main_agent": {"provider": "gateway"},
                        "providers": {
                            "gateway": {
                                "model": "gpt-6-astra",
                                "reasoning_effort": " MINIMAL ",
                                "reasoning_parameters": {
                                    "extra_body": {"enable_thinking": True},
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_config(path),
                ModelConfig(
                    model="gpt-6-astra",
                    url=None,
                    key=None,
                    max_context_tokens=None,
                    reasoning_effort="minimal",
                    reasoning_parameters={
                        "extra_body": {"enable_thinking": True},
                    },
                ),
            )

    def test_rejects_invalid_provider_reasoning_policy(self) -> None:
        for field, value in (
            ("reasoning_effort", "balanced"),
            ("reasoning_parameters", []),
            ("reasoning_parameters", {"typo": True}),
            ("reasoning_parameters", {"thinking": True}),
            ("api", "automatic"),
            ("api", []),
        ):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(
                    json.dumps(
                        {
                            "main_agent": {"provider": "test"},
                            "providers": {
                                "test": {
                                    "model": "openai/test-model",
                                    field: value,
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                with self.subTest(field=field), self.assertRaises(ValueError):
                    load_config(path)

    def test_loads_explicit_relay_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "main_agent": {"provider": "relay"},
                "providers": {"relay": {"model": "custom-model", "api": "chat_completions"}},
            }), encoding="utf-8")
            self.assertEqual(load_config(path).api, "chat_completions")

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
                ModelProvider, "capabilities_for_model", capabilities_stub(VISION_MODEL)
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
                ModelProvider, "capabilities_for_model", capabilities_stub(VISION_MODEL)
            ):
                self.assertEqual(
                    load_config(path, role="coder").model, "openai/special"
                )
                vision = load_vision_config(path, role="coder")

            self.assertEqual(vision.model, VISION_MODEL)

    def test_no_vision_provider_resolves_to_none(self) -> None:
        """Only an explicit vision_provider may route images elsewhere."""
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory)

            with patch.object(
                ModelProvider, "capabilities_for_model", capabilities_stub(VISION_MODEL)
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
                ModelProvider, "capabilities_for_model", capabilities_stub(VISION_MODEL)
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


class ToolSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        initialize_config_directory(self.root)
        self.path = self.root / "tool_config.json"
        self.document = json.loads(self.path.read_text(encoding="utf-8"))

    def load(self):
        self.path.write_text(json.dumps(self.document), encoding="utf-8")
        return load_tool_settings(self.path).web_search

    def test_installs_default_and_preserves_custom_configuration(self) -> None:
        self.assertEqual(load_tool_settings(self.path).web_search.provider, "duckduckgo")
        self.assertEqual(load_tool_settings(self.path).web_fetch.provider, "direct")
        self.document["web_search"]["provider"] = "deepseek"
        self.load()
        before = self.path.read_bytes()
        self.assertEqual(initialize_config_directory(self.root), ())
        self.assertEqual(self.path.read_bytes(), before)

    def test_resolves_only_the_selected_provider_key(self) -> None:
        for provider in ("exa", "deepseek"):
            with self.subTest(provider=provider), patch.dict(os.environ, {"SEARCH_KEY": "secret"}):
                self.document["web_search"]["provider"] = provider
                self.document["web_search"]["providers"][provider]["key"] = "${SEARCH_KEY}"
                config = self.load()
                self.assertEqual(config.provider, provider)
                self.assertEqual(config.api_key, "secret")
                self.assertNotIn("secret", repr(config))

    def test_missing_or_empty_keys_allow_runtime_fallback(self) -> None:
        with patch.dict(os.environ):
            os.environ.pop("NOSIS_TEST_MISSING_KEY", None)
            for key in (None, "", "  ", "${NOSIS_TEST_MISSING_KEY}"):
                with self.subTest(key=key):
                    self.document["web_search"]["provider"] = "exa"
                    self.document["web_search"]["providers"]["exa"]["key"] = key
                    self.assertIsNone(self.load().api_key)

    def test_accepts_literal_key_and_does_not_use_implicit_environment_key(self) -> None:
        self.document["web_search"]["provider"] = "exa"
        self.document["web_search"]["providers"]["exa"]["key"] = " literal-secret "
        self.assertEqual(self.load().api_key, "literal-secret")
        self.document["web_search"]["providers"] = {}
        with patch.dict(os.environ, {"EXA_API_KEY": "implicit-secret"}):
            self.assertIsNone(self.load().api_key)

    def test_rejects_invalid_tool_settings(self) -> None:
        for document in (
            [], {}, {"other_tool": {}}, {"web_search": None},
            {"web_search": {"provider": "unknown"}},
            {"web_search": {"provider": ["exa"]}},
            {"web_search": {"provider": "exa", "providers": []}},
            {"web_search": {"provider": "exa", "providers": {"unknown": {}}}},
            {"web_search": {"provider": "exa", "providers": {"exa": []}}},
            {"web_search": {"provider": "exa", "providers": {"exa": {"key": True}}}},
            {"web_search": {"provider": "exa", "providers": {"exa": {"key": "${}"}}}},
        ):
            with self.subTest(document=document), self.assertRaises(ValueError):
                self.document = (
                    {"web_fetch": {"provider": "direct"}, **document}
                    if isinstance(document, dict) else document
                )
                self.load()

    def test_ignores_unknown_fields_in_tool_settings(self) -> None:
        self.document["web_search"] = {
            "provider": "exa", "providers": {"exa": {"key": "search-secret"}},
        }
        self.document["web_fetch"] = {
            "provider": "deepseek", "providers": {"deepseek": {"key": "fetch-secret"}},
        }
        self.load()
        expected = load_tool_settings(self.path)
        self.document["extra"] = {"enabled": False}
        for tool, provider in (("web_search", "exa"), ("web_fetch", "deepseek")):
            self.document[tool]["extra"] = True
            self.document[tool]["providers"][provider]["extra"] = {"key": "unused"}

        self.load()

        self.assertEqual(load_tool_settings(self.path), expected)

    def test_requires_configuration_file(self) -> None:
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            load_tool_settings(self.path)

    def test_fetch_key_configuration_is_independent_of_search(self) -> None:
        self.document["web_search"]["provider"] = "exa"
        self.document["web_search"]["providers"]["exa"]["key"] = "search-secret"
        self.document["web_fetch"]["provider"] = "deepseek"
        for key, expected in (
            ("${FETCH_KEY}", "fetch-secret"), ("literal", "literal"),
            ("${MISSING_FETCH_KEY}", None), ("", None), ("  ", None), (None, None),
        ):
            with self.subTest(key=key), patch.dict(os.environ, {"FETCH_KEY": "fetch-secret"}):
                os.environ.pop("MISSING_FETCH_KEY", None)
                self.document["web_fetch"]["providers"]["deepseek"]["key"] = key
                self.load()
                settings = load_tool_settings(self.path)
                self.assertEqual(settings.web_fetch.provider, "deepseek")
                self.assertEqual(settings.web_fetch.api_key, expected)
                self.assertEqual(settings.web_search.api_key, "search-secret")
                self.assertNotIn("fetch-secret", repr(settings))

    def test_rejects_invalid_fetch_settings(self) -> None:
        for value in (
            None, [], {}, {"provider": "exa"}, {"provider": ["deepseek"]},
            {"provider": "deepseek", "providers": []},
            {"provider": "deepseek", "providers": {"exa": {}}},
            {"provider": "deepseek", "providers": {"deepseek": {"key": True}}},
            {"provider": "deepseek", "providers": {"deepseek": {"key": "${}"}}},
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "web_fetch"):
                self.document["web_fetch"] = value
                self.load()

    def test_requires_both_tool_sections(self) -> None:
        for missing in ("web_search", "web_fetch"):
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, missing):
                document = dict(self.document)
                del document[missing]
                self.path.write_text(json.dumps(document), encoding="utf-8")
                load_tool_settings(self.path)


if __name__ == "__main__":
    unittest.main()

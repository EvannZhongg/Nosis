import json
import tempfile
import unittest
from pathlib import Path

from agent_core import AgentConfig, MemoryConfig, ProviderRequestConfig, ToolConfig, load_agent_config


ENABLED_TOOLS = {
    "read_file": True,
    "edit_file": True,
    "search_files": True,
    "list_directory": True,
    "shell": True,
    "web_search": False,
}


class AgentConfigTest(unittest.TestCase):
    def load_json(self, payload: dict) -> AgentConfig:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_agent_config(path)

    def load(self, fields: dict) -> AgentConfig:
        return self.load_json(
            {
                "max_same_tool_calls": 5,
                "output_reserve_tokens": 100,
                "provider": {
                    "request_timeout_seconds": 300,
                    "max_retries": 2,
                },
                "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
                **fields,
            }
        )

    def test_loads_agent_behavior_config(self) -> None:
        self.assertEqual(
            self.load(
                {
                    "main_agent": {
                        "tools": {
                            **ENABLED_TOOLS,
                            "edit_file": False,
                            "shell": False,
                        }
                    }
                }
            ),
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(
                    enabled=("read_file", "search_files", "list_directory")
                ),
                workspace_instruction_files=("CLAUDE.md", "AGENTS.md"),
            )
        )

    def test_loads_workspace_instruction_files_in_priority_order(self) -> None:
        config = self.load(
            {
                "workspace_instruction_files": [
                    "PROJECT.md",
                    "TEAM.md",
                ],
                "main_agent": {"tools": ENABLED_TOOLS},
            }
        )

        self.assertEqual(
            config.workspace_instruction_files,
            ("PROJECT.md", "TEAM.md"),
        )

    def test_requires_workspace_instruction_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace_instruction_files.*array"):
            self.load_json(
                {
                    "max_same_tool_calls": 5,
                    "output_reserve_tokens": 100,
                    "provider": {
                        "request_timeout_seconds": 300,
                        "max_retries": 2,
                    },
                    "main_agent": {"tools": ENABLED_TOOLS},
                }
            )

    def test_rejects_non_root_workspace_instruction_paths(self) -> None:
        for value in ("../AGENTS.md", "docs/RULES.md", "/RULES.md"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "workspace root filenames",
                ):
                    self.load(
                        {
                            "workspace_instruction_files": [value],
                            "main_agent": {"tools": ENABLED_TOOLS},
                        }
                    )

    def test_rejects_duplicate_workspace_instruction_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate filenames"):
            self.load(
                {
                    "workspace_instruction_files": ["RULES.md", "RULES.md"],
                    "main_agent": {"tools": ENABLED_TOOLS},
                }
            )

    def test_rejects_non_positive_limit(self) -> None:
        with self.assertRaises(ValueError):
            self.load({"max_same_tool_calls": 0, "main_agent": {"tools": ENABLED_TOOLS}})

    def test_rejects_boolean_limit(self) -> None:
        with self.assertRaises(ValueError):
            self.load({"max_same_tool_calls": True, "main_agent": {"tools": ENABLED_TOOLS}})

    def test_rejects_non_positive_output_reserve(self) -> None:
        with self.assertRaises(ValueError):
            self.load(
                {"output_reserve_tokens": 0, "main_agent": {"tools": ENABLED_TOOLS}}
            )

    def test_loads_optional_generation_limit(self) -> None:
        config = self.load(
            {
                "max_generation_tokens": 50,
                "main_agent": {"tools": ENABLED_TOOLS},
            }
        )

        self.assertEqual(config.max_generation_tokens, 50)

    def test_loads_provider_request_policy(self) -> None:
        config = self.load(
            {
                "provider": {
                    "request_timeout_seconds": 45,
                    "max_retries": 4,
                },
                "main_agent": {"tools": ENABLED_TOOLS},
            }
        )

        self.assertEqual(config.provider, ProviderRequestConfig(45, 4))

    def test_defaults_provider_request_policy(self) -> None:
        cases = (
            (None, ProviderRequestConfig(300, 2)),
            ({}, ProviderRequestConfig(300, 2)),
            ({"request_timeout_seconds": 45}, ProviderRequestConfig(45, 2)),
            ({"max_retries": 4}, ProviderRequestConfig(300, 4)),
        )
        for provider, expected in cases:
            with self.subTest(provider=provider):
                payload = {
                    "max_same_tool_calls": 5,
                    "output_reserve_tokens": 100,
                    "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
                    "main_agent": {"tools": ENABLED_TOOLS},
                }
                if provider is not None:
                    payload["provider"] = provider
                self.assertEqual(self.load_json(payload).provider, expected)

    def test_rejects_invalid_provider_request_policy(self) -> None:
        for provider in (
            {"request_timeout_seconds": 0, "max_retries": 2},
            {"request_timeout_seconds": True, "max_retries": 2},
            {"request_timeout_seconds": 300, "max_retries": -1},
            {"request_timeout_seconds": 300, "max_retries": True},
            {"request_timeout_seconds": 300, "max_retries": 2, "extra": 1},
        ):
            with self.subTest(provider=provider):
                with self.assertRaises(ValueError):
                    self.load(
                        {
                            "provider": provider,
                            "main_agent": {"tools": ENABLED_TOOLS},
                        }
                    )

    def test_defaults_generation_limit_to_none(self) -> None:
        config = self.load(
            {"main_agent": {"tools": ENABLED_TOOLS}}
        )

        self.assertIsNone(config.max_generation_tokens)

    def test_loads_memory_config(self) -> None:
        config = self.load(
            {
                "memory": {
                    "enabled": False,
                    "global_max_tokens": 1200,
                    "workspace_max_tokens": 2400,
                },
                "main_agent": {"tools": ENABLED_TOOLS},
            }
        )

        self.assertEqual(config.memory, MemoryConfig(False, 1200, 2400))

    def test_defaults_memory_config(self) -> None:
        config = self.load({"main_agent": {"tools": ENABLED_TOOLS}})

        self.assertEqual(config.memory, MemoryConfig())

    def test_rejects_invalid_memory_config(self) -> None:
        for memory in (
            {"enabled": "yes"},
            {"global_max_tokens": 0},
            {"workspace_max_tokens": True},
            {"unexpected": 1},
        ):
            with self.subTest(memory=memory):
                with self.assertRaises(ValueError):
                    self.load(
                        {
                            "memory": memory,
                            "main_agent": {"tools": ENABLED_TOOLS},
                        }
                    )

    def test_rejects_invalid_generation_limit(self) -> None:
        for value in (0, True, "100"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "max_generation_tokens.*positive integer or null",
                ):
                    self.load(
                        {
                            "max_generation_tokens": value,
                            "main_agent": {"tools": ENABLED_TOOLS},
                        }
                    )

    def test_rejects_unknown_compression_field(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "context.compression.*unexpected",
        ):
            self.load(
                {
                    "context": {
                        "compression": {
                            "unexpected": 0.5,
                        }
                    },
                    "main_agent": {"tools": ENABLED_TOOLS},
                }
            )

    def test_loads_context_unit_retention(self) -> None:
        config = self.load(
            {
                "context": {
                    "compression": {
                        "keep_recent_units": 7,
                    }
                },
                "main_agent": {"tools": ENABLED_TOOLS},
            }
        )

        self.assertEqual(config.context.keep_recent_units, 7)

    def test_rejects_invalid_context_unit_retention(self) -> None:
        for value in (0, -1, True, "4"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "keep_recent_units.*positive integer",
                ):
                    self.load(
                        {
                            "context": {
                                "compression": {
                                    "keep_recent_units": value,
                                }
                            },
                            "main_agent": {"tools": ENABLED_TOOLS},
                        }
                    )

    def test_rejects_missing_tool_config(self) -> None:
        with self.assertRaisesRegex(ValueError, "'main_agent'.*object"):
            self.load_json(
                {
                    "max_same_tool_calls": 5,
                    "output_reserve_tokens": 100,
                    "provider": {
                        "request_timeout_seconds": 300,
                        "max_retries": 2,
                    },
                    "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
                }
            )

    def test_defaults_missing_tools_to_disabled(self) -> None:
        self.assertEqual(
            self.load({"main_agent": {"tools": {"read_file": True}}}).tools,
            ToolConfig(enabled=("read_file",)),
        )

    def test_orders_enabled_tools_by_the_canonical_tool_list(self) -> None:
        """Tool schemas are a prompt-cache prefix, so their order is fixed.

        The order follows TOOL_NAMES rather than the config's key order, so
        two configs that enable the same tools produce the same prefix.
        """
        self.assertEqual(
            self.load(
                {
                    "main_agent": {
                        "tools": {
                            "shell": True,
                            "read_file": True,
                            "list_directory": True,
                        }
                    }
                }
            ).tools.enabled,
            ("read_file", "list_directory", "shell"),
        )

    def test_rejects_non_boolean_tool_setting(self) -> None:
        with self.assertRaisesRegex(ValueError, "tools.shell.*boolean"):
            self.load(
                {
                    "main_agent": {
                        "tools": {**ENABLED_TOOLS, "shell": "true"},
                    }
                }
            )

    def test_rejects_unknown_tool_setting(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.load(
                {
                    "main_agent": {
                        "tools": {**ENABLED_TOOLS, "unknown": True},
                    }
                }
            )

    def test_rejects_unknown_main_agent_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "'main_agent': provider"):
            self.load(
                {
                    "main_agent": {
                        "tools": ENABLED_TOOLS,
                        "provider": "openai",
                    }
                }
            )


class SubagentRoleConfigTest(unittest.TestCase):
    def load(self, roles: dict) -> AgentConfig:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "provider": {
                            "request_timeout_seconds": 300,
                            "max_retries": 2,
                        },
                        "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
                        "main_agent": {"tools": ENABLED_TOOLS},
                        "subagent_roles": roles,
                    }
                ),
                encoding="utf-8",
            )
            return load_agent_config(path)

    def test_a_role_is_enabled_by_default(self) -> None:
        config = self.load(
            {"researcher": {"description": "Reads.", "tools": {}}}
        )

        self.assertTrue(config.subagent_roles["researcher"].enabled)

    def test_keeps_a_disabled_role_with_its_switch_off(self) -> None:
        """A disabled role stays in the config so it can be turned back on."""
        config = self.load(
            {
                "researcher": {
                    "enabled": False,
                    "description": "Reads.",
                    "tools": {"read_file": True},
                }
            }
        )

        role = config.subagent_roles["researcher"]
        self.assertFalse(role.enabled)
        self.assertEqual(role.tools.enabled, ("read_file",))

    def test_rejects_a_non_boolean_enabled(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "subagent_roles.researcher.enabled.*boolean"
        ):
            self.load(
                {
                    "researcher": {
                        "enabled": "yes",
                        "description": "Reads.",
                        "tools": {},
                    }
                }
            )

    def test_rejects_an_unknown_role_field(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "'subagent_roles.researcher': provider"
        ):
            self.load(
                {
                    "researcher": {
                        "description": "Reads.",
                        "provider": "openai",
                        "tools": {},
                    }
                }
            )


if __name__ == "__main__":
    unittest.main()

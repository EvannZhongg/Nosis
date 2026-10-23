import json
import tempfile
import unittest
from pathlib import Path

from agent_core import (
    AgentConfig,
    MemoryConfig,
    ProviderRequestConfig,
    SkillConfig,
    ToolConfig,
    load_agent_config,
)
from agent_core.tools.config import ROLE_TOOL_NAMES, TOOL_NAMES


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
                "skills": {
                    "global_paths": ["~/.nosis/skills"],
                    "workspace_paths": [
                        "skills",
                        ".agents/skills",
                        ".nosis/skills",
                    ],
                },
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

    def test_loads_global_and_workspace_skill_paths(self) -> None:
        config = self.load(
            {
                "skills": {
                    "global_paths": ["skills", "~/shared-skills"],
                    "workspace_paths": [".agents/skills", "/opt/skills"],
                },
                "main_agent": {"tools": ENABLED_TOOLS},
            }
        )

        self.assertEqual(
            config.skills,
            SkillConfig(
                global_paths=("skills", "~/shared-skills"),
                workspace_paths=(".agents/skills", "/opt/skills"),
            ),
        )

    def test_requires_skill_path_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "config field 'skills'.*object"):
            self.load_json(
                {
                    "max_same_tool_calls": 5,
                    "output_reserve_tokens": 100,
                    "provider": {
                        "request_timeout_seconds": 300,
                        "max_retries": 2,
                    },
                    "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
                    "main_agent": {"tools": ENABLED_TOOLS},
                }
            )

    def test_rejects_invalid_skill_path_configuration(self) -> None:
        for skills in (
            {"global_paths": "skills", "workspace_paths": []},
            {"global_paths": ["skills"], "workspace_paths": [""]},
            {"global_paths": ["skills", "skills"], "workspace_paths": []},
        ):
            with self.subTest(skills=skills):
                with self.assertRaises(ValueError):
                    self.load(
                        {
                            "skills": skills,
                            "main_agent": {"tools": ENABLED_TOOLS},
                        }
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
                    "skills": {
                        "global_paths": ["skills"],
                        "workspace_paths": [],
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

    def test_rejects_invalid_max_same_tool_calls(self) -> None:
        for value in (0, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.load(
                        {
                            "max_same_tool_calls": value,
                            "main_agent": {"tools": ENABLED_TOOLS},
                        }
                    )

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
                    "skills": {
                        "global_paths": ["skills"],
                        "workspace_paths": [],
                    },
                    "main_agent": {"tools": ENABLED_TOOLS},
                }
                if provider is not None:
                    payload["provider"] = provider
                self.assertEqual(self.load_json(payload).provider, expected)

    def test_session_title_generation_switch_requires_a_boolean(self) -> None:
        for enabled in (True, False):
            self.assertIs(self.load({
                "generate_session_title": enabled,
                "main_agent": {"tools": ENABLED_TOOLS},
            }).generate_session_title, enabled)
        for value in (None, "false", 0, 1, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "generate_session_title"):
                self.load({"generate_session_title": value, "main_agent": {"tools": ENABLED_TOOLS}})

    def test_rejects_invalid_provider_request_policy(self) -> None:
        for provider in (
            {"request_timeout_seconds": 0, "max_retries": 2},
            {"request_timeout_seconds": True, "max_retries": 2},
            {"request_timeout_seconds": 300, "max_retries": -1},
            {"request_timeout_seconds": 300, "max_retries": True},
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

    def test_ignores_unknown_fields_in_config_objects(self) -> None:
        expected = self.load({"main_agent": {"tools": ENABLED_TOOLS}})
        config = self.load({
            "extra": {"enabled": False},
            "provider": {"extra": 1},
            "skills": {
                "global_paths": ["~/.nosis/skills"],
                "workspace_paths": ["skills", ".agents/skills", ".nosis/skills"],
                "extra": [],
            },
            "context": {"extra": True, "compression": {"extra": 0.5}},
            "memory": {"extra": 1},
            "main_agent": {"tools": ENABLED_TOOLS, "extra": "metadata"},
        })

        self.assertEqual(config, expected)

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

    def test_loads_user_anchor_token_budget(self) -> None:
        config = self.load({
            "context": {"compression": {"user_anchor_token_budget": 3500}},
            "main_agent": {"tools": ENABLED_TOOLS},
        })

        self.assertEqual(config.context.user_anchor_token_budget, 3500)
        default = self.load({"main_agent": {"tools": ENABLED_TOOLS}})
        self.assertEqual(default.context.user_anchor_token_budget, 2000)

    def test_rejects_invalid_user_anchor_token_budget(self) -> None:
        for value in (0, -1, True, "2000", 2.5, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError, "user_anchor_token_budget.*positive integer"
                ):
                    self.load({
                        "context": {"compression": {"user_anchor_token_budget": value}},
                        "main_agent": {"tools": ENABLED_TOOLS},
                    })

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
                    "skills": {
                        "global_paths": ["skills"],
                        "workspace_paths": [],
                    },
                }
            )

    def test_defaults_missing_tools_to_disabled(self) -> None:
        self.assertEqual(
            self.load({"main_agent": {"tools": {"read_file": True}}}).tools,
            ToolConfig(enabled=("read_file",)),
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

    def test_computer_tools_are_main_agent_only(self) -> None:
        self.assertIn("computer_screenshot", TOOL_NAMES)
        self.assertIn("computer_action", TOOL_NAMES)
        self.assertNotIn("computer_screenshot", ROLE_TOOL_NAMES)
        self.assertNotIn("computer_action", ROLE_TOOL_NAMES)

    def test_main_agent_can_configure_computer_tools(self) -> None:
        config = self.load(
            {
                "main_agent": {
                    "tools": {
                        "computer_screenshot": True,
                        "computer_action": True,
                    }
                }
            }
        )

        self.assertEqual(
            config.tools.enabled,
            ("computer_screenshot", "computer_action"),
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
                        "skills": {
                            "global_paths": ["skills"],
                            "workspace_paths": [],
                        },
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

    def test_mcp_references_are_parsed_without_discovery(self) -> None:
        selection = {
            "analyzer": True,
            "disabled": False,
            "docs": {"search": True, "read.page": True},
            "review-kit:writer": {"write": False},
        }
        config = self.load({"researcher": {"description": "Reads.", "mcp": selection}})
        self.assertEqual(config.subagent_roles["researcher"].mcp, selection)
        self.assertEqual(config.subagent_roles["researcher"].tools.enabled, ())

    def test_missing_or_empty_mcp_selects_no_servers(self) -> None:
        for settings in ({}, {"mcp": {}}):
            with self.subTest(settings=settings):
                config = self.load({"researcher": {"description": "Reads.", **settings}})
                self.assertEqual(config.subagent_roles["researcher"].mcp, {})

    def test_rejects_invalid_mcp_selection(self) -> None:
        for selection, message in (
            (None, "must be an object"),
            ([], "must be an object"),
            ({"docs": {}}, "empty tool map"),
            ({"docs": {"search": True, "write": False}}, "mix true and false"),
            ({"docs": {"search": 1}}, "must be a boolean"),
            ({"docs": {"search": "true"}}, "must be a boolean"),
            ({"docs": {"search": None}}, "must be a boolean"),
            ({"docs": 1}, "boolean or an object"),
            ({"docs": []}, "boolean or an object"),
            ({"": True}, "non-empty server names"),
            ({"docs": {" ": True}}, "non-empty tool names"),
        ):
            with self.subTest(selection=selection):
                with self.assertRaisesRegex(ValueError, f"subagent_roles.researcher.mcp.*{message}"):
                    self.load({"researcher": {"description": "Reads.", "mcp": selection}})

    def test_ignores_unknown_role_fields(self) -> None:
        expected = self.load({"researcher": {"description": "Reads.", "tools": {}}})
        config = self.load({"researcher": {
            "description": "Reads.",
            "tools": {},
            "extra": {"enabled": False},
        }})

        self.assertEqual(config, expected)

    def test_rejects_computer_tools_for_a_role(self) -> None:
        with self.assertRaisesRegex(ValueError, "computer_action"):
            self.load(
                {
                    "researcher": {
                        "description": "Reads.",
                        "tools": {"computer_action": True},
                    }
                }
            )


if __name__ == "__main__":
    unittest.main()

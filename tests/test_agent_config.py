import json
import tempfile
import unittest
from pathlib import Path

from agent_core import AgentConfig, ToolConfig, load_agent_config


ENABLED_TOOLS = {
    "read_file": True,
    "edit_file": True,
    "search_files": True,
    "list_directory": True,
    "shell": True,
    "web_search": False,
}


class AgentConfigTest(unittest.TestCase):
    def load(self, fields: dict) -> AgentConfig:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": 5,
                        "max_output_tokens": 100,
                        **fields,
                    }
                ),
                encoding="utf-8",
            )
            return load_agent_config(path)

    def test_loads_agent_behavior_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": 5,
                        "max_output_tokens": 100,
                        "shell_timeout_seconds": 30,
                        "main_agent": {
                            "tools": {
                                **ENABLED_TOOLS,
                                "edit_file": False,
                                "shell": False,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_agent_config(path),
                AgentConfig(
                    max_same_tool_calls=5,
                    max_output_tokens=100,
                    shell_timeout_seconds=30,
                    tools=ToolConfig(
                        enabled=(
                            "read_file",
                            "search_files",
                            "list_directory",
                        )
                    ),
                ),
            )

    def test_defaults_shell_timeout_to_60_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": 5,
                        "max_output_tokens": 100,
                        "main_agent": {"tools": ENABLED_TOOLS},
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_agent_config(path).shell_timeout_seconds,
                60,
            )

    def test_rejects_invalid_shell_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            for timeout_seconds in (0, 901, True):
                with self.subTest(timeout_seconds=timeout_seconds):
                    path.write_text(
                        json.dumps(
                            {
                                "max_same_tool_calls": 5,
                                "max_output_tokens": 100,
                                "shell_timeout_seconds": timeout_seconds,
                                "main_agent": {"tools": ENABLED_TOOLS},
                            }
                        ),
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(
                        ValueError,
                        "shell_timeout_seconds.*between 1 and 900",
                    ):
                        load_agent_config(path)

    def test_rejects_non_positive_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": 0,
                        "max_output_tokens": 100,
                        "main_agent": {"tools": ENABLED_TOOLS},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_agent_config(path)

    def test_rejects_boolean_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": True,
                        "max_output_tokens": 100,
                        "main_agent": {"tools": ENABLED_TOOLS},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_agent_config(path)

    def test_rejects_non_positive_output_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": 5,
                        "max_output_tokens": 0,
                        "main_agent": {"tools": ENABLED_TOOLS},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_agent_config(path)

    def test_rejects_missing_tool_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": 5,
                        "max_output_tokens": 100,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "'main_agent'.*object"):
                load_agent_config(path)

    def test_defaults_missing_tools_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": 5,
                        "max_output_tokens": 100,
                        "main_agent": {"tools": {"read_file": True}},
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_agent_config(path).tools,
                ToolConfig(enabled=("read_file",)),
            )

    def test_orders_enabled_tools_by_the_canonical_tool_list(self) -> None:
        """Tool schemas are a prompt-cache prefix, so their order is fixed.

        The order follows TOOL_NAMES rather than the config's key order, so
        two configs that enable the same tools produce the same prefix.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": 5,
                        "max_output_tokens": 100,
                        "main_agent": {
                            "tools": {
                                "shell": True,
                                "read_file": True,
                                "list_directory": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_agent_config(path).tools.enabled,
                ("read_file", "list_directory", "shell"),
            )

    def test_rejects_non_boolean_tool_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": 5,
                        "max_output_tokens": 100,
                        "main_agent": {
                            "tools": {
                                **ENABLED_TOOLS,
                                "shell": "true",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "tools.shell.*boolean",
            ):
                load_agent_config(path)

    def test_rejects_unknown_tool_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_same_tool_calls": 5,
                        "max_output_tokens": 100,
                        "main_agent": {
                            "tools": {
                                **ENABLED_TOOLS,
                                "unknown": True,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unknown"):
                load_agent_config(path)

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
                        "max_output_tokens": 100,
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

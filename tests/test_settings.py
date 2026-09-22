import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interfaces.bridge.settings import EnvironmentReloader, SettingsStore, configuration_fingerprint


PROVIDERS = {
    "main_agent": {"provider": "first", "vision_provider": ""},
    "subagent": {"provider": "", "vision_provider": ""},
    "subagent_roles": {},
    "providers": {"first": {"model": "openai/first", "key": "${FIRST_KEY}"}},
}

AGENT = {
    "max_same_tool_calls": 5,
    "output_reserve_tokens": 100,
    "max_generation_tokens": None,
    "workspace_instruction_files": ["AGENTS.md"],
    "context": {"compression": {"enabled": True, "trigger_ratio": None, "keep_recent_units": 4}},
    "main_agent": {"tools": {"read_file": True}},
    "subagent_roles": {},
    "mcp": {"enabled": False, "servers": {}},
}


class SettingsStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "provider_config.json").write_text(json.dumps(PROVIDERS), encoding="utf-8")
        (self.root / "agent_config.json").write_text(json.dumps(AGENT), encoding="utf-8")
        (self.root / "skills").mkdir()
        (self.root / "plugins").mkdir()
        self.store = SettingsStore(self.root)

    def test_snapshot_redacts_provider_secret(self) -> None:
        (self.root / ".env").write_text("FIRST_KEY=secret\n", encoding="utf-8")
        snapshot = self.store.snapshot()
        self.assertNotIn("secret", json.dumps(snapshot))
        self.assertEqual(snapshot["providers"][0]["credential"], {
            "source": "env", "env_name": "FIRST_KEY", "configured": True,
        })
        self.assertEqual(snapshot["agent"]["memory"], {
            "enabled": True,
            "global_max_tokens": 2000,
            "workspace_max_tokens": 3000,
        })

    def test_provider_save_is_validated_and_writes_secret_to_dotenv(self) -> None:
        snapshot = self.store.save_provider("second", {
            "model": "openai/second",
            "url": "https://example.test/v1",
            "max_context_tokens": 1000,
            "api_key": {"action": "set", "value": "new-secret"},
            "set_default": True,
        })
        document = json.loads((self.root / "provider_config.json").read_text())
        self.assertEqual(document["main_agent"]["provider"], "second")
        self.assertEqual(document["providers"]["second"]["key"], "${NOSIS_SECOND_API_KEY}")
        self.assertIn("NOSIS_SECOND_API_KEY=new-secret", (self.root / ".env").read_text())
        self.assertEqual(snapshot["default_provider"], "second")

    def test_invalid_agent_update_does_not_replace_file(self) -> None:
        before = (self.root / "agent_config.json").read_text()
        with self.assertRaises(ValueError):
            self.store.save_agent({"max_same_tool_calls": 0})
        self.assertEqual((self.root / "agent_config.json").read_text(), before)

    def test_rejects_a_stale_revision(self) -> None:
        revision = configuration_fingerprint(self.root)
        (self.root / ".env").write_text("FIRST_KEY=changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            self.store.save_agent({"max_same_tool_calls": 6}, revision)

    def test_rejects_secret_line_breaks(self) -> None:
        with self.assertRaisesRegex(ValueError, "line breaks"):
            self.store.save_provider("first", {
                "model": "openai/first",
                "url": None,
                "max_context_tokens": None,
                "api_key": {"action": "set", "value": "first\nSECOND=leak"},
            })

    def test_preserves_dotenv_comments_and_unrelated_values(self) -> None:
        (self.root / ".env").write_text("# local values\nOTHER=value\nFIRST_KEY=old\n", encoding="utf-8")
        self.store.save_provider("first", {
            "model": "openai/first",
            "url": None,
            "max_context_tokens": None,
            "api_key": {"action": "set", "value": "new"},
        })
        content = (self.root / ".env").read_text()
        self.assertIn("# local values", content)
        self.assertIn("OTHER=value", content)
        self.assertIn("FIRST_KEY=new", content)

    def test_updates_provider_routing(self) -> None:
        snapshot = self.store.snapshot()
        updated = self.store.save_routing({
            "main_agent": "first",
            "vision_provider": None,
            "subagent": "first",
            "subagent_vision_provider": None,
            "roles": {},
        }, snapshot["revision"])
        self.assertEqual(updated["routing"]["subagent"], "first")


class EnvironmentReloaderTest(unittest.TestCase):
    def test_reloads_changes_and_removes_deleted_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("TOKEN=first\n", encoding="utf-8")
            reloader = EnvironmentReloader(path)
            reloader.reload()
            self.assertEqual(os.environ["TOKEN"], "first")
            self.assertEqual(reloader.loaded_names, frozenset({"TOKEN"}))
            path.write_text("TOKEN=second\n", encoding="utf-8")
            reloader.reload()
            self.assertEqual(os.environ["TOKEN"], "second")
            path.write_text("", encoding="utf-8")
            reloader.reload()
            self.assertNotIn("TOKEN", os.environ)
            self.assertEqual(reloader.loaded_names, frozenset())


if __name__ == "__main__":
    unittest.main()

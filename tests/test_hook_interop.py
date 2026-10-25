"""Plugin hooks assembled by the shared Runtime and exposed through Bridge."""

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

from agent_core import (
    AgentTurnAction, ExecutionScope, HostCommandExecutor, JsonlSessionStore,
    LLMResponse, OneShotTrigger, PermissionPreset, Schedule, ScheduledRun,
    SchedulerService, ToolCall,
)
from agent_runtime.plane_manager import ExecutionPlaneManager
from agent_runtime.plugins import PluginManager
from agent_runtime.scheduled_runner import ScheduledTurnRunner
from tests.agent_fixtures import MockProvider
from tests.bridge_fixtures import BridgeTestCase, emitted, open_session_message
from tests.plugin_fixtures import validator


def calls(*items):
    return LLMResponse(content=None, tool_calls=items)


class HookInteropTest(BridgeTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.plugin = self.root / "plugins" / "repo-review-kit"
        shutil.copytree(Path(__file__).with_name("nosis-plugin-testkit"), self.plugin)
        self.hook_path = self.plugin / "hooks/hooks.json"
        self.hook_config = json.loads(self.hook_path.read_text(encoding="utf-8"))
        for entries in self.hook_config["hooks"].values():
            entries[0]["command"] = sys.executable
        self.hook_path.write_text(json.dumps(self.hook_config), encoding="utf-8")
        self.main = MockProvider([])
        self.child = MockProvider([])
        self.enterContext(patch.object(ExecutionPlaneManager, "_provider_for", side_effect=lambda config, *_:
            None if config is None else self.child if config.model == "child" else self.main))
        # Runtime integration isolates assembly from OS-specific sandbox tests.
        self.enterContext(patch("agent_runtime.plane_manager.SandboxedCommandExecutor",
                                side_effect=lambda workspace, *args, **kwargs: HostCommandExecutor(workspace)))

    def open(self):
        bridge, stdout = self.make_bridge([], self.root)
        bridge.open_session(open_session_message(self.root, agent_config={
            "max_same_tool_calls": 5, "output_reserve_tokens": 100, "generate_session_title": False,
            "workspace_instruction_files": [], "memory": {"enabled": False},
            "main_agent": {"tools": {"subagent": True, "shell": True, "read_file": True, "write_file": True}},
        }, provider_config={
            "main_agent": {"provider": "main"}, "subagent": {"provider": "child"},
            "providers": {"main": {"model": "main", "max_context_tokens": 1000},
                          "child": {"model": "child", "max_context_tokens": 1000}},
        }))
        bridge.host.set_permission_preset(PermissionPreset.FULL_ACCESS)
        return bridge, stdout

    def install_rule(self, filename, name="rule"):
        destination = self.root / ".nosis" / f"hookify.{name}.json"
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(self.plugin / "examples" / filename, destination)

    def test_native_fixture_validates_and_routes_four_hooks(self):
        errors, registered = validator.validate_plugin(self.plugin)
        self.assertEqual(errors, [])
        self.assertIn("repo-review-kit:check-completion", registered)
        manager = PluginManager.discover(self.root / "plugins")
        self.assertEqual(len(manager.load_hooks()[0]), 4)
        bridge, _ = self.open()
        self.assertIn("hooks", bridge.host.settings.snapshot()["plugins"][0]["components"])

    def test_bridge_skill_child_and_hooks_share_execution_semantics(self):
        self.main = MockProvider([
            calls(ToolCall("skill", "read_skill", {"name": "repo-review-kit:writing-rules"})),
            calls(ToolCall("delegate", "subagent", {"role": "repo-review-kit:conversation-analyzer", "task": "Read sample.txt; supplied excerpt: avoid debug output."})),
            "Done",
        ])
        self.child = MockProvider([calls(ToolCall("read", "read_file", {"path": "sample.txt"})), "Suggested rule"])
        (self.root / "sample.txt").write_text("source")
        bridge, stdout = self.open()
        bridge.run_turn({"turn_id": "review", "text": "Use hookify"})
        messages = emitted(stdout)
        self.assertTrue(any(message["type"] == "turn_completed" for message in messages), messages)
        hook_events = [message for message in messages if message["type"] == "hook_completed"]
        parent_id = bridge.host.sessions.session.session_id
        child_ids = {message["session_id"] for message in hook_events} - {parent_id}
        self.assertEqual(len(child_ids), 1)
        self.assertEqual(len(self.child.requests), 2)
        self.assertNotIn("_hook_command", bridge.host.planes.current.catalog.names)
        self.assertTrue(all(tool.name != "_hook_command" for request in self.main.requests for tool in request.tools))
        journals = list((self.root / "sessions").rglob("subagents/*/*.jsonl"))
        self.assertEqual(len(journals), 1)
        child_events = [json.loads(line) for line in journals[0].read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(event["payload"].get("input", {}).get("source") == "delegation" for event in child_events))
        self.assertEqual(len(list((self.root / "sessions").rglob("*.jsonl"))), 2)

    def test_rule_edits_apply_next_turn_and_refresh_removes_disabled_hooks(self):
        self.main = MockProvider([
            calls(ToolCall("a", "write_file", {"path": "project/.env", "content": "one"})), "one",
            calls(ToolCall("b", "write_file", {"path": "project/.env", "content": "two", "overwrite": True})), "two",
            "three",
        ])
        bridge, stdout = self.open()
        bridge.run_turn({"turn_id": "one", "text": "write"})
        plane = bridge.host.planes.current
        self.assertEqual((self.root / "project/.env").read_text(), "one")
        self.install_rule("block-sensitive-file.json")
        bridge.run_turn({"turn_id": "two", "text": "write again"})
        self.assertIs(bridge.host.planes.current, plane)
        self.assertEqual((self.root / "project/.env").read_text(), "one")
        self.assertTrue(any(message["type"] == "tool_result" and message["error"] and message["error"]["type"] == "HookDenied" for message in emitted(stdout)))
        manifest = json.loads((self.plugin / "plugin.json").read_text(encoding="utf-8"))
        manifest["enabled"] = False
        (self.plugin / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        bridge.host.refresh_runtime()
        self.assertEqual(bridge.host.planes.current.hooks.definitions, ())
        self.assertNotIn("repo-review-kit:writing-rules", [s.identifier for s in bridge.host.planes.current.skills])

    def test_stop_requires_actual_successful_test_and_resets_between_turns(self):
        self.install_rule("require-tests.json")
        (self.root / "test_sample.py").write_text("import unittest\nclass Sample(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n")
        self.main = MockProvider([
            "I mention pytest but have not run it.",
            calls(ToolCall("test", "shell", {"command": "python -m unittest", "scope": "host"})),
            "Tests passed",
            "Next turn", "Still no tests",
        ])
        bridge, stdout = self.open()
        bridge.run_turn({"turn_id": "test", "text": "test"})
        self.assertTrue(any(message["type"] == "turn_completed" for message in emitted(stdout)), emitted(stdout))
        bridge.run_turn({"turn_id": "next", "text": "new work"})
        failures = [message for message in emitted(stdout) if message["type"] == "turn_failed"]
        self.assertEqual(failures[-1]["error"]["type"], "HookRejectedError")

    def test_scheduled_turn_uses_same_hooks_and_records_input_source(self):
        bridge, _ = self.open()
        bridge.close()
        self.main = MockProvider(["scheduled done"])
        now = datetime.now(timezone.utc)
        schedule = Schedule("schedule", OneShotTrigger(now), AgentTurnAction("scheduled work"),
                            str(self.root), "origin", "scheduled-session", ExecutionScope.WORKSPACE)
        run = ScheduledRun("run", "schedule", "scheduled-session", "scheduled-turn", now)
        scheduler = SchedulerService(self.root / "schedule.jsonl")
        self.addCleanup(scheduler.close)
        ScheduledTurnRunner(self.root, scheduler)(schedule, run)
        session = JsonlSessionStore(self.root / "sessions").load("scheduled-session")
        self.assertEqual(session.turns["scheduled-turn"].status, "completed")
        self.assertTrue(any(event.payload.get("input", {}).get("source") == "scheduled" for event in session.journal))

    def test_session_rebinding_reuses_definitions_without_turn_state(self):
        self.main = MockProvider(["first", "second"])
        bridge, _ = self.open()
        bridge.run_turn({"turn_id": "first", "text": "first"})
        plane = bridge.host.planes.current
        first = bridge.host.sessions.session
        bridge.host.open_session(self.root, session_id="other")
        bridge.host.set_permission_preset(PermissionPreset.FULL_ACCESS)
        result = bridge.host.run_turn("second", "second")
        self.assertEqual(result.status, "completed")
        self.assertIs(bridge.host.planes.current, plane)
        second = bridge.host.sessions.session
        self.assertEqual(len([event for event in first.journal if event.event_type == "hook_finished"]), 2)
        self.assertEqual(len([event for event in second.journal if event.event_type == "hook_finished"]), 2)

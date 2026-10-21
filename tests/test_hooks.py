"""Core hook semantics using the shared native Plugin Testkit."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
from unittest.mock import Mock

from agent_core import (
    Agent, AgentCancelled, ExecutionRouter, FULL_ACCESS_AUTHORITY, HostCommandExecutor,
    JsonlSessionStore, LLMResponse, PermissionController, PermissionPreset, Session,
    ShellApprovalPolicy, ToolCall, ToolCatalog, ToolExecutionContext, TurnControl, Workspace,
    WORKSPACE_ONLY_AUTHORITY, ExecutionScope, SandboxedCommandExecutor, WindowsSandboxBackend,
    JobManager,
)
from agent_core.hooks import HookExecutionError, HookRejectedError, HookRunner
from agent_core.projection import project_context_units
from agent_core.tools.builtin import ReadFileTool, ShellTool, WriteFileTool
from agent_runtime.plugins import PluginManager
from agent_core.mcp.tool import McpTool
from tests.test_agent import AGENT_CONFIG, MockProvider


FIXTURE = Path(__file__).with_name("nosis-plugin-testkit")


class HookTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.plugin = self.root / "plugins" / "repo-review-kit"
        shutil.copytree(FIXTURE, self.plugin)
        config = json.loads((self.plugin / "hooks/hooks.json").read_text(encoding="utf-8"))
        for entries in config["hooks"].values():
            entries[0]["command"] = sys.executable
        (self.plugin / "hooks/hooks.json").write_text(json.dumps(config), encoding="utf-8")
        hooks, warnings = PluginManager.discover(self.root / "plugins").load_hooks()
        self.assertEqual(warnings, ())
        self.hooks = HookRunner(hooks)
        self.session = Session()
        self.session.permission_preset = PermissionPreset.FULL_ACCESS
        self.approvals = []
        self.policy = PermissionController(self.session, ShellApprovalPolicy(self.approve))
        executor = HostCommandExecutor(self.workspace)
        self.router = ExecutionRouter(executor, executor, authority=lambda: self.policy.authority)
        self.events = []
        self.context = ToolExecutionContext(
            Workspace(self.workspace), self.session, sessions_directory=self.root / "sessions",
            hooks=self.hooks, execution_router=self.router, on_hook_event=self.events.append,
        )
        self.catalog = ToolCatalog((ReadFileTool(), WriteFileTool(), ShellTool()))

    def approve(self, command):
        self.approvals.append(command)
        return True

    def rule(self, event, *, action="block", conditions=None, tools=None, name="test"):
        directory = self.workspace / ".nosis"
        directory.mkdir(exist_ok=True)
        data = {"name": name, "event": event, "action": action, "message": "规则反馈",
                "conditions": conditions or [{"field": "input.text", "operator": "contains", "value": "blocked"}]}
        if tools is not None:
            data["tools"] = tools
        (directory / f"hookify.{name}.json").write_text(json.dumps(data), encoding="utf-8")

    def agent(self, responses, context=None):
        context = context or self.context
        provider = MockProvider(responses)
        agent = Agent(provider, context.session, "System", "Compress", AGENT_CONFIG,
                      self.catalog.select(self.catalog.names, context, policy=self.policy), context)
        return agent, provider

    def test_pre_deny_prevents_side_effect_and_preserves_tool_pair(self):
        self.rule("PreToolUse", tools=["write_file"], conditions=[
            {"field": "tool_call.arguments.path", "operator": "equals", "value": "secret.txt"},
        ])
        call = ToolCall("write", "write_file", {"path": "secret.txt", "content": "secret"})
        agent, provider = self.agent([LLMResponse(content=None, tool_calls=(call,)), "Done"])
        agent.run("write")
        self.assertFalse((self.workspace / "secret.txt").exists())
        result = next(item for item in self.session.items if item.role == "tool")
        self.assertEqual(result.tool_call_id, call.id)
        self.assertEqual(json.loads(result.content)["error"]["type"], "HookDenied")
        self.assertFalse(any(event.event == "PostToolUse" for event in self.events))
        self.assertIn("规则反馈", provider.requests[-1].messages[-1].content)

    def test_feedback_follows_complete_batch_and_is_not_user_authored(self):
        self.rule("PreToolUse", action="warn", tools=["write_file"], conditions=[
            {"field": "tool_call.arguments.content", "operator": "contains", "value": "debug"},
        ])
        self.rule("PostToolUse", action="warn", name="after", conditions=[
            {"field": "tool_result.ok", "operator": "equals", "value": True},
        ])
        calls = tuple(ToolCall(str(i), "write_file", {"path": f"{i}.txt", "content": "debug"}) for i in range(2))
        agent, provider = self.agent([LLMResponse(content=None, tool_calls=calls), "Done"])
        agent.run("write")
        messages = provider.requests[-1].messages
        tool_indices = [i for i, item in enumerate(messages) if item.role == "tool"]
        feedback_index = next(i for i, item in enumerate(messages) if item.origin == "hook_feedback")
        self.assertGreater(feedback_index, max(tool_indices))
        self.assertEqual(len(self.session.user_anchors), 1)
        feedback = next(item for item in self.session.items if item.origin == "hook_feedback")
        self.assertFalse(feedback.is_user_authored)
        self.assertEqual(len(project_context_units(self.session.items)[1].messages), 4)
        self.assertTrue((self.workspace / "1.txt").is_file())

    def test_mcp_call_is_checked_before_reaching_the_manager(self):
        tool = McpTool("example", "write", "write", {"type": "object"})
        manager = Mock()
        context = replace(self.context, mcp=manager)
        self.rule("PreToolUse", tools=[tool.name], conditions=[
            {"field": "tool_call.name", "operator": "equals", "value": tool.name},
        ])
        result = ToolCatalog((tool,)).select((tool.name,), context, policy=self.policy).execute(ToolCall("mcp", tool.name, {}))
        self.assertEqual(result.error.type, "HookDenied")
        self.assertEqual(manager.mock_calls, [])

    def test_parallel_calls_keep_hook_feedback_after_the_result_sequence(self):
        class ConcurrentRead(ReadFileTool):
            concurrent = True
        self.catalog = ToolCatalog((ConcurrentRead(),))
        self.rule("PostToolUse", action="warn", conditions=[
            {"field": "tool_result.ok", "operator": "equals", "value": True},
        ])
        (self.workspace / "a").write_text("a")
        calls = tuple(ToolCall(str(i), "read_file", {"path": "a"}) for i in range(2))
        agent, provider = self.agent([LLMResponse(content=None, tool_calls=calls), "done"])
        agent.run("read")
        messages = provider.requests[-1].messages
        self.assertEqual([item.tool_call_id for item in messages if item.role == "tool"], ["0", "1"])
        self.assertEqual(messages[-1].origin, "hook_feedback")
        invocation_ids = [event.invocation_id for event in self.events]
        self.assertEqual(len(invocation_ids), len(set(invocation_ids)))

    def test_post_failure_keeps_committed_tool_side_effect_and_result(self):
        definition = replace(self.hooks.definitions[2], command=sys.executable, args=("-c", "print('invalid')"))
        context = replace(self.context, hooks=HookRunner((definition,)))
        call = ToolCall("write", "write_file", {"path": "written", "content": "saved"})
        agent, _ = self.agent([LLMResponse(content=None, tool_calls=(call,))], context)
        with self.assertRaises(HookExecutionError):
            agent.run("write")
        self.assertEqual((self.workspace / "written").read_text(), "saved")
        self.assertEqual(self.session.tool_executions["write"].status, "completed")
        self.assertTrue(any(item.tool_call_id == "write" for item in self.session.items))

    def test_background_test_is_checked_only_after_job_completion(self):
        self.rule("Stop", conditions=[{"field": "tests_passed", "operator": "equals", "value": False}])
        (self.workspace / "test_sample.py").write_text(
            "import time, unittest\nclass Sample(unittest.TestCase):\n    def test_ok(self): time.sleep(.3)\n")
        jobs = JobManager(self.session)
        self.addCleanup(jobs.close)
        context = replace(self.context, jobs=jobs)
        call = ToolCall("tests", "shell", {"command": "python -m unittest", "background": True})
        agent, _ = self.agent([LLMResponse(content=None, tool_calls=(call,)), "candidate", "done"], context)
        agent.run("test")
        self.assertTrue(all(job.status == "completed" and job.result_summary["exit_code"] == 0 for job in self.session.jobs.values()))
        self.assertTrue(all(event.decision == "pass" for event in self.events if event.event == "Stop"))

    def test_large_stderr_is_artifact_diagnostics_not_a_protocol_failure(self):
        definition = replace(self.hooks.definitions[0], command=sys.executable, args=(
            "-c", "import sys; sys.stderr.write('x' * 70000); print('{}')",
        ))
        context = replace(self.context, hooks=HookRunner((definition,)))
        agent, _ = self.agent(["done"], context)
        self.assertEqual(agent.run("work").content, "done")
        self.assertTrue(list((self.root / "sessions").rglob("*.txt")))

    def test_input_rejection_never_enters_provider_context(self):
        self.rule("InputSubmitted")
        agent, provider = self.agent([])
        with self.assertRaises(HookRejectedError):
            agent.run("blocked")
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.session.items, [])
        self.assertTrue(any(event.payload.get("input", {}).get("text") == "blocked" for event in self.session.journal))

    def test_steering_uses_input_hook_and_cannot_bypass_rejection(self):
        self.rule("InputSubmitted")
        control = TurnControl()
        control.steer("s", "blocked")
        (self.workspace / "a.txt").write_text("a")
        response = LLMResponse(content=None, tool_calls=(ToolCall("read", "read_file", {"path": "a.txt"}),))
        agent, provider = self.agent([response])
        with self.assertRaises(HookRejectedError):
            agent.run("read", turn_control=control)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual([item.content for item in self.session.items if item.is_user_authored], ["read"])

    def test_stop_correction_is_bounded_and_control_remains_open(self):
        self.rule("Stop", conditions=[{"field": "response", "operator": "equals", "value": "bad"}])
        control = TurnControl()
        def notify(event):
            self.events.append(event)
            if event.event == "Stop" and event.decision == "continue":
                self.assertTrue(control.steer("extra", "please fix"))
        context = replace(self.context, on_hook_event=notify)
        agent, provider = self.agent(["bad", "good"], context)
        self.assertEqual(agent.run("work", turn_control=control).content, "good")
        self.assertFalse(control.steer("late", "late"))
        self.assertTrue(any(item.content == "please fix" for item in provider.requests[-1].messages))

    def test_stop_never_silently_allows_repeated_rejection(self):
        self.rule("Stop", conditions=[{"field": "response", "operator": "equals", "value": "bad"}])
        agent, provider = self.agent(["bad", "bad"])
        with self.assertRaises(HookRejectedError):
            agent.run("work")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(next(iter(self.session.turns.values())).status, "failed")

    def test_cancel_during_stop_wins_over_completion(self):
        control = TurnControl()
        context = replace(self.context, on_hook_event=lambda event: control.cancel() if event.event == "Stop" else None)
        agent, _ = self.agent(["done"], context)
        with self.assertRaises(AgentCancelled):
            agent.run("work", turn_control=control)
        self.assertEqual(next(iter(self.session.turns.values())).status, "cancelled")

    def test_steering_arriving_during_passing_stop_is_consumed(self):
        control = TurnControl()
        seen = []
        def notify(event):
            if event.event == "Stop" and not seen:
                seen.append(event)
                self.assertTrue(control.steer("extra", "more work"))
        agent, provider = self.agent(["candidate", "finished"], replace(self.context, on_hook_event=notify))
        self.assertEqual(agent.run("work", turn_control=control).content, "finished")
        self.assertEqual(len(provider.requests), 2)

    def test_hook_and_original_command_have_separate_approvals(self):
        self.session.set_permission_preset(PermissionPreset.ASK_FOR_APPROVAL)
        self.hooks = HookRunner(tuple(hook for hook in self.hooks.definitions if hook.event == "PreToolUse"))
        context = replace(self.context, hooks=self.hooks)
        tools = self.catalog.select(("shell",), context, policy=self.policy)
        result = tools.execute(ToolCall("shell", "shell", {"command": "echo hello"}))
        self.assertIsNone(result.error)
        self.assertEqual(len(self.approvals), 2)
        self.assertTrue(self.approvals[0].startswith("[Hook] "))
        self.assertEqual(self.approvals[1], "echo hello")
        self.assertEqual(len(self.events), 1)

    def test_cancel_during_hook_approval_prevents_process_launch(self):
        self.session.set_permission_preset(PermissionPreset.ASK_FOR_APPROVAL)
        control = TurnControl()
        policy = PermissionController(self.session, ShellApprovalPolicy(lambda _: control.cancel() or True))
        tools = self.catalog.select(("write_file",), self.context, policy=policy)
        with patch("agent_core.execution.process.subprocess.Popen") as launch:
            with self.assertRaises(AgentCancelled):
                tools.execute(ToolCall("write", "write_file", {"path": "x", "content": "x"}), turn_control=control)
        launch.assert_not_called()
        self.assertFalse((self.workspace / "x").exists())

    def test_host_hook_cannot_exceed_runtime_authority(self):
        definition = replace(self.hooks.definitions[0], scope=ExecutionScope.HOST)
        router = ExecutionRouter(HostCommandExecutor(self.workspace), HostCommandExecutor(self.workspace), authority=WORKSPACE_ONLY_AUTHORITY)
        context = replace(self.context, hooks=HookRunner((definition,)), execution_router=router)
        agent, provider = self.agent([], context)
        with self.assertRaisesRegex(HookExecutionError, "exceeds authority"):
            agent.run("work")
        self.assertEqual(provider.requests, [])

    def test_failures_preserve_diagnostics_and_do_not_execute_original_tool(self):
        for script, pattern in (
            ("print('invalid')", "Expecting value"),
            ("import sys; sys.exit(7)", "exited with 7"),
            ("print('{\"decision\": \"continue\"}')", "invalid decision"),
            ("import time; time.sleep(5)", "timed out"),
            ("print('x' * 70000)", "output budget"),
        ):
            with self.subTest(script=script):
                definition = replace(self.hooks.definitions[1], command=sys.executable, args=("-c", script), timeout_seconds=1)
                context = replace(self.context, hooks=HookRunner((definition,)))
                tools = self.catalog.select(("write_file",), context, policy=self.policy)
                with self.assertRaisesRegex(HookExecutionError, pattern):
                    tools.execute(ToolCall("write", "write_file", {"path": "x", "content": "x"}))
                self.assertFalse((self.workspace / "x").exists())
        diagnostics = [event.payload["diagnostics"] for event in self.session.journal if event.event_type == "hook_finished"]
        self.assertTrue(any("artifact_path" in (item or "") for item in diagnostics))
        self.assertTrue(list((self.root / "sessions").rglob("*.txt")))

    def test_running_hook_process_observes_cancellation(self):
        definition = replace(self.hooks.definitions[0], command=sys.executable, args=("-c", "import time; time.sleep(30)"))
        control = TurnControl()
        context = replace(self.context, hooks=HookRunner((definition,)))
        agent, provider = self.agent([], context)
        timer = threading.Timer(.2, control.cancel)
        timer.start()
        self.addCleanup(timer.join)
        started = time.monotonic()
        with self.assertRaises(AgentCancelled):
            agent.run("work", turn_control=control)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(provider.requests, [])

    def test_journal_replay_retains_hook_facts_and_feedback_origin(self):
        self.rule("InputSubmitted", action="warn")
        store = JsonlSessionStore(self.root / "sessions", group_by_workspace=False)
        with store.open_journal(self.session.session_id) as sink:
            self.session.attach_journal_sink(sink)
            agent, _ = self.agent(["done"])
            agent.run("blocked")
            self.session.attach_journal_sink(None)
        restored = store.load(self.session.session_id)
        self.assertTrue(any(event.event_type == "hook_finished" for event in restored.journal))
        self.assertTrue(any(item.origin == "hook_feedback" and not item.is_user_authored for item in restored.items))
        self.assertEqual(len(restored.user_anchors), 1)

    @unittest.skipUnless(os.name == "nt", "Windows restricted stdin integration")
    def test_windows_workspace_hook_reads_json_stdin(self):
        user = "*" + subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, check=True,
        ).stdout.split(b",")[-1].strip(b'"\r\n ').decode("ascii")
        subprocess.run(["icacls", str(self.plugin), "/grant", f"{user}:(OI)(CI)RX"], capture_output=True, check=True)
        executor = SandboxedCommandExecutor(self.workspace, WindowsSandboxBackend())
        self.addCleanup(executor.close)
        router = ExecutionRouter(executor, HostCommandExecutor(self.workspace), authority=FULL_ACCESS_AUTHORITY)
        context = replace(self.context, execution_router=router)
        agent, _ = self.agent(["done"], context)
        self.assertEqual(agent.run("中文输入").content, "done")
        self.assertEqual([event.event for event in self.events], ["InputSubmitted", "Stop"])

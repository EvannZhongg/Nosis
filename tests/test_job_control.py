import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_core import (
    CommandExecutionResult, ExecutionRouter, JobControlTool, JobManager,
    JsonlSessionStore, Session, ShellTool, ToolCall, ToolExecutionContext,
    ToolResultNormalizer, Workspace, builtin_catalog,
)
from agent_core.jobs import JobResult


class JobControlTest(unittest.TestCase):
    def setUp(self):
        self.session = Session("parent")
        self.session.begin_turn("turn")
        self.jobs = JobManager(self.session, max_workers=1)
        self.context = ToolExecutionContext(
            workspace=Workspace(Path(__file__).parent), session=self.session,
            jobs=self.jobs,
        )
        self.tool = JobControlTool()
        self.addCleanup(self.jobs.close)

    def call(self, action, job_id=None):
        arguments = {"action": action}
        if job_id is not None:
            arguments["job_id"] = job_id
        return self.tool.execute(arguments, self.context)

    def test_active_list_and_single_job_cancellation(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def run(job):
            entered.set()
            release.wait(5)
            job.cancellation.raise_if_cancelled()

        running = self.jobs.submit("subagent", "turn", run, details={"task": "work"})
        self.assertTrue(entered.wait(2))
        queued = self.jobs.submit("shell", "turn", lambda job: "unused", details={"command": "pwd"})
        listed = [job.to_dict() for job in self.jobs.list()]
        self.assertEqual([item["status"] for item in listed], ["running", "submitted"])
        self.assertEqual(listed[1]["elapsed_seconds"], 0)
        self.assertEqual(self.call("cancel", queued.job_id)["job"]["status"], "cancelled")
        self.assertFalse(self.call("get", running.job_id)["job"]["cancel_requested"])
        cancelling = self.call("cancel", running.job_id)["job"]
        self.assertEqual(cancelling["status"], "running")
        self.assertTrue(cancelling["cancel_requested"])
        release.set()
        self.jobs.wait_for_turn("turn")
        self.jobs.drain_terminal_state("turn")
        self.assertEqual(self.jobs.list(), ())
        for job in (running, queued):
            snapshot = self.call("get", job.job_id)["job"]
            self.assertEqual(snapshot["status"], "cancelled")
            self.assertEqual(self.call("cancel", job.job_id), {"job": snapshot})
            self.assertIsNone(snapshot["error"])
            self.assertEqual(set(snapshot), set(listed[0]))

    def test_elapsed_excludes_queue_time_and_freezes_on_completion(self):
        first_entered = threading.Event()
        first_release = threading.Event()
        second_entered = threading.Event()
        second_release = threading.Event()
        self.addCleanup(first_release.set)
        self.addCleanup(second_release.set)

        def first(job):
            first_entered.set()
            first_release.wait(5)

        def second(job):
            second_entered.set()
            second_release.wait(5)

        with patch("agent_core.jobs.monotonic", return_value=10) as now:
            self.jobs.submit("shell", "turn", first, details={})
            self.assertTrue(first_entered.wait(2))
            handle = self.jobs.submit("shell", "turn", second, details={})
            now.return_value = 100
            self.assertEqual(self.jobs.get(handle.job_id).elapsed_seconds, 0)
            first_release.set()
            self.assertTrue(second_entered.wait(2))
            now.return_value = 105
            self.assertEqual(self.jobs.get(handle.job_id).elapsed_seconds, 5)
            second_release.set()
            self.jobs.wait_for_turn("turn")
            now.return_value = 200
            self.assertEqual(self.jobs.get(handle.job_id).elapsed_seconds, 5)
        self.jobs.drain_updates("turn")
        self.assertEqual(self.jobs.get(handle.job_id).elapsed_seconds, 5)

    def test_failure_and_terminal_snapshot_survive_consumption_and_replay(self):
        def fail(job):
            raise RuntimeError("execution failed")

        failed = self.jobs.submit("shell", "turn", fail, details={"command": "bad"})
        completed = self.jobs.submit(
            "shell", "turn", lambda job: JobResult("full output", {"exit_code": 7}),
            details={"command": "exit 7"},
        )
        self.jobs.wait_for_turn("turn")
        before = self.jobs.get(completed.job_id)
        self.jobs.drain_terminal_state("turn")
        self.assertEqual(self.jobs.get(completed.job_id), before)
        self.assertEqual(self.jobs.get(failed.job_id).error, "execution failed")
        self.assertEqual(self.jobs.get(failed.job_id).status, "failed")
        self.assertNotIn("full output", json.dumps(before.to_dict()))
        self.assertEqual(self.jobs.list(), ())
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlSessionStore(Path(directory), group_by_workspace=False)
            store.append_events(self.session.session_id, self.session.journal)
            recovered = JobManager(store.load("parent"))
            try:
                self.assertEqual(recovered.get(completed.job_id), before)
                self.assertEqual(recovered.get(failed.job_id).error, "execution failed")
                self.assertEqual(recovered.cancel(completed.job_id), before)
                self.assertEqual(recovered.list(), ())
            finally:
                recovered.close()

    def test_recovered_interrupted_is_queryable_but_inactive(self):
        self.session.job_submitted("running", "subagent", "turn", details={"task": "investigate"})
        self.session.job_started("running", "subagent", "turn")
        progress = {"recent_tools": [{"name": "read_file", "status": "running"}], "text": "reading"}
        self.session.job_progressed("running", "turn", progress)
        self.session.job_submitted("queued", "shell", "turn", details={"command": "pwd"})
        self.session.recover()
        for job_id, duration in (("running", None), ("queued", 0)):
            snapshot = self.call("get", job_id)["job"]
            self.assertEqual(snapshot["status"], "interrupted")
            self.assertEqual(snapshot["elapsed_seconds"], duration)
            self.assertEqual(self.call("cancel", job_id), {"job": snapshot})
        self.assertEqual(self.jobs.get("running").progress, progress)
        self.assertEqual(self.jobs.list(), ())

    def test_normalization_failure_keeps_execution_summary(self):
        with patch.object(self.jobs, "_normalize_output", side_effect=OSError("artifact failed")):
            handle = self.jobs.submit(
                "shell", "turn", lambda job: JobResult("output", {"exit_code": 7}),
                details={"command": "exit 7"},
            )
            self.jobs.wait_for_turn("turn")
        snapshot = self.jobs.get(handle.job_id)
        self.assertEqual(snapshot.status, "failed")
        self.assertEqual(snapshot.error, "artifact failed")
        self.assertEqual(snapshot.result_summary, {"exit_code": 7})

    def test_shell_summary_preserves_exit_and_timeout_without_output(self):
        for exit_code, timed_out in ((0, False), (7, False), (-1, True)):
            with self.subTest(exit_code=exit_code, timed_out=timed_out):
                class Executor:
                    def execute(self, command, timeout_seconds, cancellation=None):
                        return CommandExecutionResult(
                            command, exit_code, "stdout-secret" * 10000, "stderr-secret",
                            timed_out=timed_out, timeout_seconds=timeout_seconds,
                        )

                with tempfile.TemporaryDirectory() as directory:
                    self.jobs.set_result_normalizer(ToolResultNormalizer(
                        self.context.workspace, self.session.session_id,
                        sessions_directory=Path(directory),
                    ))
                    router = ExecutionRouter(Executor(), Executor())
                    arguments = {"command": "command", "background": True, "timeout_seconds": 1}
                    call = ToolCall("shell-call", "shell", arguments)
                    context = replace(self.context, execution=router.resolve(call))
                    handle = ShellTool().execute(arguments, context)
                    self.jobs.wait_for_turn("turn")
                    snapshot = self.call("get", handle["job_id"])["job"]
                    self.assertEqual(snapshot["status"], "completed")
                    self.assertIsNone(snapshot["error"])
                    self.assertIsNone(snapshot["progress"])
                    self.assertEqual(snapshot["result_summary"], {
                        "exit_code": exit_code, "timed_out": timed_out, "timeout_seconds": 1,
                    })
                    self.assertNotIn("secret", json.dumps(snapshot))
                    self.jobs.drain_updates("turn")
                    self.assertEqual(self.call("get", handle["job_id"])["job"], snapshot)

    def test_foreground_shell_creates_no_job(self):
        class Executor:
            def execute(self, command, timeout_seconds):
                return CommandExecutionResult(command, 0, "done", "")

        arguments = {"command": "pwd"}
        router = ExecutionRouter(Executor(), Executor())
        context = replace(self.context, execution=router.resolve(ToolCall("call", "shell", arguments)))
        result = ShellTool().execute(arguments, context)
        self.assertEqual(result["stdout"], "done")
        self.assertEqual(self.session.jobs, {})

    def test_progress_and_details_are_detached_snapshots(self):
        details = {"task": "original"}
        progress = {"recent_tools": [{"name": "read_file"}], "text": "reading"}

        def run(job):
            job.report_progress(progress)
            progress["recent_tools"].clear()
            return "report"

        handle = self.jobs.submit("subagent", "turn", run, details=details)
        details["task"] = "changed"
        self.jobs.wait_for_turn("turn")
        snapshot = self.jobs.get(handle.job_id)
        snapshot.progress["recent_tools"].clear()
        snapshot.details["task"] = "changed again"
        self.assertEqual(self.jobs.get(handle.job_id).details, {"task": "original"})
        self.assertEqual(self.jobs.get(handle.job_id).progress["recent_tools"], [{"name": "read_file"}])

    def test_validation_and_unavailable_child_context(self):
        for arguments in (
            {}, {"action": "wait"}, {"action": []}, {"action": "get"}, {"action": "list"},
            {"action": "cancel", "job_id": ""}, {"action": "get", "job_id": 42},
            {"action": "list", "job_id": "unused"}, {"action": "list", "extra": True},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.tool.execute(arguments, self.context)
        for action in ("get", "cancel"):
            with self.assertRaisesRegex(ValueError, "unknown job_id"):
                self.call(action, "missing")
        catalog = builtin_catalog()
        schema = self.tool.definition(self.context).parameters
        self.assertEqual(schema["properties"]["action"]["enum"], ["get", "cancel"])
        self.assertEqual(schema["required"], ["action", "job_id"])
        self.assertEqual(len(catalog.select(("job_control",), self.context).definitions), 1)
        child = replace(self.context, jobs=None)
        self.assertEqual(catalog.select(("job_control",), child).definitions, ())


if __name__ == "__main__":
    unittest.main()

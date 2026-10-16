import json
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timezone

from agent_core import (
    Agent,
    AgentCancelled,
    AgentConfig,
    CommandExecutionResult,
    CommandCancelled,
    ExecutionRouter,
    JobManager,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Session,
    ShellTool,
    ToolCall,
    ToolCatalog,
    ToolConfig,
    ToolExecutionContext,
    TurnControl,
    Workspace,
    PlanManager,
    PlanStep,
)
from agent_core.agent import _apply_job_results
from agent_core.context_manager import ContextManager


CONFIG = AgentConfig(
    max_same_tool_calls=5,
    output_reserve_tokens=100,
    tools=ToolConfig(enabled=()),
    workspace_instruction_files=(),
)
CONSOLIDATOR_PROMPT = "Consolidate the conversation."


class SequencedProvider(LLMProvider):
    def __init__(self, responses) -> None:
        self._responses = iter(responses)
        self.requests: list[LLMRequest] = []
        self.second_request = threading.Event()

    @property
    def max_context_tokens(self) -> int:
        return 1000

    def count_input_tokens(self, request: LLMRequest) -> int:
        return 1

    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        self.requests.append(request)
        if len(self.requests) == 2:
            self.second_request.set()
        response = next(self._responses)
        if response.content:
            on_text_delta(response.content)
        return response


class BlockingExecutor:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, command, timeout_seconds=60, cancellation=None):
        self.entered.set()
        while not self.release.wait(0.01):
            if cancellation is not None:
                cancellation.raise_if_cancelled()
        return CommandExecutionResult(
            command=command,
            exit_code=0,
            stdout="background output",
            stderr="",
            timeout_seconds=timeout_seconds,
        )


class ImmediateExecutor:
    def execute(self, command, timeout_seconds=60, cancellation=None):
        return CommandExecutionResult(
            command=command,
            exit_code=0,
            stdout="done",
            stderr="",
            timeout_seconds=timeout_seconds,
        )


class BackgroundJobTest(unittest.TestCase):
    def test_active_jobs_refresh_at_tail_beside_plan_without_entering_history(self):
        session = Session("dynamic-jobs")
        session.begin_turn("turn")
        session.add_item("user", "Investigate")
        session.set_archived_summary("- [durable] Existing context.", 0)
        PlanManager(session).update(
            "Investigate", (PlanStep("inspect", "Inspect", "in_progress"),),
        )
        jobs = JobManager(session, max_workers=1)
        provider = SequencedProvider(())
        manager = ContextManager(
            provider, session, "system", CONSOLIDATOR_PROMPT, CONFIG, jobs=jobs,
        )
        context = ToolExecutionContext(
            workspace=Workspace(Path(__file__).parent), session=session, jobs=jobs,
        )
        first_entered = threading.Event()
        first_release = threading.Event()
        second_entered = threading.Event()
        second_release = threading.Event()

        def first(job):
            job.report_progress({"text": "private progress"})
            first_entered.set()
            first_release.wait(5)
            return "research report"

        def second(job):
            second_entered.set()
            second_release.wait(5)
            return "review report"

        try:
            initial = manager.build_request()
            self.assertNotIn("[Active Jobs]", str(initial))
            researcher = jobs.submit(
                "subagent", "turn", first,
                details={"role": "researcher", "task": "private task"},
            )
            self.assertTrue(first_entered.wait(2))
            reviewer = jobs.submit(
                "subagent", "turn", second, details={"role": "reviewer", "task": "review"},
            )
            shell = jobs.submit(
                "shell", "turn", lambda job: "command output", details={"command": "private command"},
            )
            pending = manager.build_request()
            self.assertEqual(pending.system_prompt, initial.system_prompt)
            self.assertEqual(pending.messages[:-1], initial.messages[:-1])
            self.assertEqual(pending.messages[-1].role, "system")
            plan, active = pending.messages[-1].content.split("\n\n[Active Jobs]\n")
            self.assertEqual(plan, initial.messages[-1].content)
            self.assertEqual(active.splitlines(), [
                f"{researcher.job_id}: subagent / researcher / running",
                f"{reviewer.job_id}: subagent / reviewer / submitted",
                f"{shell.job_id}: shell / submitted",
            ])
            self.assertNotIn("private", active)

            first_release.set()
            self.assertTrue(second_entered.wait(2))
            _apply_job_results(session, context, "turn", lambda: datetime.now(timezone.utc))
            advanced = manager.build_request()
            active = advanced.messages[-1].content
            self.assertNotIn(researcher.job_id, active)
            self.assertIn(f"{reviewer.job_id}: subagent / reviewer / running", active)
            self.assertIn("research report", str(advanced.messages[:-1]))
            self.assertEqual(advanced.messages[0], initial.messages[0])
            self.assertIn(f"{researcher.job_id}: subagent / researcher / running",
                          pending.messages[-1].content)

            second_release.set()
            jobs.wait_for_turn("turn")
            _apply_job_results(session, context, "turn", lambda: datetime.now(timezone.utc))
            finished = manager.build_request()
            self.assertNotIn("[Active Jobs]", str(finished))
            self.assertEqual(finished.messages[-1], initial.messages[-1])
            self.assertIn("review report", str(finished))
            self.assertIn("command output", str(finished))
            self.assertNotIn("[Active Jobs]", str(session.items))
            self.assertNotIn("[Active Jobs]", str(session.journal))
        finally:
            first_release.set()
            second_release.set()
            jobs.close()

    def test_turn_control_cancels_foreground_shell_batches_without_leaking(self) -> None:
        for count in (1, 2):
            with self.subTest(count=count):
                barrier = threading.Barrier(count + 1, timeout=3)
                release = threading.Event()
                signals = []

                class ConcurrentShell(ShellTool):
                    concurrent = True

                class Executor:
                    def execute(self, command, timeout_seconds=60, cancellation=None):
                        signals.append(cancellation)
                        if not release.is_set():
                            barrier.wait()
                        while not release.wait(0.01):
                            if cancellation is not None and cancellation.cancelled:
                                raise CommandCancelled
                        return CommandExecutionResult(command, 0, "done", "")

                calls = tuple(ToolCall(f"call-{i}", "shell", {"command": "slow"})
                              for i in range(count))
                provider = SequencedProvider([
                    LLMResponse(None, tool_calls=calls),
                    LLMResponse(None, tool_calls=calls),
                    LLMResponse("done"),
                ])
                session = Session("foreground")
                executor = Executor()
                context = ToolExecutionContext(
                    workspace=Workspace(Path(__file__).parent), session=session,
                    execution_router=ExecutionRouter(executor, executor),
                )
                agent = Agent(provider, session, "system", CONSOLIDATOR_PROMPT, CONFIG,
                              ToolCatalog((ConcurrentShell(),)).select(("shell",), context), context)
                control = TurnControl()
                errors = []

                def run():
                    try:
                        agent.run("run", turn_id="first", turn_control=control)
                    except BaseException as error:
                        errors.append(error)

                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                try:
                    barrier.wait()
                    control.cancel()
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], AgentCancelled)
                    self.assertEqual([item.status for item in session.tool_executions.values()],
                                     ["cancelled"] * count)
                    self.assertEqual(session.turns["first"].status, "cancelled")
                    self.assertIsNone(session.turns["first"].error)
                    self.assertIsNone(context.cancellation)
                finally:
                    release.set()
                    thread.join(3)

                next_control = TurnControl()
                agent.run("again", turn_id="second", turn_control=next_control)
                self.assertEqual(signals, [control] * count + [next_control] * count)
                self.assertEqual(session.turns["second"].status, "completed")

    def test_background_shell_does_not_block_the_next_model_call(self) -> None:
        call = ToolCall(
            "call-1",
            "shell",
            {"command": "slow", "background": True},
        )
        provider = SequencedProvider(
            (
                LLMResponse(content=None, tool_calls=(call,)),
                LLMResponse(content="I will continue while it runs."),
                LLMResponse(content="The background command finished."),
            )
        )
        session = Session("session-1")
        jobs = JobManager(session, max_workers=1)
        executor = BlockingExecutor()
        context = ToolExecutionContext(
            workspace=Workspace(Path(__file__).parent),
            session=session,
            execution_router=ExecutionRouter(executor, executor),
            jobs=jobs,
        )
        tools = ToolCatalog((ShellTool(),)).select(("shell",), context)
        agent = Agent(provider, session, "system", CONSOLIDATOR_PROMPT, CONFIG, tools, context)
        errors = []

        def run() -> None:
            try:
                agent.run("start it", turn_id="turn-1")
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(executor.entered.wait(2))
        self.assertTrue(provider.second_request.wait(2))
        self.assertTrue(thread.is_alive())

        handle = next(
            item for item in session.items
            if item.role == "tool" and item.tool_call_id == "call-1"
        )
        self.assertIn('"status": "submitted"', str(handle.content))

        executor.release.set()
        thread.join(5)
        jobs.close()
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]

        self.assertEqual(len(provider.requests), 3)
        job_id = json.loads(handle.content)["output"]["job_id"]
        self.assertNotIn("[Active Jobs]", str(provider.requests[0]))
        active = provider.requests[1].messages[-1]
        self.assertEqual(active.role, "system")
        self.assertIn(f"[Active Jobs]\n{job_id}: shell / ", active.content)
        self.assertNotIn("[Active Jobs]", str(provider.requests[2]))
        self.assertIn("background output", str(provider.requests[2].messages))
        job_messages = [
            item for item in session.items if item.origin == "job_result"
        ]
        self.assertEqual(len(job_messages), 1)
        self.assertIn("background output", str(job_messages[0].content))
        self.assertFalse(job_messages[0].is_user_authored)
        self.assertEqual(
            [job.status for job in session.jobs.values()], ["completed"]
        )

    def test_recovery_marks_unfinished_jobs_interrupted(self) -> None:
        session = Session("session-1")
        session.begin_turn("turn-1")
        session.job_submitted("job-1", "shell", "turn-1", details={"command": "slow"})
        session.job_started("job-1", "shell", "turn-1")

        session.recover()

        self.assertEqual(session.jobs["job-1"].status, "interrupted")
        self.assertEqual(session.journal[-1].event_type, "job_interrupted")

    def test_background_shell_schema_and_handle(self) -> None:
        session = Session("session-1")
        session.begin_turn("turn-1")
        jobs = JobManager(session, max_workers=1)
        context = ToolExecutionContext(
            workspace=Workspace(Path(__file__).parent),
            session=session,
            execution_router=ExecutionRouter(
                ImmediateExecutor(), ImmediateExecutor()
            ),
            jobs=jobs,
        )
        tool = ShellTool()
        arguments = {
            "command": "true",
            "background": True,
            "timeout_seconds": 86400,
        }
        call = ToolCall("call-1", "shell", arguments)
        result = tool.execute(
            arguments,
            replace(context, execution=context.execution_router.resolve(call)),
        )
        jobs.wait_for_turn("turn-1")

        self.assertIn("background", tool.definition(context).parameters["properties"])
        self.assertEqual(result["kind"], "shell")
        self.assertEqual(result["status"], "submitted")
        self.assertTrue(str(result["job_id"]).startswith("job_"))
        updates = jobs.drain_updates("turn-1")
        self.assertEqual([update.status for update in updates], ["submitted", "running", "completed"])
        self.assertTrue(all(update.details == {"command": "true"} for update in updates))
        jobs.close()

    def test_queued_job_handle_reports_submitted(self) -> None:
        session = Session("session-1")
        session.begin_turn("turn-1")
        jobs = JobManager(session, max_workers=1)
        entered = threading.Event()
        release = threading.Event()

        first = jobs.submit(
            "shell",
            "turn-1",
            lambda cancellation: entered.set() or release.wait(2),
            details={"command": "first"},
        )
        self.assertTrue(entered.wait(1))
        second = jobs.submit("shell", "turn-1", lambda cancellation: None,
                             details={"command": "second"})

        self.assertEqual(first.status, "submitted")
        self.assertEqual(second.status, "submitted")
        self.assertEqual(session.jobs[second.job_id].status, "submitted")
        updates = jobs.drain_updates("turn-1")
        self.assertEqual(
            [(update.job_id, update.details) for update in updates],
            [(first.job_id, {"command": "first"}),
             (first.job_id, {"command": "first"}),
             (second.job_id, {"command": "second"})],
        )
        release.set()
        jobs.wait_for_turn("turn-1")
        jobs.close()

    def test_unormalized_dict_result_is_injected_as_json(self) -> None:
        from agent_core.agent import _apply_job_results

        session = Session("session-1")
        session.begin_turn("turn-1")
        jobs = JobManager(session, max_workers=1)
        jobs.submit("test", "turn-1", lambda cancellation: {"a": 1}, details={})
        jobs.wait_for_turn("turn-1")
        context = ToolExecutionContext(
            workspace=Workspace(Path(__file__).parent),
            session=session,
            jobs=jobs,
        )

        applied, pending = _apply_job_results(
            session,
            context,
            "turn-1",
            lambda: datetime.now(timezone.utc),
        )

        self.assertTrue(applied)
        self.assertFalse(pending)
        self.assertIn('result: {"a": 1}', str(session.items[-1].content))
        jobs.close()

    def test_cancelled_turn_cancels_background_shell(self) -> None:
        call = ToolCall(
            "call-1",
            "shell",
            {"command": "slow", "background": True},
        )
        provider = SequencedProvider(
            (
                LLMResponse(content=None, tool_calls=(call,)),
                LLMResponse(content="waiting"),
            )
        )
        session = Session("session-1")
        jobs = JobManager(session, max_workers=1)
        executor = BlockingExecutor()
        context = ToolExecutionContext(
            workspace=Workspace(Path(__file__).parent),
            session=session,
            execution_router=ExecutionRouter(executor, executor),
            jobs=jobs,
        )
        agent = Agent(
            provider,
            session,
            "system",
            CONSOLIDATOR_PROMPT,
            CONFIG,
            ToolCatalog((ShellTool(),)).select(("shell",), context),
            context,
        )
        control = TurnControl()
        errors = []

        def run() -> None:
            try:
                agent.run("start it", turn_id="turn-1", turn_control=control)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(provider.second_request.wait(2))
        control.cancel()
        thread.join(5)
        jobs.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(type(errors[0]).__name__, "AgentCancelled")
        self.assertEqual(
            [job.status for job in session.jobs.values()], ["cancelled"]
        )


if __name__ == "__main__":
    unittest.main()

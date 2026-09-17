import threading
import unittest
from pathlib import Path

from agent_core import (
    Agent,
    AgentConfig,
    CommandExecutionResult,
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
)


CONFIG = AgentConfig(
    max_same_tool_calls=5,
    output_reserve_tokens=100,
    tools=ToolConfig(enabled=()),
)


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
            command_executor=executor,
            jobs=jobs,
        )
        tools = ToolCatalog((ShellTool(),)).select(("shell",), context)
        agent = Agent(provider, session, "system", CONFIG, tools, context)
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
        self.assertIn('"status": "running"', str(handle.content))

        executor.release.set()
        thread.join(5)
        jobs.close()
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]

        self.assertEqual(len(provider.requests), 3)
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
        session.job_submitted("job-1", "shell", "turn-1")
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
            command_executor=ImmediateExecutor(),
            jobs=jobs,
        )
        tool = ShellTool()

        result = tool.execute(
            {"command": "true", "background": True}, context
        )
        jobs.wait_for_turn("turn-1")

        self.assertIn("background", tool.definition(context).parameters["properties"])
        self.assertEqual(result["kind"], "shell")
        self.assertEqual(result["status"], "running")
        self.assertTrue(str(result["job_id"]).startswith("job_"))
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
            command_executor=executor,
            jobs=jobs,
        )
        agent = Agent(
            provider,
            session,
            "system",
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

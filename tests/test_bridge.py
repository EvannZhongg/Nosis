import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from unittest.mock import patch

from agent_core import (
    AssistantMessageDeltaEvent,
    ProviderCapabilities,
    AssistantMessageEvent,
    ContextWindow,
    ContextWindowEvent,
    JsonlSessionStore,
    JobHandle,
    JobStatusEvent,
    Message,
    PermissionPreset,
    ReasoningDeltaEvent,
    Session,
    ToolBatchStartedEvent,
    ToolCall,
    ToolCallEvent,
    ToolError,
    ToolResult,
    ToolResultEvent,
    ToolExecutionContext,
    UserSteerAppliedEvent,
    TurnControl,
    Workspace,
)
from agent_core.llm import TokenUsage
from agent_core.projection import project_context_units
from agent_core.providers import LiteLLMProvider
from agent_core.subagent import vision_aware_tool_names
from agent_core.tools.config import TOOL_NAMES
from interfaces.bridge.bridge import Bridge, Cancelled
from interfaces.bridge.protocol import (
    attachment_replaced_message,
    decode,
    event_to_message,
    format_timestamp,
    session_ready_message,
    runtime_state_message,
    usage_to_dict,
)

TOOL_CALL = ToolCall(id="call-1", name="shell", arguments={"command": "ls"})
SECOND_TOOL_CALL = ToolCall(id="call-2", name="shell", arguments={"command": "pwd"})
EVENT_TIME = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)


class ProtocolTest(unittest.TestCase):
    def test_encodes_assistant_delta(self) -> None:
        self.assertEqual(
            event_to_message(
                AssistantMessageDeltaEvent(text="hi", model_call_index=1),
                "t1",
            ),
            {
                "type": "assistant_delta",
                "turn_id": "t1",
                "text": "hi",
                "model_call_index": 1,
            },
        )

    def test_encodes_reasoning_delta(self) -> None:
        self.assertEqual(
            event_to_message(
                ReasoningDeltaEvent(text="thinking", model_call_index=1),
                "t1",
            ),
            {
                "type": "reasoning_delta",
                "turn_id": "t1",
                "text": "thinking",
                "model_call_index": 1,
            },
        )

    def test_encodes_context_window(self) -> None:
        self.assertEqual(
            event_to_message(
                ContextWindowEvent(
                    ContextWindow(
                        input_tokens=120,
                        max_input_tokens=900,
                        max_context_tokens=1000,
                        output_reserve_tokens=100,
                        compression_threshold=720,
                        compression_count=2,
                    )
                ),
                "t1",
            ),
            {
                "type": "context_window",
                "turn_id": "t1",
                "input_tokens": 120,
                "max_input_tokens": 900,
                "max_context_tokens": 1000,
                "output_reserve_tokens": 100,
                "compression_threshold": 720,
                "compression_count": 2,
            },
        )

    def test_encodes_assistant_message_with_utc_timestamp(self) -> None:
        message = event_to_message(
            AssistantMessageEvent(
                content="done",
                timestamp_utc=EVENT_TIME,
                model_call_index=2,
            ),
            "t1",
        )
        self.assertEqual(message["type"], "assistant_message")
        self.assertEqual(message["content"], "done")
        self.assertTrue(str(message["timestamp_utc"]).endswith("Z"))

    def test_encodes_tool_call_events(self) -> None:
        self.assertEqual(
            event_to_message(
                ToolBatchStartedEvent(
                    model_call_index=1,
                    tool_calls=(TOOL_CALL,),
                ),
                "t1",
            )["tool_calls"],
            [{"id": "call-1", "name": "shell", "arguments": {"command": "ls"}}],
        )
        self.assertEqual(
            event_to_message(
                ToolCallEvent(
                    tool_call=TOOL_CALL,
                    tool_index=1,
                    tool_count=2,
                ),
                "t1",
            )["tool_count"],
            2,
        )

    def test_tool_result_reports_status_without_output(self) -> None:
        message = event_to_message(
            ToolResultEvent(
                tool_result=ToolResult(
                    tool_call_id="call-1",
                    name="shell",
                    output={"stdout": "x" * 5000},
                ),
                tool_index=1,
                tool_count=1,
            ),
            "t1",
        )
        self.assertTrue(message["ok"])
        self.assertIsNone(message["error"])
        # Output can be large and is offloaded to session artifacts.
        self.assertNotIn("output", message)

    def test_encodes_applied_user_steering(self) -> None:
        message = event_to_message(
            UserSteerAppliedEvent(
                steer_id="s1",
                text="check tests first",
                timestamp_utc=EVENT_TIME,
            ),
            "t1",
        )
        self.assertEqual(message["type"], "user_steer_applied")
        self.assertEqual(message["steer_id"], "s1")
        self.assertEqual(message["text"], "check tests first")

    def test_encodes_background_job_status(self) -> None:
        self.assertEqual(
            event_to_message(
                JobStatusEvent("job-1", "shell", "running"), "t1"
            ),
            {
                "type": "job_status",
                "turn_id": "t1",
                "job_id": "job-1",
                "kind": "shell",
                "status": "running",
            },
        )

    def test_tool_result_reports_error(self) -> None:
        message = event_to_message(
            ToolResultEvent(
                tool_result=ToolResult(
                    tool_call_id="call-1",
                    name="shell",
                    error=ToolError(type="PermissionError", message="denied"),
                ),
                tool_index=1,
                tool_count=1,
            ),
            "t1",
        )
        self.assertFalse(message["ok"])
        self.assertEqual(
            message["error"],
            {"type": "PermissionError", "message": "denied"},
        )

    def test_usage_to_dict(self) -> None:
        self.assertIsNone(usage_to_dict(None))
        self.assertEqual(
            usage_to_dict(
                TokenUsage(input_tokens=1, output_tokens=2, total_tokens=3)
            ),
            {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )

    def test_session_ready_message_describes_the_control_plane(self) -> None:
        self.assertEqual(
            session_ready_message(
                session_id="s1",
                workspace="/tmp/work",
                provider="test",
                model="test/model",
                resumed=False,
                message_count=0,
                permission_preset="ask_for_approval",
            ),
            {
                "type": "session_ready",
                "session_id": "s1",
                "workspace": "/tmp/work",
                "provider": "test",
                "model": "test/model",
                "resumed": False,
                "message_count": 0,
                "permission_preset": "ask_for_approval",
            },
        )

    def test_runtime_state_message_restores_gui_attachment_state(self) -> None:
        approval = {
            "type": "approval_request",
            "turn_id": "t1",
            "request_id": "t1:1",
            "command": "ls",
        }
        self.assertEqual(
            runtime_state_message(
                phase="waiting_approval",
                turn_id="t1",
                approval=approval,
                question=None,
                provider="second",
                permission_preset="full_access",
                context_window={"input_tokens": 120},
                event_sequence=12,
                jobs=[{"job_id": "j1", "kind": "shell", "status": "running"}],
            ),
            {
                "type": "runtime_state",
                "phase": "waiting_approval",
                "turn_id": "t1",
                "approval": approval,
                "question": None,
                "provider": "second",
                "permission_preset": "full_access",
                "context_window": {"input_tokens": 120},
                "event_sequence": 12,
                "jobs": [{"job_id": "j1", "kind": "shell", "status": "running"}],
                "skill_warnings": [],
            },
        )

    def test_attachment_replaced_message_preserves_runtime_state(self) -> None:
        self.assertEqual(
            attachment_replaced_message(phase="running"),
            {"type": "attachment_replaced", "phase": "running"},
        )

    def test_format_timestamp_normalizes_to_utc(self) -> None:
        self.assertEqual(
            format_timestamp(EVENT_TIME),
            "2026-09-09T08:00:00.000000Z",
        )

    def test_decode_rejects_non_object_and_untyped_messages(self) -> None:
        with self.assertRaises(ValueError):
            decode("[1, 2]")
        with self.assertRaises(ValueError):
            decode('{"text": "hello"}')


def make_bridge(lines: list[str]) -> tuple[Bridge, io.StringIO]:
    stdin = io.StringIO("".join(f"{line}\n" for line in lines))
    stdout = io.StringIO()
    return Bridge(stdin, stdout), stdout


def emitted(stdout: io.StringIO) -> list[dict]:
    return [
        json.loads(line)
        for line in stdout.getvalue().splitlines()
        if line.strip()
    ]


class PermissionProtocolTest(unittest.TestCase):
    def test_bridge_updates_permission_before_runtime_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, stdout = make_bridge([])
            bridge.open_session(open_session_message(root, session_id="s"))

            bridge._set_permission_preset(
                {"type": "permission_set", "preset": "full_access"}
            )

            self.assertIsNone(bridge._agent)
            self.assertIsNone(bridge._mcp)
            assert bridge._session is not None
            self.assertEqual(
                bridge._session.permission_preset,
                PermissionPreset.FULL_ACCESS,
            )
            self.assertEqual(
                JsonlSessionStore(root / "sessions").permission_preset_for("s"),
                PermissionPreset.FULL_ACCESS,
            )
            self.assertEqual(
                bridge._session.journal[-1].event_type,
                "permission_preset_changed",
            )
            self.assertEqual(JsonlSessionStore(root / "sessions").list_sessions(), [])
            self.assertEqual(
                emitted(stdout)[-1],
                {"type": "permission_changed", "preset": "full_access"},
            )

    def test_bridge_changes_session_configuration_without_initializing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            next_workspace = root / "next"
            next_workspace.mkdir()
            bridge, stdout = make_bridge([])
            bridge.open_session(open_session_message(root, session_id="s"))

            bridge._set_provider({"type": "provider_set", "provider": "second"})
            bridge._set_workspace(
                {"type": "workspace_set", "workspace": str(next_workspace)}
            )
            bridge._set_permission_preset(
                {"type": "permission_set", "preset": "full_access"}
            )

            self.assertIsNone(bridge._agent)
            self.assertIsNone(bridge._mcp)
            self.assertEqual(
                JsonlSessionStore(root / "sessions").provider_for("s"),
                "second",
            )
            self.assertEqual(
                bridge._workspace.path,
                next_workspace.resolve(),
            )
            self.assertEqual(
                JsonlSessionStore(root / "sessions").permission_preset_for("s"),
                PermissionPreset.FULL_ACCESS,
            )
            self.assertEqual(
                [message["type"] for message in emitted(stdout)[-5:]],
                [
                    "provider_changed",
                    "runtime_state",
                    "workspace_changed",
                    "runtime_state",
                    "permission_changed",
                ],
            )


class _SlowStdin:
    """A stdin whose readline yields the GIL mid-call.

    A real pipe blocks in readline while other threads run, which is what
    lets two unsynchronized approvals interleave. StringIO returns without
    ever yielding and would hide the race.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = iter(f"{line}\n" for line in lines)

    def readline(self) -> str:
        time.sleep(0.01)
        return next(self._lines, "")


class _BlockingAfterLines:
    def __init__(self, lines: list[str]) -> None:
        self._lines = iter(f"{line}\n" for line in lines)
        self.release = threading.Event()

    def readline(self) -> str:
        try:
            return next(self._lines)
        except StopIteration:
            self.release.wait(5)
            return ""


class BridgeApprovalTest(unittest.TestCase):
    def test_runtime_state_contains_active_jobs(self) -> None:
        class Jobs:
            @staticmethod
            def snapshot():
                return (JobHandle("j1", "shell", "running"),)

        with tempfile.TemporaryDirectory() as directory:
            bridge, stdout = make_bridge([])
            bridge.open_session(
                open_session_message(Path(directory), session_id="s")
            )
            bridge._jobs = Jobs()

            bridge._emit_runtime_state("running")

            self.assertEqual(
                emitted(stdout)[-1]["jobs"],
                [{"job_id": "j1", "kind": "shell", "status": "running"}],
            )

    def test_runtime_state_contains_the_pending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, stdout = make_bridge(
                ['{"type": "approval_response", "request_id": "t1:1",'
                 ' "approved": true}']
            )
            bridge.open_session(
                open_session_message(Path(directory), session_id="s")
            )
            bridge._turn_id = "t1"

            self.assertTrue(bridge.request_permission("ls"))

            states = [
                message for message in emitted(stdout)
                if message["type"] == "runtime_state"
            ]
            self.assertEqual(states[-2]["phase"], "waiting_approval")
            self.assertEqual(states[-2]["approval"]["command"], "ls")
            self.assertIsNone(states[-2]["question"])
            self.assertIsNone(states[-1]["approval"])
            self.assertEqual(states[-1]["phase"], "running")

    def test_approves_matching_request(self) -> None:
        bridge, stdout = make_bridge(
            ['{"type": "approval_response", "request_id": "None:1",'
             ' "approved": true}']
        )
        self.assertTrue(bridge.request_permission("ls"))
        self.assertEqual(
            next(message for message in emitted(stdout) if message["type"] == "approval_request")["command"],
            "ls",
        )

    def test_denies_when_response_is_false(self) -> None:
        bridge, _ = make_bridge(
            ['{"type": "approval_response", "request_id": "None:1",'
             ' "approved": false}']
        )
        self.assertFalse(bridge.request_permission("rm -rf /"))

    def test_buffers_interleaved_messages_and_replays_them(self) -> None:
        """A typed-ahead turn must not be consumed as the answer."""
        bridge, _ = make_bridge(
            [
                '{"type": "user_turn", "turn_id": "t2", "text": "later"}',
                '{"type": "approval_response", "request_id": "None:1",'
                ' "approved": true}',
            ]
        )
        self.assertTrue(bridge.request_permission("ls"))

        deferred = bridge.read_message()
        assert deferred is not None
        self.assertEqual(deferred["type"], "user_turn")
        self.assertEqual(deferred["turn_id"], "t2")
        self.assertIsNone(bridge.read_message())

    def test_ignores_stale_request_id(self) -> None:
        bridge, _ = make_bridge(
            [
                '{"type": "approval_response", "request_id": "stale",'
                ' "approved": true}',
                '{"type": "approval_response", "request_id": "None:1",'
                ' "approved": false}',
            ]
        )
        self.assertFalse(bridge.request_permission("ls"))

    def test_cancels_when_input_ends(self) -> None:
        bridge, _ = make_bridge([])
        with self.assertRaises(Cancelled):
            bridge.request_permission("ls")

    def test_cancels_on_shutdown(self) -> None:
        bridge, _ = make_bridge(['{"type": "shutdown"}'])
        with self.assertRaises(Cancelled):
            bridge.request_permission("ls")

    def test_serializes_approvals_from_parallel_tool_calls(self) -> None:
        """Concurrent sub-agents share one protocol channel.

        Without serialization two threads interleave their read-modify-write
        on stdin: each can consume the other's response, so a request never
        sees its answer and both wait forever.
        """
        approvals = 4
        responses = [
            '{"type": "approval_response", "request_id": "None:%d",'
            ' "approved": true}' % index
            for index in range(1, approvals + 1)
        ]
        stdout = io.StringIO()
        bridge = Bridge(_SlowStdin(responses), stdout)
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(approvals, timeout=5)

        def approve(index: int) -> None:
            barrier.wait()
            approved = bridge.request_permission(f"command {index}")
            with lock:
                results.append(approved)

        threads = [
            threading.Thread(target=approve, args=(index,), daemon=True)
            for index in range(approvals)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            # An answer consumed by the wrong thread would hang here.
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "an approval never returned")

        self.assertEqual(results, [True] * approvals)
        # Each request got its own id, and each was answered exactly once.
        request_ids = [
            message["request_id"]
            for message in emitted(stdout)
            if message["type"] == "approval_request"
        ]
        self.assertEqual(len(request_ids), approvals)
        self.assertEqual(len(set(request_ids)), approvals)

    def test_routes_steering_while_waiting_for_approval(self) -> None:
        stdin = _BlockingAfterLines(
            [
                '{"type": "user_steer", "turn_id": "t1", "steer_id": "s1", "text": "check tests"}',
                '{"type": "approval_response", "request_id": "t1:1", "approved": true}',
            ]
        )
        stdout = io.StringIO()
        bridge = Bridge(stdin, stdout)
        control = TurnControl()
        bridge._turn_id = "t1"
        bridge._turn_control = control

        self.assertTrue(bridge.request_permission("ls"))
        self.assertEqual(control.drain_steering()[0].text, "check tests")
        self.assertIn(
            "user_steer_received",
            [message["type"] for message in emitted(stdout)],
        )
        bridge._turn_control = None
        stdin.release.set()

    def test_ignores_a_stale_response_that_arrived_before_its_waiter(self) -> None:
        bridge, _ = make_bridge(
            [
                '{"type": "approval_response", "request_id": "None:1", "approved": true}',
            ]
        )
        bridge._start_reader()
        time.sleep(0.02)

        with self.assertRaises(Cancelled):
            bridge.request_permission("ls")

    def test_routes_cancel_for_a_turn_queued_before_run_turn(self) -> None:
        bridge, _ = make_bridge(
            [
                '{"type": "user_turn", "turn_id": "t1", "text": "work"}',
                '{"type": "cancel", "turn_id": "t1"}',
            ]
        )

        message = bridge.read_message()
        assert message is not None
        time.sleep(0.02)

        self.assertIn("t1", bridge._pending_cancels)


class BridgeUserQuestionTest(unittest.TestCase):
    OPTIONS = [
        {"id": "memory", "label": "Memory"},
        {"id": "sqlite", "label": "SQLite"},
    ]

    def test_runtime_state_contains_the_pending_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, stdout = make_bridge(
                ['{"type": "user_question_response", "request_id": "t1:1",'
                 ' "option_id": "sqlite"}']
            )
            bridge.open_session(
                open_session_message(Path(directory), session_id="s")
            )
            bridge._turn_id = "t1"

            bridge.request_user_choice("Cache?", self.OPTIONS, False)

            states = [
                message for message in emitted(stdout)
                if message["type"] == "runtime_state"
            ]
            self.assertEqual(states[-2]["phase"], "waiting_user")
            self.assertEqual(states[-2]["question"]["question"], "Cache?")
            self.assertIsNone(states[-2]["approval"])
            self.assertIsNone(states[-1]["question"])
            self.assertEqual(states[-1]["phase"], "running")

    def test_returns_the_selected_option(self) -> None:
        bridge, stdout = make_bridge(
            ['{"type": "user_question_response", "request_id": "None:1",'
             ' "option_id": "sqlite"}']
        )

        self.assertEqual(
            bridge.request_user_choice("Cache?", self.OPTIONS, False),
            {"type": "option", "id": "sqlite", "label": "SQLite"},
        )
        self.assertEqual(
            next(message for message in emitted(stdout) if message["type"] == "user_question")["type"],
            "user_question",
        )

    def test_returns_trimmed_free_text(self) -> None:
        bridge, _ = make_bridge(
            ['{"type": "user_question_response", "request_id": "None:1",'
             ' "text": "  Redis  "}']
        )

        self.assertEqual(
            bridge.request_user_choice("Cache?", self.OPTIONS, True),
            {"type": "text", "text": "Redis"},
        )

    def test_ignores_invalid_answers(self) -> None:
        bridge, _ = make_bridge(
            [
                '{"type": "user_question_response", "request_id": "None:1",'
                ' "option_id": "unknown"}',
                '{"type": "user_question_response", "request_id": "None:1",'
                ' "option_id": "memory"}',
            ]
        )

        self.assertEqual(
            bridge.request_user_choice("Cache?", self.OPTIONS, False)["id"],
            "memory",
        )


class BridgeServeTest(unittest.TestCase):
    def test_stops_on_shutdown(self) -> None:
        bridge, _ = make_bridge(['{"type": "shutdown"}'])
        bridge.serve()

    def test_stops_at_end_of_input(self) -> None:
        bridge, _ = make_bridge([])
        bridge.serve()

    def test_does_not_swallow_an_external_keyboard_interrupt(self) -> None:
        bridge, _ = make_bridge([])
        with patch.object(bridge, "read_message", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                bridge.serve()

    def test_rejects_turn_before_open_session(self) -> None:
        bridge, _ = make_bridge(
            ['{"type": "user_turn", "turn_id": "t1", "text": "hi"}']
        )
        with self.assertRaises(SystemExit):
            bridge.serve()

    def test_reports_malformed_input_as_fatal(self) -> None:
        bridge, stdout = make_bridge(["not json"])
        with self.assertRaises(SystemExit):
            bridge.serve()
        self.assertEqual(emitted(stdout)[0]["type"], "fatal")

    def test_answers_the_workspace_sessions_and_the_stored_conversation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            session = Session("saved")
            session.begin_turn("t")
            session.add_item("user", "hello there")
            session.add_item("assistant", "hi")
            session.finish_turn("completed")
            store.bind_workspace(session.session_id, root)
            store.append_events(
                session.session_id, session.journal, workspace=root
            )

            bridge, stdout = make_bridge(
                [
                    json.dumps(open_session_message(root, session_id="saved")),
                    '{"type": "load_session"}',
                    '{"type": "list_sessions"}',
                    '{"type": "shutdown"}',
                ]
            )
            bridge.serve()

            messages = emitted(stdout)
            loaded = next(
                message
                for message in messages
                if message["type"] == "session_items"
            )
            self.assertEqual(
                [item["content"] for item in loaded["items"]],
                ["hello there", "hi"],
            )
            listed = next(
                message
                for message in messages
                if message["type"] == "sessions_listed"
            )
            self.assertEqual(
                listed["sessions"],
                [{"session_id": "saved", "title": "hello there"}],
            )

    def test_applies_a_permission_queued_immediately_after_open_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, stdout = make_bridge(
                [
                    json.dumps(open_session_message(root, session_id="empty")),
                    '{"type": "permission_set", "preset": "full_access"}',
                    '{"type": "shutdown"}',
                ]
            )

            bridge.serve()

            self.assertEqual(
                [message["type"] for message in emitted(stdout)],
                ["session_ready", "runtime_state", "permission_changed"],
            )
            self.assertEqual(
                JsonlSessionStore(root / "sessions").permission_preset_for(
                    "empty"
                ),
                PermissionPreset.FULL_ACCESS,
            )
            self.assertIsNone(bridge._agent)


def open_session_message(
    directory: Path,
    agent_config: dict | None = None,
    provider_config: dict | None = None,
    **extra: object,
) -> dict:
    provider_config_path = directory / "provider_config.json"
    provider_config_path.write_text(
        json.dumps(
            provider_config
            if provider_config is not None
            else {
                "main_agent": {"provider": "first"},
                "subagent": {"provider": ""},
                "providers": {
                    "first": {
                        "model": "openai/first",
                        "max_context_tokens": 1000,
                    },
                    "second": {
                        "model": "openai/second",
                        "max_context_tokens": 1000,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    agent_config_path = directory / "agent_config.json"
    agent_config_path.write_text(
        json.dumps(
            agent_config
            if agent_config is not None
            else {
                "max_same_tool_calls": 5,
                "output_reserve_tokens": 100,
                "main_agent": {"tools": {name: False for name in TOOL_NAMES}},
            }
        ),
        encoding="utf-8",
    )
    return {
        "type": "open_session",
        "workspace": str(directory),
        "session_id": None,
        "provider_config_path": str(provider_config_path),
        "agent_config_path": str(agent_config_path),
        **extra,
    }


class ScriptedAgent:
    """Stands in for the agent loop: it edits the session, then stops."""

    def __init__(
        self,
        session: Session,
        tail: tuple[Message, ...] = (),
        error: BaseException | None = None,
    ) -> None:
        self._session = session
        self._tail = tail
        self._error = error

    def run(
        self,
        user_input: str,
        on_event: object = None,
        attachments: object = (),
        turn_id: str | None = None,
        turn_control: TurnControl | None = None,
    ) -> None:
        self._session.begin_turn(turn_id)
        self._session.add_item("user", user_input)
        for item in self._tail:
            self._session.add_item(
                item.role, item.content, item.timestamp_utc, item.tool_calls,
                item.tool_call_id, item.reasoning, origin=item.origin,
            )
        if self._error is not None:
            if isinstance(self._error, Exception):
                self._session.finish_turn("failed", turn_id)
            raise self._error


class FailingAgent:
    """Stands in for a provider that fails before the agent loop runs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def run(
        self,
        user_input: str,
        on_event: object = None,
        attachments: object = (),
        turn_id: str | None = None,
        turn_control: TurnControl | None = None,
    ) -> None:
        # A provider can fail only after the Runtime has durably opened a turn.
        self._session.begin_turn(turn_id)
        self._session.finish_turn("failed", turn_id)
        raise ValueError("provider refused the request")


class InterruptedTurnTest(unittest.TestCase):
    """A turn keeps the transcript it produced before it ended early."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bridge, self.stdout = make_bridge([])
        self.bridge.open_session(open_session_message(self.root))
        self.session_id = next(
            message["session_id"]
            for message in emitted(self.stdout)
            if message["type"] == "session_ready"
        )
        self.store = JsonlSessionStore(self.root / "sessions")
        session = self.bridge._session
        assert session is not None
        self.session = session

    def start_turn(self, agent: object, text: str = "do the work") -> None:
        patcher = patch.object(self.bridge, "_agent", agent)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bridge.run_turn({"turn_id": "t1", "text": text})

    def stored_items(self) -> list[Message]:
        return self.store.load(self.session_id).items

    def test_stores_a_cancelled_turn(self) -> None:
        self.start_turn(
            ScriptedAgent(
                self.session,
                tail=(Message(role="assistant", content="half an answer"),),
                error=KeyboardInterrupt(),
            )
        )

        self.assertEqual(emitted(self.stdout)[-1]["type"], "turn_cancelled")
        self.assertEqual(
            [message.content for message in self.stored_items()],
            ["do the work", "half an answer"],
        )

    def test_stores_a_failed_turn(self) -> None:
        self.start_turn(
            ScriptedAgent(
                self.session,
                error=ValueError("provider refused the request"),
            )
        )

        failure = emitted(self.stdout)[-1]
        self.assertEqual(failure["type"], "turn_failed")
        self.assertEqual(failure["error"]["type"], "ValueError")
        self.assertEqual(
            [message.content for message in self.stored_items()],
            ["do the work"],
        )

    def test_journal_records_a_turn_that_produced_nothing(self) -> None:
        self.start_turn(FailingAgent(self.session))

        failure = emitted(self.stdout)[-1]
        self.assertEqual(failure["type"], "turn_failed")
        self.assertEqual(failure["error"]["type"], "ValueError")
        # The failed turn is a fact even though it produced no messages.
        self.assertEqual(self.stored_items(), [])
        self.assertTrue(self.store.has_journal(self.session_id))

    def test_projection_excludes_a_tool_call_whose_result_never_arrived(self) -> None:
        self.start_turn(
            ScriptedAgent(
                self.session,
                tail=(
                    Message(
                        role="assistant",
                        content=None,
                        tool_calls=(TOOL_CALL,),
                    ),
                ),
                error=KeyboardInterrupt(),
            )
        )

        self.assertEqual(
            [message.role for message in self.stored_items()],
            ["user", "assistant"],
        )
        # The next turn must not send the unanswered call to the provider.
        units = project_context_units(self.session.items)
        self.assertEqual([message.role for unit in units for message in unit.messages], ["user"])

    def test_keeps_a_tool_step_that_received_its_result(self) -> None:
        self.start_turn(
            ScriptedAgent(
                self.session,
                tail=(
                    Message(
                        role="assistant",
                        content=None,
                        tool_calls=(TOOL_CALL,),
                    ),
                    Message(
                        role="tool",
                        content='{"ok": true}',
                        tool_call_id=TOOL_CALL.id,
                    ),
                ),
                error=KeyboardInterrupt(),
            )
        )

        self.assertEqual(
            [message.role for message in self.stored_items()],
            ["user", "assistant", "tool"],
        )

    def test_journal_stores_items_as_each_item_settles(self) -> None:
        self.start_turn(
            ScriptedAgent(
                self.session,
                tail=(Message(role="assistant", content="half an answer"),),
                error=KeyboardInterrupt(),
            )
        )

        self.assertEqual(emitted(self.stdout)[-1]["type"], "turn_cancelled")
        self.assertEqual(
            [message.content for message in self.stored_items()],
            ["do the work", "half an answer"],
        )

    def test_projection_excludes_an_incomplete_tool_batch(self) -> None:
        self.start_turn(
            ScriptedAgent(
                self.session,
                tail=(
                    Message(
                        role="assistant",
                        content=None,
                        tool_calls=(TOOL_CALL, SECOND_TOOL_CALL),
                    ),
                    Message(
                        role="tool",
                        content='{"ok": true}',
                        tool_call_id=TOOL_CALL.id,
                    ),
                ),
                error=KeyboardInterrupt(),
            )
        )

        # Execution history keeps both facts; provider projection omits the
        # incomplete batch without rewriting the journal.
        self.assertEqual([message.role for message in self.stored_items()], ["user", "assistant", "tool"])
        units = project_context_units(self.session.items)
        self.assertEqual([message.role for unit in units for message in unit.messages], ["user"])

    def test_projection_restores_a_completed_tool_batch_once(self) -> None:
        self.start_turn(
            ScriptedAgent(
                self.session,
                tail=(
                    Message(
                        role="assistant",
                        content=None,
                        tool_calls=(TOOL_CALL, SECOND_TOOL_CALL),
                    ),
                    Message(
                        role="tool",
                        content='{"ok": true}',
                        tool_call_id=TOOL_CALL.id,
                    ),
                    Message(
                        role="tool",
                        content='{"ok": true}',
                        tool_call_id=SECOND_TOOL_CALL.id,
                    ),
                    Message(role="assistant", content="half an answer"),
                ),
                error=KeyboardInterrupt(),
            )
        )

        self.assertEqual(
            [(message.role, message.tool_call_id) for message in self.stored_items()],
            [
                ("user", None),
                ("assistant", None),
                ("tool", TOOL_CALL.id),
                ("tool", SECOND_TOOL_CALL.id),
                ("assistant", None),
            ],
        )

    def test_reports_a_storage_failure_without_ending_the_bridge(self) -> None:
        storage = patch.object(
            self.bridge._store,
            "append_events",
            side_effect=ValueError("session already exists in workspace: 'x'"),
        )
        storage.start()

        self.start_turn(ScriptedAgent(self.session, error=KeyboardInterrupt()))

        failure = emitted(self.stdout)[-1]
        self.assertEqual(failure["type"], "turn_failed")

        # The bridge keeps serving turns once storage recovers.
        storage.stop()
        self.start_turn(
            ScriptedAgent(self.session, error=KeyboardInterrupt()),
            text="second try",
        )
        self.assertEqual(emitted(self.stdout)[-1]["type"], "turn_cancelled")
        self.assertEqual(
            [message.content for message in self.stored_items()],
            ["second try"],
        )


class BridgeSessionOpenTest(unittest.TestCase):
    """The bridge selects the provider the interface asked for."""

    def started_model(self, **extra: object) -> str:
        with tempfile.TemporaryDirectory() as directory:
            bridge, stdout = make_bridge([])
            bridge.open_session(open_session_message(Path(directory), **extra))
            return str(emitted(stdout)[0]["model"])

    def test_uses_the_configured_provider_by_default(self) -> None:
        self.assertEqual(self.started_model(), "openai/first")

    def test_uses_the_requested_provider(self) -> None:
        self.assertEqual(
            self.started_model(provider="second"),
            "openai/second",
        )

    def test_uses_the_configured_provider_when_selection_is_null(self) -> None:
        self.assertEqual(self.started_model(provider=None), "openai/first")

    def test_restores_the_session_provider_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            store.set_provider("saved", "second", root)
            bridge, stdout = make_bridge([])

            bridge.open_session(open_session_message(root, session_id="saved"))

            self.assertEqual(emitted(stdout)[0]["provider"], "second")
            self.assertEqual(emitted(stdout)[0]["model"], "openai/second")

    def test_opening_a_session_does_not_initialize_the_agent_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, stdout = make_bridge([])

            bridge.open_session(open_session_message(Path(directory)))

            self.assertIsNone(bridge._agent)
            self.assertIsNone(bridge._mcp)
            self.assertEqual(
                [message["type"] for message in emitted(stdout)],
                ["session_ready", "runtime_state"],
            )
            self.assertEqual(emitted(stdout)[-1]["phase"], "inactive")

    def test_opening_a_session_does_not_require_the_provider_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message = open_session_message(
                root,
                provider_config={
                    "main_agent": {"provider": "first"},
                    "providers": {
                        "first": {
                            "model": "openai/first",
                            "key": "${MISSING_NOSIS_TEST_KEY}",
                        }
                    },
                },
            )
            bridge, stdout = make_bridge([])

            bridge.open_session(message)

            self.assertEqual(emitted(stdout)[0]["type"], "session_ready")
            with self.assertRaisesRegex(
                ValueError,
                "MISSING_NOSIS_TEST_KEY",
            ):
                bridge._ensure_runtime()

    def test_restores_the_session_permission_preset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            session = Session("saved")
            session.set_permission_preset(PermissionPreset.FULL_ACCESS)
            store.bind_workspace(session.session_id, root)
            store.set_permission_preset(
                session.session_id,
                PermissionPreset.FULL_ACCESS,
                root,
            )
            store.append_events(session.session_id, session.journal, workspace=root)
            bridge, stdout = make_bridge([])

            bridge.open_session(open_session_message(root, session_id=session.session_id))

            ready = emitted(stdout)[0]
            self.assertEqual(ready["permission_preset"], "full_access")
            assert bridge._permissions is not None
            bridge._permissions.authorize(
                TOOL_CALL,
                ToolExecutionContext(
                    workspace=Workspace(Path(__file__).parent),
                    session=bridge._permissions._session,
                ),
            )
            self.assertFalse(
                any(message["type"] == "approval_request" for message in emitted(stdout))
            )

    def test_rejects_an_unconfigured_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "not configured"):
            self.started_model(provider="unknown")

    def test_registers_discovered_skills_and_their_read_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_directory = root / "skills" / "demo"
            skill_directory.mkdir(parents=True)
            (skill_directory / "SKILL.md").write_text(
                "---\n"
                "name: demo-skill\n"
                "description: Does demo work.\n"
                "---\n\n"
                "# Detailed instructions\n",
                encoding="utf-8",
            )
            bridge, _ = make_bridge([])

            bridge.open_session(open_session_message(root))
            bridge._ensure_runtime()

            names = [item.name for item in bridge._agent._tools.definitions]
            prompt = bridge._agent._context._system_prompt

        self.assertIn("read_skill", names)
        self.assertIn("demo-skill: Does demo work.", prompt)
        self.assertNotIn("# Detailed instructions", prompt)

    def test_invalid_skill_does_not_prevent_bridge_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "skills" / "invalid"
            invalid.mkdir(parents=True)
            (invalid / "SKILL.md").write_text(
                "# Missing front matter\n", encoding="utf-8"
            )
            valid = root / "skills" / "valid"
            valid.mkdir()
            (valid / "SKILL.md").write_text(
                "---\nname: valid-skill\n"
                "description: Still available.\n---\n",
                encoding="utf-8",
            )
            bridge, stdout = make_bridge([])

            bridge.open_session(open_session_message(root))
            bridge._ensure_runtime()

            runtime_state = emitted(stdout)[-1]
            names = [item.name for item in bridge._agent._tools.definitions]

        self.assertEqual(runtime_state["type"], "runtime_state")
        self.assertEqual(runtime_state["phase"], "starting")
        self.assertEqual(len(bridge._skill_warnings), 1)
        self.assertIn("Skipping skill at", bridge._skill_warnings[0])
        self.assertIn("read_skill", names)


class SubagentRoleStartTest(unittest.TestCase):
    """Only enabled roles reach the model's subagent schema."""

    def offered_roles(self, roles: dict) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _ = make_bridge([])
            bridge.open_session(
                open_session_message(
                    Path(directory),
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "main_agent": {"tools": {"subagent": True}},
                        "subagent_roles": roles,
                    },
                )
            )
            bridge._ensure_runtime()
            subagents = bridge._agent._tools._context.subagents
            if subagents is None:
                return []
            return [role.name for role in subagents.roles]

    def test_offers_every_enabled_role(self) -> None:
        self.assertEqual(
            self.offered_roles(
                {
                    "researcher": {"description": "Reads.", "tools": {}},
                    "coder": {
                        "enabled": True,
                        "description": "Writes.",
                        "tools": {},
                    },
                }
            ),
            ["researcher", "coder"],
        )

    def test_omits_a_disabled_role(self) -> None:
        self.assertEqual(
            self.offered_roles(
                {
                    "researcher": {"description": "Reads.", "tools": {}},
                    "coder": {
                        "enabled": False,
                        "description": "Writes.",
                        "tools": {},
                    },
                }
            ),
            ["researcher"],
        )

    def test_disabling_every_role_removes_the_subagent_tool(self) -> None:
        """With no role left there is nothing to delegate to."""
        with tempfile.TemporaryDirectory() as directory:
            bridge, _ = make_bridge([])
            bridge.open_session(
                open_session_message(
                    Path(directory),
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "main_agent": {"tools": {"subagent": True}},
                        "subagent_roles": {
                            "researcher": {
                                "enabled": False,
                                "description": "Reads.",
                                "tools": {},
                            }
                        },
                    },
                )
            )
            bridge._ensure_runtime()

            self.assertEqual(
                [definition.name for definition in bridge._agent._tools.definitions],
                ["ask_user"],
            )

    def test_ask_user_is_registered_only_for_the_main_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _ = make_bridge([])
            bridge.open_session(
                open_session_message(
                    Path(directory),
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "main_agent": {"tools": {"subagent": True}},
                        "subagent_roles": {
                            "researcher": {
                                "description": "Reads.",
                                "tools": {"read_file": True},
                            }
                        },
                    },
                )
            )
            bridge._ensure_runtime()

            main_names = {
                definition.name for definition in bridge._agent._tools.definitions
            }
            subagent = bridge._agent._tools._context.subagents
            assert subagent is not None
            role = subagent.roles.get("researcher")
            role_context = bridge._agent._tools._context
            role_names = {
                definition.name
                for definition in subagent._catalog.select(
                    role.tools, role_context
                ).definitions
            }

            self.assertIn("ask_user", main_names)
            self.assertNotIn("ask_user", role_names)


VISION_MODEL = "vision/model"
TEXT_MODEL = "text/model"


def _capabilities(model: str, base_url: str | None = None):
    """Report only VISION_MODEL as image-capable, without touching LiteLLM."""
    modalities = {"text"}
    if model == VISION_MODEL:
        modalities.add("image")
    return ProviderCapabilities(frozenset(modalities))


# capabilities_for_model is a classmethod, so the patch must absorb `cls`.
_CAPABILITIES_PATCH = classmethod(
    lambda cls, model, base_url=None: _capabilities(model, base_url)
)


class AnalyzeImageDerivationTest(unittest.TestCase):
    """The image tools are derived from capability, never configured.

    Exactly one of them is registered: a model that reads images gets
    ``read_image``, and a model that cannot gets ``analyze_image`` --
    but only when the Runtime resolved somewhere to send them instead.
    """

    def tool_names(
        self,
        main_model: str,
        vision_provider: str | None,
        role_blocks: dict | None = None,
        role_tools: dict | None = None,
    ) -> tuple[list[str], list[str]]:
        """Return (main agent tool names, 'researcher' role tool names)."""
        main_agent: dict[str, object] = {"provider": "main"}
        if vision_provider is not None:
            main_agent["vision_provider"] = vision_provider
        with tempfile.TemporaryDirectory() as directory:
            bridge, _ = make_bridge([])
            with patch.object(
                LiteLLMProvider, "capabilities_for_model", _CAPABILITIES_PATCH
            ):
                bridge.open_session(
                    open_session_message(
                        Path(directory),
                        provider_config={
                            "main_agent": main_agent,
                            "subagent": {"provider": ""},
                            "subagent_roles": role_blocks or {},
                            "providers": {
                                "main": {
                                    "model": main_model,
                                    "max_context_tokens": 1000,
                                },
                                "seeing": {
                                    "model": VISION_MODEL,
                                    "max_context_tokens": 1000,
                                },
                                "blind": {
                                    "model": TEXT_MODEL,
                                    "max_context_tokens": 1000,
                                },
                            },
                        },
                        agent_config={
                            "max_same_tool_calls": 5,
                            "output_reserve_tokens": 100,
                            "main_agent": {"tools": {"subagent": True}},
                            "subagent_roles": {
                                "researcher": {
                                    "description": "Reads.",
                                    "tools": role_tools or {},
                                }
                            },
                        },
                    )
                )
                bridge._ensure_runtime()
                main_names = sorted(
                    d.name for d in bridge._agent._tools.definitions
                )
                role = bridge._agent._tools._context.subagents.roles.get(
                    "researcher"
                )
                role_names = sorted(
                    vision_aware_tool_names(
                        role.tools, role.provider, role.vision_provider
                    )
                )
        return main_names, role_names

    def test_a_vision_model_does_not_get_the_tool(self) -> None:
        """Images inline, so the tool would be a second, redundant call."""
        main, role = self.tool_names(VISION_MODEL, "seeing")

        self.assertNotIn("analyze_image", main)
        self.assertNotIn("analyze_image", role)

    def test_a_vision_model_gets_read_image_instead(self) -> None:
        """It can see for itself, so it is handed the pixels."""
        main, role = self.tool_names(VISION_MODEL, "seeing")

        self.assertIn("read_image", main)
        self.assertIn("read_image", role)

    def test_a_text_model_with_a_vision_provider_gets_the_tool(self) -> None:
        main, role = self.tool_names(TEXT_MODEL, "seeing")

        self.assertIn("analyze_image", main)
        self.assertIn("analyze_image", role)

    def test_a_text_model_never_gets_read_image(self) -> None:
        """Inlining an image for a text model would be dropped anyway."""
        for vision_provider in ("seeing", None):
            with self.subTest(vision_provider=vision_provider):
                main, role = self.tool_names(TEXT_MODEL, vision_provider)

                self.assertNotIn("read_image", main)
                self.assertNotIn("read_image", role)

    def test_the_two_image_tools_are_never_both_registered(self) -> None:
        """They are alternatives: one route into context, not two."""
        for model in (VISION_MODEL, TEXT_MODEL):
            for vision_provider in ("seeing", None):
                with self.subTest(model=model, vision=vision_provider):
                    main, role = self.tool_names(model, vision_provider)

                    for names in (main, role):
                        self.assertLess(
                            len(
                                {"read_image", "analyze_image"}
                                & set(names)
                            ),
                            2,
                        )

    def test_a_text_model_without_a_vision_provider_does_not(self) -> None:
        """Nothing to route images to, so the tool would always fail."""
        main, role = self.tool_names(TEXT_MODEL, None)

        self.assertNotIn("analyze_image", main)
        self.assertNotIn("analyze_image", role)

    def test_a_role_derives_from_its_own_model(self) -> None:
        """A text-only role under a vision main agent still gets the tool."""
        main, role = self.tool_names(
            VISION_MODEL,
            "seeing",
            role_blocks={"researcher": {"provider": "blind"}},
        )

        self.assertNotIn("analyze_image", main)
        self.assertIn("analyze_image", role)

    def test_a_role_cannot_ask_for_the_tool_in_its_config(self) -> None:
        with self.assertRaisesRegex(ValueError, "analyze_image"):
            self.tool_names(
                TEXT_MODEL, "seeing", role_tools={"analyze_image": True}
            )


class CrossFileRoleValidationTest(unittest.TestCase):
    """The two config files must agree on which roles exist."""

    def start(self, provider_roles: dict, agent_roles: dict) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _ = make_bridge([])
            bridge.open_session(
                open_session_message(
                    Path(directory),
                    provider_config={
                        "main_agent": {"provider": "first"},
                        "subagent": {"provider": ""},
                        "subagent_roles": provider_roles,
                        "providers": {
                            "first": {
                                "model": "openai/first",
                                "max_context_tokens": 1000,
                            }
                        },
                    },
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "main_agent": {"tools": {"subagent": True}},
                        "subagent_roles": agent_roles,
                    },
                )
            )
            bridge._ensure_runtime()

    def test_rejects_a_provider_override_for_an_unknown_role(self) -> None:
        """A typo must fail loudly instead of silently doing nothing."""
        with self.assertRaisesRegex(
            ValueError, "unknown subagent role\\(s\\): codr"
        ):
            self.start(
                {"codr": {"provider": "first"}},
                {"coder": {"description": "Writes.", "tools": {}}},
            )

    def test_accepts_matching_role_names(self) -> None:
        self.start(
            {"coder": {"provider": "first"}},
            {"coder": {"description": "Writes.", "tools": {}}},
        )


if __name__ == "__main__":
    unittest.main()

import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import weakref
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest.mock import patch

from agent_core import (
    AssistantMessageDeltaEvent,
    ProviderCapabilities,
    AssistantMessageEvent,
    ContextWindow,
    ContextWindowEvent,
    ExecutionScope,
    JsonlSessionStore,
    JobHandle,
    JobStatusEvent,
    Message,
    MemoryDocument,
    MemoryStore,
    PermissionPreset,
    PlanManager,
    PlanSnapshot,
    PlanStep,
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
    FULL_ACCESS_AUTHORITY,
    WORKSPACE_ONLY_AUTHORITY,
    Workspace,
)
from agent_core.scheduler import (
    AgentTurnAction,
    OneShotTrigger,
    Schedule,
    ScheduledRun,
    SchedulerService,
)
from agent_core.llm import TokenUsage
from agent_core.projection import project_context_units
from agent_core.providers import LiteLLMProvider
from agent_core.subagent import vision_aware_tool_names
from agent_core.tools.config import ROLE_TOOL_NAMES, TOOL_NAMES
from interfaces.bridge.bridge import Bridge, Cancelled
from agent_runtime.attachments import parse_attachments as _parse_attachments
from agent_runtime.host import RuntimeHost
from agent_runtime.plane_manager import ExecutionPlaneManager
from agent_runtime.scheduled_runner import ScheduledTurnRunner
from agent_runtime.execution_plane import ExecutionPlane
from interfaces.bridge.protocol import (
    attachment_replaced_message,
    decode,
    event_to_message,
    format_timestamp,
    session_ready_message,
    runtime_state_message,
    usage_to_dict,
    runtime_failure_to_dict,
)

TOOL_CALL = ToolCall(id="call-1", name="shell", arguments={"command": "ls"})
SECOND_TOOL_CALL = ToolCall(id="call-2", name="shell", arguments={"command": "pwd"})
EVENT_TIME = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)


class ExecutionPlaneTest(unittest.TestCase):
    def test_uses_identity_equality_and_hashing(self) -> None:
        self.assertIs(ExecutionPlane.__eq__, object.__eq__)
        self.assertIs(ExecutionPlane.__hash__, object.__hash__)


class AttachmentParsingTest(unittest.TestCase):
    def test_parses_an_arbitrary_file_from_its_actual_workspace_state(self) -> None:
        from agent_core import FilePart

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".nosis" / "attachments" / "data.bin"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"binary data")

            attachments = _parse_attachments(
                [
                    {
                        "type": "file",
                        "path": ".nosis/attachments/data.bin",
                        "filename": "source.xlsx",
                        "mime_type": "application/vnd.ms-excel",
                        "size_bytes": 1,
                    }
                ],
                Workspace(root),
            )

        self.assertEqual(
            attachments,
            (
                FilePart(
                    path=".nosis/attachments/data.bin",
                    filename="source.xlsx",
                    mime_type="application/vnd.ms-excel",
                    size_bytes=11,
                ),
            ),
        )

    def test_image_bytes_override_a_file_type_claim(self) -> None:
        from agent_core import ImagePart
        from tests.test_media import png_bytes

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".nosis" / "attachments" / "upload.bin"
            path.parent.mkdir(parents=True)
            path.write_bytes(png_bytes(4, 3))

            attachments = _parse_attachments(
                [
                    {
                        "type": "file",
                        "path": ".nosis/attachments/upload.bin",
                        "filename": "diagram.bin",
                        "mime_type": "application/octet-stream",
                        "size_bytes": 1,
                    }
                ],
                Workspace(root),
            )

        self.assertEqual(attachments[0].type, "image")
        self.assertIsInstance(attachments[0], ImagePart)
        self.assertEqual(attachments[0].mime_type, "image/png")
        self.assertEqual(attachments[0].filename, "diagram.bin")


class ProtocolTest(unittest.TestCase):
    def test_runtime_failure_to_dict_includes_details(self) -> None:
        from agent_core import RuntimeErrorInfo

        self.assertEqual(
            runtime_failure_to_dict(
                RuntimeErrorInfo(
                    "ProviderProtocolError",
                    "invalid tool arguments",
                    {"phase": "tool_call_assembly"},
                )
            ),
            {
                "type": "ProviderProtocolError",
                "message": "invalid tool arguments",
                "details": {"phase": "tool_call_assembly"},
            },
        )

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
                plan=PlanSnapshot(
                    "plan-1",
                    "Ship plan support",
                    2,
                    (PlanStep("verify", "Verify", "in_progress"),),
                ),
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
                "runtime_warnings": [],
                "plan": {
                    "plan_id": "plan-1",
                    "goal": "Ship plan support",
                    "revision": 2,
                    "steps": [
                        {
                            "id": "verify",
                            "title": "Verify",
                            "status": "in_progress",
                        }
                    ],
                },
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


def make_bridge(
    lines: list[str],
    config_directory: Path | None = None,
) -> tuple[Bridge, io.StringIO]:
    stdin = io.StringIO("".join(f"{line}\n" for line in lines))
    stdout = io.StringIO()
    if config_directory is None:
        return isolated_bridge(stdin, stdout), stdout
    with patch(
        "interfaces.bridge.bridge.default_config_directory",
        return_value=config_directory,
    ):
        return Bridge(stdin, stdout, start_scheduler=False), stdout


def isolated_bridge(stdin, stdout: io.StringIO) -> Bridge:
    directory = tempfile.mkdtemp(prefix="nosis-bridge-test-")
    scheduler = SchedulerService(Path(directory) / "schedule.jsonl")
    bridge = Bridge(
        stdin,
        stdout,
        scheduler=scheduler,
        start_scheduler=False,
    )
    weakref.finalize(bridge, shutil.rmtree, directory, ignore_errors=True)
    return bridge


def emitted(stdout: io.StringIO) -> list[dict]:
    return [
        json.loads(line)
        for line in stdout.getvalue().splitlines()
        if line.strip()
    ]


class IsolatedBridgeTest(unittest.TestCase):
    def test_scheduler_directory_survives_helper_return(self) -> None:
        bridge = isolated_bridge(io.StringIO(), io.StringIO())
        try:
            self.assertTrue(bridge.host.scheduler.path.parent.is_dir())
        finally:
            bridge.close()


class PermissionProtocolTest(unittest.TestCase):
    def test_bridge_accepts_workspace_access_before_runtime_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, stdout = make_bridge([], root)
            bridge.open_session(open_session_message(root, session_id="s"))

            bridge._set_permission_preset(
                {"type": "permission_set", "preset": "workspace_access"}
            )

            self.assertIsNone(bridge.host.planes.current)
            assert bridge.host.sessions.session is not None
            self.assertEqual(
                bridge.host.sessions.session.permission_preset,
                PermissionPreset.WORKSPACE_ACCESS,
            )
            self.assertEqual(
                emitted(stdout)[-1],
                {"type": "permission_changed", "preset": "workspace_access"},
            )

    def test_bridge_updates_permission_before_runtime_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, stdout = make_bridge([], root)
            bridge.open_session(open_session_message(root, session_id="s"))

            bridge._set_permission_preset(
                {"type": "permission_set", "preset": "full_access"}
            )

            self.assertIsNone(bridge.host.planes.current)
            assert bridge.host.sessions.session is not None
            self.assertEqual(
                bridge.host.sessions.session.permission_preset,
                PermissionPreset.FULL_ACCESS,
            )
            self.assertEqual(
                JsonlSessionStore(root / "sessions").permission_preset_for("s"),
                PermissionPreset.FULL_ACCESS,
            )
            self.assertEqual(
                bridge.host.sessions.session.journal[-1].event_type,
                "permission_preset_changed",
            )
            self.assertEqual(
                JsonlSessionStore(root / "sessions").list_sessions(), []
            )
            self.assertEqual(
                emitted(stdout)[-1],
                {"type": "permission_changed", "preset": "full_access"},
            )

    def test_workspace_access_routes_shell_without_host_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, stdout = make_bridge([], root)
            bridge.open_session(
                open_session_message(
                    root,
                    session_id="s",
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "workspace_instruction_files": [
                            "CLAUDE.md",
                            "AGENTS.md",
                        ],
                        "main_agent": {
                            "tools": {
                                name: name == "shell" for name in TOOL_NAMES
                            }
                        },
                    },
                )
            )
            bridge._set_permission_preset(
                {"type": "permission_set", "preset": "workspace_access"}
            )
            plane = bridge.host.planes.ensure(bridge.host.sessions)
            try:
                command = (
                    "Set-Content -NoNewline -LiteralPath routed.txt "
                    "-Value workspace"
                    if sys.platform == "win32"
                    else "printf workspace > routed.txt"
                )
                result = plane.agent._tools.execute(
                    ToolCall(
                        "call-workspace",
                        "shell",
                        {"command": command},
                    )
                )
            finally:
                bridge.host.planes.close()

            if (
                result.error is not None
                and result.error.type == "RuntimeError"
                and "Operation not permitted" in result.error.message
            ):
                self.skipTest("the test runner already forbids nested Seatbelt")
            self.assertIsNone(result.error)
            self.assertEqual(
                (root / "routed.txt").read_text(encoding="utf-8"),
                "workspace",
            )
            self.assertFalse(
                any(
                    message["type"] == "approval_request"
                    for message in emitted(stdout)
                )
            )

    def test_bridge_changes_session_configuration_without_initializing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            next_workspace = root / "next"
            next_workspace.mkdir()
            bridge, stdout = make_bridge([], root)
            bridge.open_session(open_session_message(root, session_id="s"))

            bridge._set_provider({"type": "provider_set", "provider": "second"})
            bridge._set_workspace(
                {"type": "workspace_set", "workspace": str(next_workspace)}
            )
            bridge._set_permission_preset(
                {"type": "permission_set", "preset": "full_access"}
            )

            self.assertIsNone(bridge.host.planes.current)
            self.assertEqual(
                JsonlSessionStore(root / "sessions").provider_for("s"),
                "second",
            )
            self.assertEqual(
                bridge.host.sessions.workspace.path,
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

    def test_provider_change_closes_the_execution_plane_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _ = make_bridge([], root)
            bridge.open_session(open_session_message(root, session_id="s"))
            plane = bridge.host.planes.ensure(bridge.host.sessions)

            with (
                patch.object(
                    plane.jobs,
                    "close",
                    wraps=plane.jobs.close,
                ) as close_jobs,
                patch.object(
                    plane.mcp,
                    "close",
                    wraps=plane.mcp.close,
                ) as close_mcp,
                patch.object(
                    plane.execution_router,
                    "close",
                    wraps=plane.execution_router.close,
                ) as close_execution_router,
            ):
                bridge._set_provider(
                    {"type": "provider_set", "provider": "second"}
                )

            self.assertIsNone(bridge.host.planes.current)
            close_jobs.assert_called_once_with()
            close_mcp.assert_called_once_with()
            close_execution_router.assert_called_once_with()

    def test_workspace_change_closes_the_execution_plane_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            next_workspace = root / "next"
            next_workspace.mkdir()
            bridge, _ = make_bridge([], root)
            bridge.open_session(open_session_message(root, session_id="s"))
            plane = bridge.host.planes.ensure(bridge.host.sessions)

            with (
                patch.object(
                    plane.jobs,
                    "close",
                    wraps=plane.jobs.close,
                ) as close_jobs,
                patch.object(
                    plane.mcp,
                    "close",
                    wraps=plane.mcp.close,
                ) as close_mcp,
                patch.object(
                    plane.execution_router,
                    "close",
                    wraps=plane.execution_router.close,
                ) as close_execution_router,
            ):
                bridge._set_workspace(
                    {
                        "type": "workspace_set",
                        "workspace": str(next_workspace),
                    }
                )

            self.assertIsNone(bridge.host.planes.current)
            close_jobs.assert_called_once_with()
            close_mcp.assert_called_once_with()
            close_execution_router.assert_called_once_with()


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
    def test_runtime_state_contains_only_the_jobs_that_are_running(self) -> None:
        class Jobs:
            @staticmethod
            def snapshot():
                return (
                    JobHandle("j1", "shell", "running"),
                    JobHandle("j2", "shell", "completed"),
                )

        class Plane:
            jobs = Jobs()
            context_window = ContextWindow(
                input_tokens=0,
                max_input_tokens=900,
                max_context_tokens=1000,
                output_reserve_tokens=100,
                compression_threshold=720,
                compression_count=0,
            )

        with tempfile.TemporaryDirectory() as directory:
            bridge, stdout = make_bridge([], Path(directory))
            bridge.open_session(
                open_session_message(Path(directory), session_id="s")
            )
            bridge.host.planes.current = Plane()

            bridge._emit_runtime_state("running")

            self.assertEqual(
                emitted(stdout)[-1]["jobs"],
                [{"job_id": "j1", "kind": "shell", "status": "running"}],
            )

    def test_runtime_state_contains_the_pending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, stdout = make_bridge(
                ['{"type": "approval_response", "request_id": "t1:1",'
                 ' "approved": true}'],
                Path(directory),
            )
            bridge.open_session(
                open_session_message(Path(directory), session_id="s")
            )
            bridge.host.turns.turn_id = "t1"

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
        bridge = isolated_bridge(_SlowStdin(responses), stdout)
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
        bridge = isolated_bridge(stdin, stdout)
        control = TurnControl()
        bridge.host.turns.turn_id = "t1"
        bridge.host.turns.control = control

        self.assertTrue(bridge.request_permission("ls"))
        self.assertEqual(control.drain_steering()[0].text, "check tests")
        self.assertIn(
            "user_steer_received",
            [message["type"] for message in emitted(stdout)],
        )
        bridge.host.turns.control = None
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

        self.assertIn("t1", bridge.host.turns._pending_cancels)


class BridgeUserQuestionTest(unittest.TestCase):
    OPTIONS = [
        {"id": "memory", "label": "Memory"},
        {"id": "sqlite", "label": "SQLite"},
    ]

    def test_runtime_state_contains_the_pending_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, stdout = make_bridge(
                ['{"type": "user_question_response", "request_id": "t1:1",'
                 ' "option_id": "sqlite"}'],
                Path(directory),
            )
            bridge.open_session(
                open_session_message(Path(directory), session_id="s")
            )
            bridge.host.turns.turn_id = "t1"

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
                ],
                root,
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
                ],
                root,
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
            self.assertIsNone(bridge.host.planes.current)

    def test_answers_settings_requests_without_starting_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, stdout = make_bridge(
                [
                    json.dumps(open_session_message(root, session_id="empty")),
                    '{"type": "settings_get", "request_id": "settings-1"}',
                    '{"type": "shutdown"}',
                ],
                root,
            )

            bridge.serve()

            snapshot = next(message for message in emitted(stdout) if message["type"] == "settings_snapshot")
            self.assertEqual(snapshot["request_id"], "settings-1")
            self.assertEqual(snapshot["settings"]["default_provider"], "first")
            self.assertIsNone(bridge.host.planes.current)


def open_session_message(
    directory: Path,
    agent_config: dict | None = None,
    provider_config: dict | None = None,
    **extra: object,
) -> dict:
    prompts = directory / "prompts"
    prompts.mkdir(exist_ok=True)
    for filename, content in {
        "Soul.md": "Bridge system prompt for {{workspace}}",
        "SubAgent.md": (
            "Role {{role}}: {{role_description}} in {{workspace}}"
        ),
        "Consolidator.md": "Consolidate the conversation.",
        "GlobalMemory.md": "Reconcile global memory as JSON.",
        "WorkspaceMemory.md": "Reconcile workspace memory as JSON.",
    }.items():
        path = prompts / filename
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    (directory / "provider_config.json").write_text(
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
    resolved_agent_config = {
        "provider": {
            "request_timeout_seconds": 300,
            "max_retries": 2,
        },
        **(agent_config if agent_config is not None else {
            "max_same_tool_calls": 5,
            "output_reserve_tokens": 100,
            "scratch_workspace_root": str(directory / "scratch"),
            "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
            "main_agent": {"tools": {name: False for name in TOOL_NAMES}},
        }),
    }
    (directory / "agent_config.json").write_text(
        json.dumps(
            resolved_agent_config
        ),
        encoding="utf-8",
    )
    return {
        "type": "open_session",
        "workspace": str(directory),
        "session_id": None,
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


class SuccessfulAgent:
    def __init__(self, session: Session) -> None:
        self._session = session

    def run(
        self,
        user_input: str,
        on_event: object = None,
        attachments: object = (),
        turn_id: str | None = None,
        turn_control: TurnControl | None = None,
    ) -> object:
        self._session.begin_turn(turn_id)
        self._session.add_item("user", user_input)
        self._session.add_item("assistant", "done")
        self._session.finish_turn("completed", turn_id)
        return SimpleNamespace(response=SimpleNamespace(usage=None))


class MemorySpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def begin_turn(self) -> None:
        self.calls.append("begin")

    def discard_pending(self) -> None:
        self.calls.append("discard")

    def reconcile_pending(self) -> bool:
        self.calls.append("reconcile")
        return True


class InterruptedTurnTest(unittest.TestCase):
    """A turn keeps the transcript it produced before it ended early."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bridge, self.stdout = make_bridge([], self.root)
        self.addCleanup(self.bridge.close)
        self.bridge.open_session(open_session_message(self.root))
        self.session_id = next(
            message["session_id"]
            for message in emitted(self.stdout)
            if message["type"] == "session_ready"
        )
        self.store = JsonlSessionStore(self.root / "sessions")
        session = self.bridge.host.sessions.session
        assert session is not None
        self.session = session

    def start_turn(self, agent: object, text: str = "do the work") -> None:
        plane = self.bridge.host.planes.ensure(self.bridge.host.sessions)
        plane.agent = agent
        self.bridge.run_turn({"turn_id": "t1", "text": text})

    def stored_items(self) -> list[Message]:
        return self.store.load(self.session_id).items

    def test_stores_a_cancelled_turn(self) -> None:
        plane = self.bridge.host.planes.ensure(self.bridge.host.sessions)
        memory = MemorySpy()
        plane.memory = memory
        plane.agent = ScriptedAgent(
            self.session,
            tail=(Message(role="assistant", content="half an answer"),),
            error=KeyboardInterrupt(),
        )
        self.bridge.run_turn({"turn_id": "t1", "text": "do the work"})

        self.assertEqual(emitted(self.stdout)[-1]["type"], "turn_cancelled")
        self.assertEqual(memory.calls, ["begin", "discard"])
        self.assertEqual(
            [message.content for message in self.stored_items()],
            ["do the work", "half an answer"],
        )

    def test_reconciles_memory_only_after_a_successful_turn(self) -> None:
        plane = self.bridge.host.planes.ensure(self.bridge.host.sessions)
        memory = MemorySpy()
        plane.memory = memory
        plane.agent = SuccessfulAgent(self.session)

        self.bridge.run_turn({"turn_id": "t1", "text": "remember this"})

        self.assertEqual(memory.calls, ["begin", "reconcile"])
        self.assertEqual(emitted(self.stdout)[-1]["type"], "turn_completed")

    def test_discards_memory_candidates_when_a_turn_fails(self) -> None:
        plane = self.bridge.host.planes.ensure(self.bridge.host.sessions)
        memory = MemorySpy()
        plane.memory = memory
        plane.agent = FailingAgent(self.session)

        self.bridge.run_turn({"turn_id": "t1", "text": "do the work"})

        self.assertEqual(memory.calls, ["begin", "discard"])

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
            self.bridge.host.sessions.store,
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


class ScheduledTurnTest(unittest.TestCase):
    def test_scheduled_worker_uses_persisted_execution_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler = SchedulerService(root / "schedule.jsonl")
            captured = []

            def open_session(worker: RuntimeHost, *args, **kwargs) -> None:
                captured.append(worker.planes._execution_authority_limit)
                raise RuntimeError("stop after capturing worker authority")

            with patch(
                "interfaces.bridge.bridge.default_config_directory",
                return_value=root,
            ), patch.object(RuntimeHost, "open_session", open_session):
                bridge = Bridge(
                    io.StringIO(),
                    io.StringIO(),
                    scheduler=scheduler,
                    start_scheduler=False,
                )
                for scope in (ExecutionScope.WORKSPACE, ExecutionScope.HOST):
                    schedule = Schedule(
                        f"schedule-{scope.value}",
                        OneShotTrigger(datetime.now(timezone.utc)),
                        AgentTurnAction("scheduled prompt"),
                        str(root),
                        "origin",
                        f"scheduled-session-{scope.value}",
                        scope,
                    )
                    run = ScheduledRun(
                        f"run-{scope.value}",
                        schedule.schedule_id,
                        schedule.schedule_session_id,
                        f"turn-{scope.value}",
                        datetime.now(timezone.utc),
                    )

                    with self.assertRaisesRegex(
                        RuntimeError,
                        "stop after capturing worker authority",
                    ):
                        ScheduledTurnRunner(root, scheduler)(schedule, run)
                bridge.close()

            self.assertEqual(
                captured,
                [WORKSPACE_ONLY_AUTHORITY, FULL_ACCESS_AUTHORITY],
            )

    def test_scheduled_turn_uses_normal_runtime_and_persists_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            open_session_message(root)
            scheduler = SchedulerService(root / "schedule.jsonl")

            class ScheduledAgent:
                def __init__(self, session: Session) -> None:
                    self.session = session

                def run(self, user_input: str, **kwargs: object) -> object:
                    turn_id = kwargs.get("turn_id")
                    self.session.begin_turn(
                        turn_id if isinstance(turn_id, str) else None
                    )
                    self.session.add_item("user", user_input)
                    self.session.add_item("assistant", "scheduled answer")
                    self.session.finish_turn("completed")
                    return type(
                        "Result",
                        (),
                        {"response": type("Response", (), {"usage": None})()},
                    )()

            def execution_plane(manager, state) -> object:
                assert state.session is not None
                return type(
                    "Plane",
                    (),
                    {
                        "agent": ScheduledAgent(state.session),
                        "jobs": type("Jobs", (), {"snapshot": lambda self: []})(),
                        "memory": None,
                        "runtime_warnings": (),
                    },
                )()

            with patch(
                "interfaces.bridge.bridge.default_config_directory",
                return_value=root,
            ), patch.object(ExecutionPlaneManager, "ensure", execution_plane):
                bridge = Bridge(
                    io.StringIO(),
                    io.StringIO(),
                    scheduler=scheduler,
                    start_scheduler=False,
                )
                cases = (
                    (
                        ExecutionScope.WORKSPACE,
                        PermissionPreset.WORKSPACE_ACCESS,
                    ),
                    (ExecutionScope.HOST, PermissionPreset.FULL_ACCESS),
                )
                for scope, _ in cases:
                    schedule = Schedule(
                        f"schedule-{scope.value}",
                        OneShotTrigger(datetime.now(timezone.utc)),
                        AgentTurnAction(f"scheduled prompt {scope.value}"),
                        str(root),
                        "origin",
                        f"scheduled-session-{scope.value}",
                        scope,
                    )
                    run = ScheduledRun(
                        f"run-{scope.value}",
                        schedule.schedule_id,
                        schedule.schedule_session_id,
                        f"turn-{scope.value}",
                        datetime.now(timezone.utc),
                    )

                    ScheduledTurnRunner(root, scheduler)(schedule, run)
                bridge.close()

            store = JsonlSessionStore(root / "sessions")
            for scope, preset in cases:
                stored = store.load(f"scheduled-session-{scope.value}")
                self.assertEqual(
                    [item.content for item in stored.items],
                    [f"scheduled prompt {scope.value}", "scheduled answer"],
                )
                self.assertEqual(
                    stored.turns[f"turn-{scope.value}"].status,
                    "completed",
                )
                self.assertEqual(stored.permission_preset, preset)


class BridgeSessionOpenTest(unittest.TestCase):
    """The bridge selects the provider the interface asked for."""

    def started_model(self, **extra: object) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, stdout = make_bridge([], root)
            bridge.open_session(open_session_message(root, **extra))
            return str(emitted(stdout)[0]["model"])

    def test_selects_the_configured_or_requested_provider(self) -> None:
        for label, extra, expected in (
            ("default", {}, "openai/first"),
            ("requested", {"provider": "second"}, "openai/second"),
            ("explicit default", {"provider": None}, "openai/first"),
        ):
            with self.subTest(label=label):
                self.assertEqual(self.started_model(**extra), expected)

    def test_restores_the_session_provider_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            store.set_provider("saved", "second", root)
            bridge, stdout = make_bridge([], root)

            bridge.open_session(open_session_message(root, session_id="saved"))

            self.assertEqual(emitted(stdout)[0]["provider"], "second")
            self.assertEqual(emitted(stdout)[0]["model"], "openai/second")

    def test_opening_a_session_does_not_initialize_the_agent_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, stdout = make_bridge([], root)

            bridge.open_session(open_session_message(root))

            self.assertIsNone(bridge.host.planes.current)
            self.assertEqual(
                [message["type"] for message in emitted(stdout)],
                ["session_ready", "runtime_state"],
            )
            self.assertEqual(emitted(stdout)[-1]["phase"], "inactive")

    def test_applies_provider_request_policy_to_all_chat_providers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _ = make_bridge([], root)
            bridge.open_session(
                open_session_message(
                    root,
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "provider": {
                            "request_timeout_seconds": 45,
                            "max_retries": 4,
                        },
                        "scratch_workspace_root": str(root / "scratch"),
                        "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
                        "main_agent": {"tools": {"subagent": True}},
                        "subagent_roles": {
                            "researcher": {
                                "description": "Reads.",
                                "tools": {},
                            }
                        },
                    },
                    provider_config={
                        "main_agent": {
                            "provider": "first",
                            "vision_provider": "second",
                        },
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
                    },
                )
            )

            with patch.object(
                LiteLLMProvider,
                "capabilities_for_model",
                classmethod(
                    lambda cls, model, base_url=None: ProviderCapabilities(
                        frozenset({"text", "image"})
                    )
                ),
            ):
                plane = bridge.host.planes.ensure(bridge.host.sessions)
            main_provider = plane.agent._provider
            vision_provider = plane.agent._tools._context.vision_provider
            subagents = plane.agent._tools._context.subagents
            assert vision_provider is not None
            assert subagents is not None
            role_provider = subagents.roles.get("researcher").provider

        self.assertEqual(main_provider._request_timeout_seconds, 45)
        self.assertEqual(main_provider._max_retries, 4)
        self.assertEqual(vision_provider._request_timeout_seconds, 45)
        self.assertEqual(vision_provider._max_retries, 4)
        self.assertEqual(role_provider._request_timeout_seconds, 45)
        self.assertEqual(role_provider._max_retries, 4)

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
            bridge, stdout = make_bridge([], root)

            bridge.open_session(message)

            self.assertEqual(emitted(stdout)[0]["type"], "session_ready")
            with self.assertRaisesRegex(
                ValueError,
                "MISSING_NOSIS_TEST_KEY",
            ):
                bridge.host.planes.ensure(bridge.host.sessions)

    def test_injects_the_configured_image_generator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _ = make_bridge([], root)
            bridge.open_session(
                open_session_message(
                    root,
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "scratch_workspace_root": str(root / "scratch"),
                        "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
                        "main_agent": {"tools": {"generate_image": True}},
                    },
                    provider_config={
                        "main_agent": {"provider": "first"},
                        "image_generation": {
                            "provider": "images",
                            "model": "openrouter/example/image",
                            "default_aspect_ratio": "16:9",
                            "default_image_size": "2K",
                        },
                        "providers": {
                            "first": {
                                "model": "openai/first",
                                "max_context_tokens": 1000,
                            },
                            "images": {
                                "model": "openrouter/unused",
                                "url": "https://example.test/v1",
                                "key": "secret",
                            },
                        },
                    },
                )
            )

            plane = bridge.host.planes.ensure(bridge.host.sessions)
            generator = plane.agent._tools._context.image_generator

        self.assertIsNotNone(generator)
        self.assertEqual(generator.model, "openrouter/example/image")
        self.assertIn(
            "generate_image",
            [definition.name for definition in plane.agent._tools.definitions],
        )

    def test_enabled_image_tool_requires_image_generation_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _ = make_bridge([], root)
            bridge.open_session(
                open_session_message(
                    root,
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "scratch_workspace_root": str(root / "scratch"),
                        "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
                        "main_agent": {"tools": {"generate_image": True}},
                    },
                )
            )

            with self.assertRaisesRegex(ValueError, "no image_generation"):
                bridge.host.planes.ensure(bridge.host.sessions)

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
            bridge, stdout = make_bridge([], root)

            bridge.open_session(open_session_message(root, session_id=session.session_id))

            ready = emitted(stdout)[0]
            self.assertEqual(ready["permission_preset"], "full_access")
            assert bridge.host.sessions.permissions is not None
            bridge.host.sessions.permissions.authorize(
                TOOL_CALL,
                ToolExecutionContext(
                    workspace=Workspace(Path(__file__).parent),
                    session=bridge.host.sessions.permissions._session,
                ),
            )
            self.assertFalse(
                any(message["type"] == "approval_request" for message in emitted(stdout))
            )

    def test_restores_the_session_plan_in_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            session = Session("saved")
            store.bind_workspace(session.session_id, root)
            session.attach_journal_sink(
                lambda events: store.append_events(
                    session.session_id, events, workspace=root
                )
            )
            PlanManager(session).update(
                "Ship plans",
                (PlanStep("verify", "Verify recovery", "in_progress"),),
            )
            bridge, stdout = make_bridge([], root)

            bridge.open_session(open_session_message(root, session_id="saved"))

            state = emitted(stdout)[-1]
            self.assertEqual(state["type"], "runtime_state")
            self.assertEqual(state["plan"]["goal"], "Ship plans")
            self.assertEqual(state["plan"]["revision"], 1)

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
            bridge, _ = make_bridge([], root)

            bridge.open_session(open_session_message(root))
            plane = bridge.host.planes.ensure(bridge.host.sessions)

            names = [item.name for item in plane.agent._tools.definitions]
            prompt = plane.agent._context._system_prompt

        self.assertIn("read_skill", names)
        self.assertIn("demo-skill: Does demo work.", prompt)
        self.assertNotIn("# Detailed instructions", prompt)

    def test_registers_plugin_skills_with_the_plugin_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugins" / "example"
            skill = plugin / "skills" / "demo"
            skill.mkdir(parents=True)
            (plugin / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "example",
                        "components": {"skills": ["skills"]},
                    }
                ),
                encoding="utf-8",
            )
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: demo-skill\n"
                "description: Plugin demo work.\n"
                "---\n",
                encoding="utf-8",
            )
            bridge, _ = make_bridge([], root)

            bridge.open_session(open_session_message(root))
            plane = bridge.host.planes.ensure(bridge.host.sessions)

            skills = plane.agent._execution_context.skills
            assert skills is not None
            definition = next(
                item
                for item in plane.agent._tools.definitions
                if item.name == "read_skill"
            )

        self.assertEqual(skills.names, ("example:demo-skill",))
        self.assertEqual(
            definition.parameters["properties"]["name"]["enum"],
            ["example:demo-skill"],
        )

    def test_registers_and_calls_plugin_mcp_tools_in_the_execution_plane(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugins" / "example"
            plugin.mkdir(parents=True)
            (plugin / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "example",
                        "components": {"mcp": [".mcp.json"]},
                    }
                ),
                encoding="utf-8",
            )
            (plugin / ".mcp.json").write_text(
                json.dumps(
                    {
                        "fake": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": [
                                str(
                                    Path(__file__).with_name(
                                        "fake_mcp_server.py"
                                    )
                                )
                            ],
                            "tools": {
                                "enabled": ["echo"],
                                "approval": "never",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            bridge, stdout = make_bridge([], root)
            bridge.open_session(
                open_session_message(
                    root,
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "workspace_instruction_files": [
                            "CLAUDE.md",
                            "AGENTS.md",
                        ],
                        "main_agent": {
                            "tools": {
                                name: False for name in TOOL_NAMES
                            }
                        },
                        "mcp": {"enabled": True, "servers": {}},
                    },
                )
            )

            try:
                plane = bridge.host.planes.ensure(bridge.host.sessions)
                names = {
                    item.name for item in plane.agent._tools.definitions
                }
                result = plane.agent._tools.execute(
                    ToolCall(
                        "plugin-mcp",
                        "mcp__example_fake__echo",
                        {"text": "hello"},
                    )
                )
                statuses = [
                    message
                    for message in emitted(stdout)
                    if message["type"] == "mcp_server_status"
                ]
            finally:
                bridge.host.planes.close()

        self.assertIn("mcp__example_fake__echo", names)
        self.assertIsNone(result.error)
        self.assertEqual(
            result.output["structured_content"],
            {"echo": "hello"},
        )
        self.assertTrue(
            any(
                status["server"] == "example:fake"
                and status["status"] == "ready"
                for status in statuses
            )
        )

    def test_global_mcp_switch_prevents_loading_plugin_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugins" / "example"
            plugin.mkdir(parents=True)
            (plugin / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "example",
                        "components": {"mcp": ["missing.json"]},
                    }
                ),
                encoding="utf-8",
            )
            bridge, stdout = make_bridge([], root)
            bridge.open_session(open_session_message(root))

            try:
                plane = bridge.host.planes.ensure(bridge.host.sessions)
            finally:
                bridge.host.planes.close()

        self.assertEqual(plane.mcp.tool_names, ())
        self.assertEqual(plane.runtime_warnings, ())
        self.assertFalse(
            any(
                message["type"] == "mcp_server_status"
                for message in emitted(stdout)
            )
        )

    def test_missing_plugin_mcp_warns_without_failing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugins" / "example"
            plugin.mkdir(parents=True)
            (plugin / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "example",
                        "components": {"mcp": ["missing.json"]},
                    }
                ),
                encoding="utf-8",
            )
            bridge, _ = make_bridge([], root)
            bridge.open_session(
                open_session_message(
                    root,
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "workspace_instruction_files": [
                            "CLAUDE.md",
                            "AGENTS.md",
                        ],
                        "main_agent": {
                            "tools": {
                                name: False for name in TOOL_NAMES
                            }
                        },
                        "mcp": {"enabled": True, "servers": {}},
                    },
                )
            )

            try:
                plane = bridge.host.planes.ensure(bridge.host.sessions)
            finally:
                bridge.host.planes.close()

        self.assertEqual(plane.mcp.tool_names, ())
        self.assertEqual(len(plane.runtime_warnings), 1)
        self.assertIn("Skipping MCP component", plane.runtime_warnings[0])

    def test_uses_prompt_templates_from_the_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message = open_session_message(root)
            (root / "prompts" / "Soul.md").write_text(
                "Custom prompt for {{workspace}}",
                encoding="utf-8",
            )
            bridge, _ = make_bridge([], root)

            bridge.open_session(message)
            plane = bridge.host.planes.ensure(bridge.host.sessions)

            prompt = plane.agent._context._system_prompt

        self.assertTrue(prompt.startswith(f"Custom prompt for {root.resolve()}"))
        self.assertIn("## Long-term Memory", prompt)

    def test_injects_global_and_workspace_instructions_without_persisting_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (root / "AGENTS.md").write_text("global rule", encoding="utf-8")
            (workspace / "CLAUDE.md").write_text("claude rule", encoding="utf-8")
            (workspace / "AGENTS.md").write_text("workspace rule", encoding="utf-8")
            bridge, _ = make_bridge([], root)

            bridge.open_session(open_session_message(root, workspace=str(workspace)))
            plane = bridge.host.planes.ensure(bridge.host.sessions)

            prompt = plane.agent._context._system_prompt
            session = bridge.host.sessions.session
            assert session is not None

        self.assertLess(prompt.index("global rule"), prompt.index("claude rule"))
        self.assertLess(prompt.index("claude rule"), prompt.index("workspace rule"))
        self.assertEqual(session.items, [])
        self.assertFalse(
            any(
                "global rule" in json.dumps(event, ensure_ascii=False)
                for event in session.journal
            )
        )

    def test_uses_configured_workspace_instruction_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "PROJECT.md").write_text("project rule", encoding="utf-8")
            (workspace / "AGENTS.md").write_text("not configured", encoding="utf-8")
            bridge, _ = make_bridge([], root)
            message = open_session_message(
                root,
                workspace=str(workspace),
                agent_config={
                    "max_same_tool_calls": 5,
                    "output_reserve_tokens": 100,
                    "workspace_instruction_files": ["PROJECT.md"],
                    "main_agent": {"tools": {name: False for name in TOOL_NAMES}},
                },
            )

            bridge.open_session(message)
            plane = bridge.host.planes.ensure(bridge.host.sessions)
            prompt = plane.agent._context._system_prompt

        self.assertIn("project rule", prompt)
        self.assertIn("<workspace>/PROJECT.md", prompt)
        self.assertNotIn("not configured", prompt)
        self.assertNotIn("<workspace>/AGENTS.md", prompt)

    def test_reuses_runtime_until_instructions_change_between_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instructions_path = root / "AGENTS.md"
            instructions_path.write_text("first rule", encoding="utf-8")
            bridge, _ = make_bridge([], root)
            bridge.open_session(open_session_message(root))
            first_plane = bridge.host.planes.ensure(bridge.host.sessions)

            self.assertIs(bridge.host.planes.ensure(bridge.host.sessions), first_plane)

            instructions_path.write_text("second rule", encoding="utf-8")
            second_plane = bridge.host.planes.ensure(bridge.host.sessions)

            self.assertIsNot(second_plane, first_plane)
            self.assertIn(
                "second rule", second_plane.agent._context._system_prompt
            )
            self.assertNotIn(
                "first rule", second_plane.agent._context._system_prompt
            )

    def test_memory_changes_are_loaded_by_the_next_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _ = make_bridge([], root)
            bridge.open_session(open_session_message(root))
            first_plane = bridge.host.planes.ensure(bridge.host.sessions)
            store = MemoryStore(
                root / "MEMORY.md",
                root / "sessions",
            )
            store.write_updates(
                root,
                global_memory=MemoryDocument(
                    preferences=("Prefer concise responses.",)
                ),
            )

            second_plane = bridge.host.planes.ensure(bridge.host.sessions)

            self.assertIs(second_plane, first_plane)
            self.assertNotIn(
                "Prefer concise responses.",
                second_plane.agent._context._system_prompt,
            )

            bridge.host.planes.close()
            third_plane = bridge.host.planes.ensure(bridge.host.sessions)

            self.assertIsNot(third_plane, first_plane)
            self.assertIn(
                "Prefer concise responses.",
                third_plane.agent._context._system_prompt,
            )
            self.assertIn(
                "current request has highest priority",
                third_plane.agent._context._system_prompt,
            )

    def test_rebuilds_runtime_when_the_configured_instruction_list_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "PROJECT.md").write_text("project rule", encoding="utf-8")
            bridge, _ = make_bridge([], root)
            bridge.open_session(open_session_message(root))
            first_plane = bridge.host.planes.ensure(bridge.host.sessions)
            config = json.loads(
                (root / "agent_config.json").read_text(encoding="utf-8")
            )
            config["workspace_instruction_files"] = ["PROJECT.md"]
            (root / "agent_config.json").write_text(
                json.dumps(config),
                encoding="utf-8",
            )

            second_plane = bridge.host.planes.ensure(bridge.host.sessions)

            self.assertIsNot(second_plane, first_plane)
            self.assertIn(
                "project rule", second_plane.agent._context._system_prompt
            )
            self.assertIn(
                "<workspace>/PROJECT.md",
                second_plane.agent._context._system_prompt,
            )

    def test_rebuilds_the_execution_plane_when_agent_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _ = make_bridge([], root)
            bridge.open_session(open_session_message(root))
            first_plane = bridge.host.planes.ensure(bridge.host.sessions)
            config = json.loads(
                (root / "agent_config.json").read_text(encoding="utf-8")
            )
            config["max_same_tool_calls"] = 6
            (root / "agent_config.json").write_text(
                json.dumps(config),
                encoding="utf-8",
            )

            with (
                patch.object(
                    first_plane.jobs,
                    "close",
                    wraps=first_plane.jobs.close,
                ) as close_jobs,
                patch.object(
                    first_plane.mcp,
                    "close",
                    wraps=first_plane.mcp.close,
                ) as close_mcp,
            ):
                second_plane = bridge.host.planes.ensure(bridge.host.sessions)

            self.assertIsNot(second_plane, first_plane)
            self.assertEqual(second_plane.agent_config.max_same_tool_calls, 6)
            close_jobs.assert_called_once_with()
            close_mcp.assert_called_once_with()

    def test_rebuilds_the_execution_plane_when_provider_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _ = make_bridge([], root)
            bridge.open_session(open_session_message(root))
            first_plane = bridge.host.planes.ensure(bridge.host.sessions)
            config = json.loads(
                (root / "provider_config.json").read_text(encoding="utf-8")
            )
            config["providers"]["first"]["model"] = "openai/updated"
            (root / "provider_config.json").write_text(
                json.dumps(config),
                encoding="utf-8",
            )

            second_plane = bridge.host.planes.ensure(bridge.host.sessions)

            self.assertIsNot(second_plane, first_plane)
            self.assertNotEqual(
                second_plane.configuration_fingerprint,
                first_plane.configuration_fingerprint,
            )
            self.assertEqual(
                second_plane.agent._provider._model,
                "openai/updated",
            )

    def test_rebuilds_the_execution_plane_when_dotenv_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "main_agent": {"provider": "first"},
                "subagent": {"provider": ""},
                "providers": {
                    "first": {
                        "model": "openai/first",
                        "key": "${FIRST_KEY}",
                        "max_context_tokens": 1000,
                    }
                },
            }
            bridge, _ = make_bridge([], root)
            open_session_message(root, provider_config=config)
            (root / ".env").write_text("FIRST_KEY=first\n", encoding="utf-8")
            bridge.open_session({"type": "open_session", "workspace": str(root), "session_id": None})
            first_plane = bridge.host.planes.ensure(bridge.host.sessions)

            (root / ".env").write_text("FIRST_KEY=second\n", encoding="utf-8")
            second_plane = bridge.host.planes.ensure(bridge.host.sessions)

            self.assertIsNot(second_plane, first_plane)
            self.assertEqual(second_plane.agent._provider._api_key, "second")

    def test_failed_assembly_does_not_publish_a_partial_execution_plane(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, _ = make_bridge([], root)
            bridge.open_session(open_session_message(root))

            with (
                patch(
                    "agent_runtime.plane_manager.Agent",
                    side_effect=RuntimeError("assembly failed"),
                ),
                patch(
                    "agent_runtime.plane_manager.JobManager.close",
                    autospec=True,
                ) as close_jobs,
                patch(
                    "agent_runtime.plane_manager.McpClientManager.close",
                    autospec=True,
                ) as close_mcp,
            ):
                with self.assertRaisesRegex(RuntimeError, "assembly failed"):
                    bridge.host.planes.ensure(bridge.host.sessions)

            self.assertIsNone(bridge.host.planes.current)
            close_jobs.assert_called_once()
            close_mcp.assert_called_once()

    def test_context_window_event_updates_the_execution_plane_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge, stdout = make_bridge([], root)
            bridge.open_session(open_session_message(root))
            plane = bridge.host.planes.ensure(bridge.host.sessions)

            bridge.host.publish_event(
                ContextWindowEvent(
                    ContextWindow(
                        input_tokens=240,
                        max_input_tokens=900,
                        max_context_tokens=1000,
                        output_reserve_tokens=100,
                        compression_threshold=720,
                        compression_count=3,
                    )
                ),
                "t1",
            )
            bridge._emit_runtime_state("running")

            self.assertEqual(plane.context_window.input_tokens, 240)
            self.assertEqual(
                emitted(stdout)[-1]["context_window"],
                {
                    "input_tokens": 240,
                    "max_input_tokens": 900,
                    "max_context_tokens": 1000,
                    "output_reserve_tokens": 100,
                    "compression_threshold": 720,
                    "compression_count": 3,
                },
            )

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
            bridge, stdout = make_bridge([], root)

            bridge.open_session(open_session_message(root))
            plane = bridge.host.planes.ensure(bridge.host.sessions)

            runtime_state = emitted(stdout)[-1]
            names = [item.name for item in plane.agent._tools.definitions]

        self.assertEqual(runtime_state["type"], "runtime_state")
        self.assertEqual(runtime_state["phase"], "starting")
        self.assertEqual(len(plane.runtime_warnings), 1)
        self.assertIn("Skipping skill at", plane.runtime_warnings[0])
        self.assertIn("read_skill", names)


class SubagentRoleStartTest(unittest.TestCase):
    """Only enabled roles reach the model's subagent schema."""

    def offered_roles(self, roles: dict) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _ = make_bridge([], Path(directory))
            bridge.open_session(
                open_session_message(
                    Path(directory),
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
                        "main_agent": {"tools": {"subagent": True}},
                        "subagent_roles": roles,
                    },
                )
            )
            plane = bridge.host.planes.ensure(bridge.host.sessions)
            subagents = plane.agent._tools._context.subagents
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
            bridge, _ = make_bridge([], Path(directory))
            bridge.open_session(
                open_session_message(
                    Path(directory),
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
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
            plane = bridge.host.planes.ensure(bridge.host.sessions)

            self.assertEqual(
                [definition.name for definition in plane.agent._tools.definitions],
                ["ask_user", "update_plan", "remember"],
            )

    def test_runtime_tools_are_registered_only_for_the_main_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge, _ = make_bridge([], Path(directory))
            bridge.open_session(
                open_session_message(
                    Path(directory),
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
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
            plane = bridge.host.planes.ensure(bridge.host.sessions)

            main_names = {
                definition.name for definition in plane.agent._tools.definitions
            }
            subagent = plane.agent._tools._context.subagents
            assert subagent is not None
            role = subagent.roles.get("researcher")
            role_context = plane.agent._tools._context
            role_names = {
                definition.name
                for definition in subagent._catalog.select(
                    role.tools, role_context
                ).definitions
            }

            self.assertIn("ask_user", main_names)
            self.assertIn("update_plan", main_names)
            self.assertNotIn("ask_user", role_names)
            self.assertNotIn("update_plan", role_names)
            self.assertNotIn("remember", role_names)


class PluginAgentStartTest(unittest.TestCase):
    def test_assembles_plugin_agents_with_prompts_tools_and_providers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugins" / "review-kit"
            agents = plugin / "agents"
            agents.mkdir(parents=True)
            (plugin / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "review-kit",
                        "components": {
                            "agents": [
                                "agents/matched.md",
                                "agents/fallback.md",
                                "agents/overridden.md",
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            for name, model in (
                ("matched", "second"),
                ("fallback", "opus"),
                ("overridden", "inherit"),
            ):
                (agents / f"{name}.md").write_text(
                    "---\n"
                    f"name: {name}\n"
                    f"description: Review as {name}.\n"
                    f"model: {model}\n"
                    "---\n\n"
                    f"System instructions for {name}.\n",
                    encoding="utf-8",
                )
            bridge, _ = make_bridge([], root)
            bridge.open_session(
                open_session_message(
                    root,
                    agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "workspace_instruction_files": [
                            "CLAUDE.md",
                            "AGENTS.md",
                        ],
                        "main_agent": {"tools": {"subagent": True}},
                    },
                    provider_config={
                        "main_agent": {"provider": "first"},
                        "subagent": {"provider": "first"},
                        "subagent_roles": {
                            "review-kit:overridden": {"provider": "second"}
                        },
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
                    },
                )
            )

            plane = bridge.host.planes.ensure(bridge.host.sessions)
            runtime = plane.agent._tools._context.subagents
            assert runtime is not None
            matched = runtime.roles.get("review-kit:matched")
            fallback = runtime.roles.get("review-kit:fallback")
            overridden = runtime.roles.get("review-kit:overridden")

        self.assertEqual(matched.description, "Review as matched.")
        self.assertEqual(
            matched.instructions,
            "System instructions for matched.",
        )
        self.assertEqual(matched.tools, ROLE_TOOL_NAMES)
        self.assertEqual(matched.provider._model, "openai/second")
        self.assertEqual(fallback.provider._model, "openai/first")
        self.assertEqual(overridden.provider._model, "openai/second")


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
            bridge, _ = make_bridge([], Path(directory))
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
                            "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
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
                plane = bridge.host.planes.ensure(bridge.host.sessions)
                main_names = sorted(
                    d.name for d in plane.agent._tools.definitions
                )
                role = plane.agent._tools._context.subagents.roles.get(
                    "researcher"
                )
                role_names = sorted(
                    vision_aware_tool_names(
                        role.tools, role.provider, role.vision_provider
                    )
                )
        return main_names, role_names

    def test_derives_the_single_image_tool_from_capabilities(self) -> None:
        cases = (
            ("vision model", VISION_MODEL, "seeing", "read_image"),
            ("text model with route", TEXT_MODEL, "seeing", "analyze_image"),
            ("text model without route", TEXT_MODEL, None, None),
        )
        for label, model, vision_provider, expected in cases:
            with self.subTest(label=label):
                main, role = self.tool_names(model, vision_provider)
                for names in (main, role):
                    image_tools = {"read_image", "analyze_image"} & set(names)
                    if expected is None:
                        self.assertEqual(image_tools, set())
                    else:
                        self.assertEqual(image_tools, {expected})

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
            bridge, _ = make_bridge([], Path(directory))
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
                        "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
                        "main_agent": {"tools": {"subagent": True}},
                        "subagent_roles": agent_roles,
                    },
                )
            )
            bridge.host.planes.ensure(bridge.host.sessions)

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

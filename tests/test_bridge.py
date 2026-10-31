import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.provider_fixtures import capabilities_stub, TEXT_MODEL, VISION_MODEL
from tests.bridge_fixtures import BridgeTestCase, emitted, open_session_message

from agent_core import (
    AgentRunResult,
    CacheUsage,
    TokenUsage,
    AssistantMessageDeltaEvent,
    ProviderCapabilities,
    AssistantMessageEvent,
    ModelCompletedEvent,
    CompressionCompletedEvent,
    ContextWindow,
    ContextWindowEvent,
    ExecutionScope,
    JsonlSessionStore,
    JobStatusEvent,
    Message,
    LLMResponse,
    MemoryCandidate,
    MemoryDocument,
    MemoryStore,
    PermissionPreset,
    PlanManager,
    PlanSnapshot,
    PlanStep,
    ReasoningDeltaEvent,
    Session,
    SessionInUseError,
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
from agent_core.usage import usage_to_dict
from agent_core.session_paths import session_directory
from agent_core.projection import project_context_units
from agent_core.providers import ModelProvider
from agent_core.subagent import vision_aware_tool_names
from agent_core.tools.config import ROLE_TOOL_NAMES, TOOL_NAMES
from interfaces.bridge.bridge import Cancelled
from tests.bridge_fixtures import Bridge
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
    inactive_runtime_state,
    session_ready_message,
    runtime_state_message,
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
        from tests.image_fixtures import png_bytes

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
    def test_initial_runtime_state_contract_and_independent_arrays(self) -> None:
        expected = {
            "type": "runtime_state", "phase": "inactive", "turn_id": None,
            "approval": None, "question": None, "provider": None,
            "permission_preset": "ask_for_approval", "context_window": None,
            "cache_usage": None, "jobs": [], "runtime_warnings": [], "plan": None,
        }
        first = inactive_runtime_state()
        self.assertEqual(first, expected)
        first["jobs"].append({"job_id": "other-session"})
        first["runtime_warnings"].append("other-session warning")
        self.assertEqual(inactive_runtime_state(), expected)
        self.assertEqual(runtime_state_message(phase="starting"), {**expected, "phase": "starting"})

    def test_initial_runtime_state_preserves_attachment_context(self) -> None:
        totals = {"input_tokens": 100, "cache_read_tokens": 60}
        state = inactive_runtime_state(
            provider="second", workspace="/workspace", cache_usage=totals, event_sequence=0,
        )
        self.assertEqual(state, {
            **inactive_runtime_state(), "provider": "second", "workspace": "/workspace",
            "cache_usage": totals, "event_sequence": 0,
        })

    def test_encodes_only_compression_cache_totals(self):
        for totals in (CacheUsage(2000, 800), None):
            with self.subTest(totals=totals):
                self.assertEqual(event_to_message(CompressionCompletedEvent(totals), "t"), {
                    "type": "compression_completed", "turn_id": "t",
                    "cache_usage": {"input_tokens": 2000, "cache_read_tokens": 800} if totals else None,
                })

    def test_invalid_tool_arguments_and_error_are_transmitted_with_the_call(self):
        raw = '{"command" "bad"}'
        call = ToolCall.from_raw_arguments("bad-call", "shell", raw)
        for event, key in (
            (ToolBatchStartedEvent(1, (call,)), "tool_calls"),
            (ToolCallEvent(call, 1, 1), "tool_call"),
        ):
            with self.subTest(key=key):
                message = event_to_message(event, "t")
                payload = message[key][0] if key == "tool_calls" else message[key]
                self.assertEqual(payload["arguments"], raw)
                self.assertEqual(payload["argument_error"]["type"], "invalid_tool_arguments")
                self.assertEqual(payload["id"], call.id)

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

    def test_encodes_model_completed_usage_including_unreported_usage(self) -> None:
        for usage, expected in (
            (TokenUsage(1000, 20, 1020, 800, None), {
                "input_tokens": 1000, "output_tokens": 20, "total_tokens": 1020,
                "cache_read_tokens": 800, "cache_write_tokens": None,
            }),
            (None, None),
        ):
            with self.subTest(usage=usage):
                self.assertEqual(event_to_message(ModelCompletedEvent(2, usage, CacheUsage(2000, 1500)), "t1"), {
                    "type": "model_completed", "turn_id": "t1", "model_call_index": 2,
                    "usage": expected,
                    "cache_usage": {"input_tokens": 2000, "cache_read_tokens": 1500},
                })

    def test_encodes_context_window(self) -> None:
        self.assertEqual(
            event_to_message(
                ContextWindowEvent(
                    ContextWindow(
                        input_tokens=120,
                        system_prompt_tokens=40,
                        max_input_tokens=900,
                        max_context_tokens=1000,
                        output_reserve_tokens=100,
                        compression_threshold=720,
                        compression_count=2,
                        compression_summary="Current summary",
                    )
                ),
                "t1",
            ),
            {
                "type": "context_window",
                "turn_id": "t1",
                "input_tokens": 120,
                "system_prompt_tokens": 40,
                "max_input_tokens": 900,
                "max_context_tokens": 1000,
                "output_reserve_tokens": 100,
                "compression_threshold": 720,
                "compression_count": 2,
                "compression_summary": "Current summary",
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
                JobStatusEvent("job-1", "shell", "running", {"command": "npm test"}), "t1"
            ),
            {
                "type": "job_status",
                "turn_id": "t1",
                "job_id": "job-1",
                "kind": "shell",
                "status": "running",
                "details": {"command": "npm test"},
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

    def test_tool_result_preserves_partial_patch_failure_details(self) -> None:
        details = {
            "completed": [{"path": "a.txt", "operation": "updated"}],
            "failed": {"path": "b.txt", "operation": "updated"},
            "pending": [],
        }
        message = event_to_message(ToolResultEvent(
            tool_result=ToolResult("patch", "apply_patch", error=ToolError(
                "PatchApplyError", "locked", details,
            )),
            tool_index=1, tool_count=1,
        ), "t1")
        self.assertFalse(message["ok"])
        self.assertEqual(message["error"]["details"], details)

    def test_usage_to_dict(self) -> None:
        self.assertIsNone(usage_to_dict(None))
        self.assertEqual(
            usage_to_dict(
                TokenUsage(1, 2, 3, 0, None)
            ),
            {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3,
             "cache_read_tokens": 0, "cache_write_tokens": None},
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
                "protocol_version": 3,
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
                cache_usage=None,
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
                "cache_usage": None,
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


class IsolatedBridgeTest(BridgeTestCase):
    def test_unopened_snapshot_preserves_failure_phase_and_warnings(self) -> None:
        bridge, stdout = self.make_bridge([])
        bridge._emit_runtime_state("failed", runtime_warnings=("Unavailable",))
        state = emitted(stdout)[-1]
        self.assertEqual(state["phase"], "failed")
        self.assertEqual(state["runtime_warnings"], ["Unavailable"])
        self.assertEqual(state["permission_preset"], inactive_runtime_state()["permission_preset"])

    def test_bridges_own_distinct_configuration_and_scheduler_storage(self) -> None:
        first, _ = self.make_bridge([])
        second, _ = self.make_bridge([])
        self.assertNotEqual(first.host.config_directory, second.host.config_directory)
        for bridge in (first, second):
            self.assertTrue(bridge.host.config_directory.is_dir())
            self.assertEqual(bridge.host.scheduler.path.parent, bridge.host.config_directory)


class PermissionProtocolTest(BridgeTestCase):
    def test_file_writers_use_bridge_approval_and_preserve_denied_files(self) -> None:
        from tests.file_write_fixtures import write_call

        for name in ("apply_patch", "edit_file", "write_file"):
            for approved in (True, False):
                with self.subTest(tool=name, approved=approved):
                    directory = self.enterContext(tempfile.TemporaryDirectory())
                    root = Path(directory)
                    target = root / "file.txt"
                    target.write_bytes(b"old\n")
                    bridge, stdout = self.make_bridge([
                        json.dumps({"type": "approval_response", "request_id": "t1:1", "approved": approved}),
                    ], root)
                    bridge.open_session(open_session_message(root, session_id="s", agent_config={
                        "max_same_tool_calls": 5,
                        "output_reserve_tokens": 100,
                        "workspace_instruction_files": [],
                        "main_agent": {"tools": {name: True}},
                    }))
                    bridge.host.turns.turn_id = "t1"
                    plane = bridge.host.planes.ensure(bridge.host.sessions)

                    result = plane.agent._tools.execute(write_call(name))

                    approval = next(message for message in emitted(stdout) if message["type"] == "approval_request")
                    self.assertEqual(approval["kind"], "file")
                    self.assertEqual(approval["tool_name"], name)
                    self.assertIn(name, approval["command"])
                    self.assertEqual(result.error is None, approved)
                    self.assertEqual(target.read_bytes(), b"new\n" if approved else b"old\n")

    def test_bridge_accepts_workspace_access_before_runtime_start(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, stdout = self.make_bridge([], root)
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
            {key: emitted(stdout)[-1][key] for key in ("type", "preset")},
            {"type": "permission_changed", "preset": "workspace_access"},
        )

    def test_bridge_updates_permission_before_runtime_initialization(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, stdout = self.make_bridge([], root)
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
            JsonlSessionStore(root / "sessions").load("s").permission_preset,
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
            {key: emitted(stdout)[-1][key] for key in ("type", "preset")},
            {"type": "permission_changed", "preset": "full_access"},
        )

    def test_workspace_access_routes_shell_without_host_approval(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        if sys.platform == "win32":
            user = "*" + subprocess.run(
                ["whoami", "/user", "/fo", "csv", "/nh"],
                capture_output=True, check=True,
            ).stdout.split(b",")[-1].strip(b'"\r\n ').decode("ascii")
            subprocess.run(
                ["icacls", str(root), "/grant", f"{user}:(OI)(CI)F"],
                capture_output=True, check=True,
            )
        bridge, stdout = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        next_workspace = root / "next"
        next_workspace.mkdir()
        bridge, stdout = self.make_bridge([], root)
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
            JsonlSessionStore(root / "sessions").load("s").permission_preset,
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        next_workspace = root / "next"
        next_workspace.mkdir()
        bridge, _ = self.make_bridge([], root)
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


class BridgeApprovalTest(BridgeTestCase):
    def test_runtime_state_contains_only_the_jobs_that_are_running(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        bridge, stdout = self.make_bridge([], Path(directory))
        bridge.open_session(
            open_session_message(Path(directory), session_id="s")
        )
        bridge._emit_agent_event(JobStatusEvent("j1", "shell", "running", {"command": "npm test"}), "t1")
        bridge._emit_agent_event(JobStatusEvent("j2", "subagent", "running", {"role": "researcher", "task": "inspect"}), "t1")
        bridge._emit_runtime_state("running")
        self.assertEqual(emitted(stdout)[-1]["jobs"][1]["details"],
                         {"role": "researcher", "task": "inspect"})
        bridge._emit_agent_event(JobStatusEvent("j2", "subagent", "completed", {"role": "researcher", "task": "inspect"}), "t1")
        bridge._emit_runtime_state("running")

        self.assertEqual(
            emitted(stdout)[-1]["jobs"],
            [{"job_id": "j1", "kind": "shell", "status": "running", "details": {"command": "npm test"}}],
        )

    def test_runtime_state_contains_the_pending_approval(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        bridge, stdout = self.make_bridge(
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
        bridge, stdout = self.make_bridge(
            ['{"type": "approval_response", "request_id": "None:1",'
             ' "approved": true}']
        )
        self.assertTrue(bridge.request_permission("ls"))
        self.assertEqual(
            next(message for message in emitted(stdout) if message["type"] == "approval_request")["command"],
            "ls",
        )

    def test_denies_when_response_is_false(self) -> None:
        bridge, _ = self.make_bridge(
            ['{"type": "approval_response", "request_id": "None:1",'
             ' "approved": false}']
        )
        self.assertFalse(bridge.request_permission("rm -rf /"))

    def test_buffers_interleaved_messages_and_replays_them(self) -> None:
        """A typed-ahead turn must not be consumed as the answer."""
        bridge, _ = self.make_bridge(
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
        bridge, _ = self.make_bridge(
            [
                '{"type": "approval_response", "request_id": "stale",'
                ' "approved": true}',
                '{"type": "approval_response", "request_id": "None:1",'
                ' "approved": false}',
            ]
        )
        self.assertFalse(bridge.request_permission("ls"))

    def test_cancels_when_input_ends(self) -> None:
        bridge, _ = self.make_bridge([])
        with self.assertRaises(Cancelled):
            bridge.request_permission("ls")

    def test_cancels_on_shutdown(self) -> None:
        bridge, _ = self.make_bridge(['{"type": "shutdown"}'])
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
        bridge, stdout = self.make_bridge(_SlowStdin(responses))
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
        bridge, stdout = self.make_bridge(stdin)
        self.addCleanup(stdin.release.set)
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
        bridge, _ = self.make_bridge(
            [
                '{"type": "approval_response", "request_id": "None:1", "approved": true}',
            ]
        )
        bridge._start_reader()
        bridge._reader.join(2)
        self.assertFalse(bridge._reader.is_alive())

        with self.assertRaises(Cancelled):
            bridge.request_permission("ls")

    def test_routes_cancel_for_a_turn_queued_before_run_turn(self) -> None:
        bridge, stdout = self.make_bridge(
            [
                '{"type": "user_turn", "turn_id": "t1", "text": "work"}',
                '{"type": "cancel", "turn_id": "t1"}',
            ]
        )

        message = bridge.read_message()
        assert message is not None
        bridge._reader.join(2)
        self.assertFalse(bridge._reader.is_alive())

        self.assertIn("t1", bridge.host.turns._pending_cancels)
        self.assertIn(
            ("user_turn_received", "t1"),
            [
                (event["type"], event.get("turn_id"))
                for event in emitted(stdout)
            ],
        )


class BridgeCancellationTest(BridgeTestCase):
    def test_input_cancels_interactions_in_a_worker_turn(self) -> None:
        for interaction in ("approval", "question"):
            for command in ('{"type":"cancel","turn_id":"t1"}',
                            '{"type":"shutdown"}', None):
                with self.subTest(interaction=interaction, command=command):
                    bridge, _ = self.make_bridge([command] if command is not None else [])
                    results = []
                    errors = []

                    class WaitingAgent:
                        def run(self, *args, **kwargs):
                            if interaction == "approval":
                                bridge.request_permission("command")
                            else:
                                bridge.request_user_choice("Question?", [], True)
                            raise AssertionError("cancelled interaction returned an answer")

                    def run():
                        try:
                            results.append(bridge.host.turns.run_turn(
                                "t1", "work",
                                lambda: SimpleNamespace(
                                    agent=WaitingAgent(), memory=None, runtime_warnings=(),
                                ),
                                lambda: (),
                            ))
                        except BaseException as error:
                            errors.append(error)

                    thread = threading.Thread(target=run, daemon=True)
                    thread.start()
                    try:
                        thread.join(3)
                        self.assertFalse(thread.is_alive())
                        self.assertEqual(errors, [])
                        self.assertEqual([result.status for result in results], ["cancelled"])
                        self.assertEqual(bridge._waiters, {})
                    finally:
                        bridge.close()
                        thread.join(3)

    def test_cancel_is_scoped_to_its_turn_and_bridge(self) -> None:
        first, _ = self.make_bridge([])
        second, _ = self.make_bridge([])
        try:
            for bridge in (first, second):
                bridge.host.turns.turn_id = "t1"
                bridge.host.turns.control = TurnControl()
            with patch.object(first, "_start_reader"), patch.object(second, "_start_reader"):
                current = first._register_interaction("t1:1", "t1")
                unrelated = first._register_interaction("other:1", "other")
                other_session = second._register_interaction("t1:1", "t1")
                first.host.turns.enqueue("queued")
                first.submit({"type": "cancel", "turn_id": "queued"})
                first.submit({"type": "cancel", "turn_id": "stale"})
                self.assertTrue(current.empty())
                first.submit({"type": "cancel", "turn_id": "t1"})
                self.assertIsInstance(current.get_nowait(), Cancelled)
                self.assertTrue(unrelated.empty())
                self.assertTrue(other_session.empty())
                self.assertFalse(second.host.turns.control.cancelled)
                late = first._register_interaction("t1:2", "t1")
                self.assertIsInstance(late.get_nowait(), Cancelled)
                second.submit({
                    "type": "approval_response", "request_id": "t1:1", "approved": True,
                })
                self.assertTrue(second._wait_for_interaction(other_session)["approved"])
        finally:
            first.close()
            second.close()


class BridgeUserQuestionTest(BridgeTestCase):
    OPTIONS = [
        {"id": "memory", "label": "Memory"},
        {"id": "sqlite", "label": "SQLite"},
    ]

    def test_runtime_state_contains_the_pending_question(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        bridge, stdout = self.make_bridge(
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
        bridge, stdout = self.make_bridge(
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
        bridge, _ = self.make_bridge(
            ['{"type": "user_question_response", "request_id": "None:1",'
             ' "text": "  Redis  "}']
        )

        self.assertEqual(
            bridge.request_user_choice("Cache?", self.OPTIONS, True),
            {"type": "text", "text": "Redis"},
        )

    def test_ignores_invalid_answers(self) -> None:
        bridge, _ = self.make_bridge(
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


class BridgeServeTest(BridgeTestCase):
    def test_lists_workspaces_without_changing_the_open_session(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        other = root / "other"
        other.mkdir()
        store = JsonlSessionStore(root / "sessions")
        for workspace, session_id in ((root, "root-history"), (other, "other-history")):
            session = Session(session_id)
            session.add_item("user", session_id)
            store.bind_workspace(session_id, workspace)
            store.append_events(session_id, session.journal, workspace=workspace)
        opening = open_session_message(root)
        bridge, output = self.make_bridge([
            json.dumps(opening),
            json.dumps({"type": "list_workspaces"}),
            json.dumps({"type": "list_sessions"}),
            json.dumps({"type": "shutdown"}),
        ], root)
        bridge.serve()
        messages = emitted(output)
        self.assertEqual([message["type"] for message in messages], [
            "session_ready", "runtime_state", "workspaces_listed", "sessions_listed",
        ])
        self.assertEqual(set(messages[2]["workspaces"]), {str(root.resolve()), str(other.resolve())})
        self.assertEqual(messages[3]["sessions"], [{"session_id": "root-history", "title": "root-history"}])
        self.assertEqual(bridge.host.sessions.workspace.path, root.resolve())
        self.assertIsNone(bridge.host.planes.current)

    def test_failed_workspace_open_allows_listing_and_opening_another_workspace(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        opening = open_session_message(root, workspace=str(root / "deleted"))
        bridge, output = self.make_bridge([
            json.dumps(opening),
            json.dumps({"type": "list_workspaces"}),
            json.dumps({**opening, "workspace": str(root)}),
            json.dumps({"type": "load_session"}),
            json.dumps({"type": "shutdown"}),
        ], root)
        bridge.serve()
        messages = emitted(output)
        self.assertEqual([message["type"] for message in messages], [
            "session_open_failed", "workspaces_listed", "session_ready", "runtime_state", "session_items",
        ])
        self.assertIsNone(messages[0]["session_id"])
        self.assertEqual(messages[0]["error"]["type"], "ValueError")
        self.assertEqual(messages[1]["workspaces"], [])
        self.assertEqual(messages[2]["workspace"], str(root.resolve()))
        self.assertEqual(messages[-1]["items"], [])

    def test_stops_on_shutdown(self) -> None:
        bridge, _ = self.make_bridge(['{"type": "shutdown"}'])
        bridge.serve()

    def test_stops_at_end_of_input(self) -> None:
        bridge, _ = self.make_bridge([])
        bridge.serve()

    def test_does_not_swallow_an_external_keyboard_interrupt(self) -> None:
        bridge, _ = self.make_bridge([])
        with patch.object(bridge, "read_message", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                bridge.serve()

    def test_rejects_turn_before_open_session(self) -> None:
        bridge, _ = self.make_bridge(
            ['{"type": "user_turn", "turn_id": "t1", "text": "hi"}']
        )
        with self.assertRaises(SystemExit):
            bridge.serve()

    def test_reports_malformed_input_as_fatal(self) -> None:
        bridge, stdout = self.make_bridge(["not json"])
        with self.assertRaises(SystemExit):
            bridge.serve()
        self.assertEqual(emitted(stdout)[0]["type"], "fatal")

    def test_answers_the_workspace_sessions_and_the_stored_conversation(
        self,
    ) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
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

        bridge, stdout = self.make_bridge(
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, stdout = self.make_bridge(
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
            JsonlSessionStore(root / "sessions").load(
                "empty"
            ).permission_preset,
            PermissionPreset.FULL_ACCESS,
        )
        self.assertIsNone(bridge.host.planes.current)

    def test_answers_settings_requests_without_starting_runtime(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, stdout = self.make_bridge(
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
        input_source: str = "user",
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
        input_source: str = "user",
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
        input_source: str = "user",
    ) -> object:
        self._session.begin_turn(turn_id)
        self._session.add_item("user", user_input)
        self._session.add_item("assistant", "done")
        self._session.finish_turn("completed", turn_id)
        return AgentRunResult("done")


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


class InterruptedTurnTest(BridgeTestCase):
    """A turn keeps the transcript it produced before it ended early."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bridge, self.stdout = self.make_bridge([], self.root)
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

    def test_checkpoints_bind_transcript_to_the_bridge_event_cursor(self) -> None:
        plane = self.bridge.host.planes.ensure(self.bridge.host.sessions)
        plane.agent = SuccessfulAgent(self.session)
        for turn_id in ("t1", "t2"):
            self.bridge.run_turn({"turn_id": turn_id, "text": turn_id})
        messages = emitted(self.stdout)
        self.assertEqual(
            [message["event_sequence"] for message in messages],
            list(range(1, len(messages) + 1)),
        )
        first, second = [message for message in messages if message["type"] == "turn_completed"]
        for message in (first, second):
            self.assertEqual(message["transcript"]["event_sequence"], message["event_sequence"])
            self.assertEqual(message["runtime"]["phase"], "idle")
            self.assertIsNone(message["runtime"]["turn_id"])
        self.assertEqual([item["content"] for item in second["transcript"]["items"]],
                         ["t1", "done", "t2", "done"])
        self.bridge.emit("fatal", error={"type": "TestError", "message": "failed", "details": {}})
        fatal = emitted(self.stdout)[-1]
        self.assertEqual(fatal["resume_after"], second["event_sequence"])
        self.assertNotIn("transcript", fatal)
        self.assertEqual(fatal["runtime"]["phase"], "failed")

    def test_turn_completed_preserves_cache_usage_from_the_model_call(self) -> None:
        self.session.begin_turn("previous")
        self.session.model_completed(1, usage=TokenUsage(100, 10, 110, 50, None),
                                     has_tool_calls=False)
        self.session.finish_turn("completed")
        plane = self.bridge.host.planes.ensure(self.bridge.host.sessions)
        plane.memory = MemorySpy()
        emit = self.bridge.emit

        def check_model_completion(type, **fields):
            if type == "model_completed":
                self.assertEqual(self.session.turns["t1"].status, "running")
                self.assertFalse(any(message["type"] == "turn_completed" for message in emitted(self.stdout)))
                self.assertEqual(self.session.journal[-1].event_type, "model_completed")
            emit(type, **fields)

        with (
            patch.object(self.bridge, "emit", side_effect=check_model_completion),
            patch.object(plane.provider, "count_input_tokens", return_value=1),
            patch.object(plane.provider, "stream_cancellable", return_value=LLMResponse(
                content="done", usage=TokenUsage(1000, 20, 1020, 800, None),
            )),
        ):
            self.bridge.run_turn({"turn_id": "t1", "text": "do the work"})

        message = emitted(self.stdout)[-1]
        self.assertEqual(message["type"], "turn_completed")
        self.assertEqual(message["usage"], {
            "input_tokens": 1000, "output_tokens": 20, "total_tokens": 1020,
            "cache_read_tokens": 800, "cache_write_tokens": None,
        })
        stored = self.store.load(self.session_id)
        call = [event for event in stored.journal if event.event_type == "model_completed"][-1]
        self.assertEqual(call.payload["usage"], message["usage"])
        completed = [item for item in emitted(self.stdout) if item["type"] == "model_completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["turn_id"], "t1")
        self.assertEqual(completed[0]["model_call_index"], 1)
        self.assertEqual(completed[0]["usage"], message["usage"])
        totals = {"input_tokens": 1100, "cache_read_tokens": 850}
        self.assertEqual(completed[0]["cache_usage"], totals)
        self.assertEqual(completed[0]["runtime"]["cache_usage"], totals)
        self.assertLess(completed[0]["event_sequence"], message["event_sequence"])
        workspace = str(self.bridge.host.sessions.workspace.path)
        self.bridge.open_session({"workspace": workspace, "session_id": self.session_id})
        self.assertEqual(emitted(self.stdout)[-1]["cache_usage"], totals)

    def test_running_checkpoint_contains_new_user_input_and_prior_turns(self) -> None:
        plane = self.bridge.host.planes.ensure(self.bridge.host.sessions)
        plane.agent = SuccessfulAgent(self.session)
        self.bridge.run_turn({"turn_id": "t1", "text": "first"})
        previous_cursor = emitted(self.stdout)[-1]["event_sequence"]
        session = self.session

        class StreamingAgent:
            def run(self, text, *, turn_id, on_event, **kwargs):
                session.begin_turn(turn_id)
                session.add_item("user", text)
                on_event(ContextWindowEvent(plane.context_window))
                on_event(AssistantMessageDeltaEvent("partial", 1))
                session.add_item("assistant", "done")
                session.finish_turn("completed", turn_id)
                return AgentRunResult("done")

        plane.agent = StreamingAgent()
        self.bridge.run_turn({"turn_id": "t2", "text": "second"})
        messages = emitted(self.stdout)
        context = next(message for message in messages if message["type"] == "context_window")
        self.assertGreater(context["transcript"]["event_sequence"], previous_cursor)
        self.assertEqual(context["runtime"]["phase"], "running")
        self.assertEqual([item["content"] for item in context["transcript"]["items"]],
                         ["first", "done", "second"])
        delta = next(message for message in messages if message["type"] == "assistant_delta")
        self.assertNotIn("transcript", delta)
        self.assertGreater(delta["event_sequence"], context["event_sequence"])

    def test_compression_cache_usage_is_persisted_pushed_and_restored(self) -> None:
        plane = self.bridge.host.planes.ensure(self.bridge.host.sessions)
        plane.memory = MemorySpy()
        for index in range(8):
            self.session.add_item("user", f"request {index}")
            self.session.add_item("assistant", f"answer {index}")
        emit = self.bridge.emit

        def check_completion(type, **fields):
            if type == "compression_completed":
                self.assertEqual(self.session.turns["t1"].status, "running")
                stored = self.store.load(self.session_id, recover=False)
                self.assertEqual(stored.journal[-1].event_type, "compression_completed")
                self.assertEqual(stored.journal[-1].payload["attempt"], len([
                    message for message in emitted(self.stdout) if message["type"] == "compression_completed"
                ]) + 1)
                self.assertEqual(stored.journal[-1].payload["model"], "openai/first")
                self.assertFalse(any(message["type"] == "model_completed" for message in emitted(self.stdout)))
            emit(type, **fields)

        with (
            patch.object(self.bridge, "emit", side_effect=check_completion),
            patch.object(plane.provider, "count_input_tokens", side_effect=lambda _request:
                         800 if self.session.archived_summary is None else 1),
            patch.object(plane.provider, "stream_cancellable", side_effect=[
                LLMResponse(content="invalid checkpoint", usage=TokenUsage(800, 20, 820, 600, None)),
                LLMResponse(content="- [durable] Prior work.", usage=TokenUsage(800, 30, 830, 700, None)),
                LLMResponse(content="done", usage=TokenUsage(100, 10, 110, 50, None)),
            ]),
        ):
            self.bridge.run_turn({"turn_id": "t1", "text": "continue"})

        attempts = [message for message in emitted(self.stdout) if message["type"] == "compression_completed"]
        self.assertEqual(len(attempts), 2)
        for message, totals in zip(attempts, (
            {"input_tokens": 800, "cache_read_tokens": 600},
            {"input_tokens": 1600, "cache_read_tokens": 1300},
        )):
            self.assertEqual(message["cache_usage"], totals)
            self.assertEqual(message["runtime"]["cache_usage"], totals)
            self.assertNotIn("model_call_index", message)
        stored = self.store.load(self.session_id)
        calls = [event.payload for event in stored.journal if event.event_type == "compression_completed"]
        self.assertEqual([(call["attempt"], call["status"]) for call in calls],
                         [(1, "invalid_response"), (2, "completed")])
        self.assertEqual(stored.cache_usage, CacheUsage(1700, 1350))
        completed = emitted(self.stdout)[-1]
        self.assertEqual(completed["type"], "turn_completed")
        self.assertEqual(completed["usage"]["input_tokens"], 100)
        self.bridge.open_session({"workspace": str(self.root), "session_id": self.session_id})
        self.assertEqual(emitted(self.stdout)[-1]["cache_usage"],
                         {"input_tokens": 1700, "cache_read_tokens": 1350})

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

        messages = emitted(self.stdout)
        self.assertEqual(messages[-2]["type"], "runtime_state")
        self.assertEqual(messages[-2]["phase"], "idle")
        self.assertEqual(messages[-1]["type"], "turn_cancelled")
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
        messages = emitted(self.stdout)
        self.assertEqual(messages[-2]["type"], "runtime_state")
        self.assertEqual(messages[-2]["phase"], "idle")
        self.assertEqual(messages[-1]["type"], "turn_completed")

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

        messages = emitted(self.stdout)
        self.assertEqual(messages[-2]["type"], "runtime_state")
        self.assertEqual(messages[-2]["phase"], "idle")
        failure = messages[-1]
        self.assertEqual(failure["type"], "turn_failed")
        self.assertEqual(failure["error"]["type"], "ValueError")
        self.assertEqual(
            [message.content for message in self.stored_items()],
            ["do the work"],
        )

    def test_journal_records_a_turn_that_produced_nothing(self) -> None:
        self.start_turn(FailingAgent(self.session))

        messages = emitted(self.stdout)
        self.assertEqual(messages[-2]["type"], "runtime_state")
        self.assertEqual(messages[-2]["phase"], "idle")
        failure = messages[-1]
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
        storage = patch(
            "agent_core.session_store._write_journal_events",
            side_effect=ValueError("session already exists in workspace: 'x'"),
        )
        storage.start()
        self.addCleanup(storage.stop)

        self.start_turn(ScriptedAgent(self.session, error=KeyboardInterrupt()))

        messages = emitted(self.stdout)
        self.assertEqual(messages[-2]["type"], "runtime_state")
        self.assertEqual(messages[-2]["phase"], "idle")
        failure = messages[-1]
        self.assertEqual(failure["type"], "turn_failed")

        # The bridge keeps serving turns once storage recovers.
        storage.stop()
        self.start_turn(
            ScriptedAgent(self.session, error=KeyboardInterrupt()),
            text="second try",
        )
        messages = emitted(self.stdout)
        self.assertEqual(messages[-2]["type"], "runtime_state")
        self.assertEqual(messages[-2]["phase"], "idle")
        self.assertEqual(messages[-1]["type"], "turn_cancelled")
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
                "tests.bridge_fixtures.default_config_directory",
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
                    return AgentRunResult("scheduled answer")

            def execution_plane(manager, state, **kwargs) -> object:
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
                "tests.bridge_fixtures.default_config_directory",
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


class BridgeSessionOpenTest(BridgeTestCase):
    """The bridge selects the provider the interface asked for."""

    @unittest.skipUnless(sys.platform == "win32", "Windows journal ownership")
    def test_occupied_switch_keeps_original_writer_and_runtime_usable(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        opening = open_session_message(root, session_id="gui-session")
        gui, _ = self.make_bridge([], root)
        gui.open_session(opening)
        gui.host.sessions.session.add_item("user", "GUI history")
        tui, output = self.make_bridge([], root)
        tui.open_session({**opening, "session_id": "tui-session"})
        original = tui.host.sessions.session
        original.add_item("user", "TUI history")
        tui.host.set_permission_preset(PermissionPreset.FULL_ACCESS)
        tui.host.sessions.plan.update("original plan", (PlanStep("1", "first step", "pending"),))
        plane = tui.host.planes.ensure(tui.host.sessions)
        plane.agent = SuccessfulAgent(original)
        tui._emit_runtime_state("idle")
        permissions, plan = tui.host.sessions.permissions, tui.host.sessions.plan
        with patch.object(plane.jobs, "close", wraps=plane.jobs.close) as close_jobs:
            with patch.object(tui, "read_message", side_effect=[
                opening, {"type": "load_session"},
                {"type": "user_turn", "turn_id": "continued", "text": "continue TUI"}, None,
            ]):
                tui.serve()
            close_jobs.assert_not_called()
        failed = next(message for message in emitted(output) if message["type"] == "session_open_failed")
        self.assertEqual(failed["runtime"]["phase"], "idle")
        self.assertEqual(failed["runtime"]["permission_preset"], "full_access")
        self.assertEqual(failed["runtime"]["plan"]["goal"], "original plan")
        self.assertIs(tui.host.sessions.session, original)
        self.assertIs(tui.host.sessions.permissions, permissions)
        self.assertIs(tui.host.sessions.plan, plan)
        self.assertIs(tui.host.planes.current, plane)
        self.assertEqual(emitted(output)[-1]["type"], "turn_completed")
        self.assertEqual([item.content for item in tui.host.sessions.store.load("tui-session").items],
                         ["TUI history", "continue TUI", "done"])
        self.assertEqual([item.content for item in gui.host.sessions.store.load("gui-session").items], ["GUI history"])
        for session_id in ("gui-session", "tui-session"):
            with self.assertRaises(SessionInUseError):
                with JsonlSessionStore(root / "sessions").open_journal(session_id):
                    pass

    def test_failed_target_preparation_preserves_the_current_binding(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bridge, _ = self.make_bridge([], root)
        opening = open_session_message(root, session_id="original")
        bridge.open_session(opening)
        original = bridge.host.sessions.session
        original.add_item("user", "original history")
        permissions, plan = bridge.host.sessions.permissions, bridge.host.sessions.plan
        for change in ({"workspace": str(root / "missing")}, {"provider": "unknown"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                bridge.open_session({**opening, "session_id": "target", **change})
            self.assertIs(bridge.host.sessions.session, original)
            self.assertIs(bridge.host.sessions.permissions, permissions)
            self.assertIs(bridge.host.sessions.plan, plan)
        with patch.object(JsonlSessionStore, "load", side_effect=ValueError("invalid target journal")):
            with self.assertRaisesRegex(ValueError, "invalid target journal"):
                bridge.open_session({**opening, "session_id": "target"})
        self.assertIs(bridge.host.sessions.session, original)
        with JsonlSessionStore(root / "sessions").open_journal("target"):
            pass
        original.add_item("user", "still writable")
        self.assertEqual([item.content for item in bridge.host.sessions.store.load("original").items],
                         ["original history", "still writable"])
        bridge.open_session(opening)
        self.assertEqual(bridge.host.sessions.session.session_id, "original")
        bridge.host.sessions.session.add_item("user", "reselected current session")
        self.assertEqual(len(bridge.host.sessions.store.load("original").items), 3)

    def test_new_workspace_session_accepts_a_turn_and_preserves_both_workspaces_history(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        other = root / "other"
        other.mkdir()
        opening = open_session_message(root)
        bridge, output = self.make_bridge([], root)
        bridge.open_session(opening)
        original = bridge.host.sessions.session
        original.add_item("user", "original history")
        bridge.host.set_permission_preset(PermissionPreset.FULL_ACCESS)
        bridge.host.sessions.plan.update("original plan", (PlanStep("1", "original step", "pending"),))
        bridge.open_session({**opening, "workspace": str(other)})
        fresh = bridge.host.sessions.session
        self.assertNotEqual(fresh.session_id, original.session_id)
        self.assertEqual(fresh.items, [])
        self.assertEqual(fresh.permission_preset, PermissionPreset.ASK_FOR_APPROVAL)
        self.assertIsNone(bridge.host.sessions.plan.snapshot)
        self.assertEqual(bridge.host.sessions.store.workspace_for(original.session_id), str(root.resolve()))

        plane = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertEqual(plane.agent._execution_context.workspace.path, other.resolve())
        self.assertIs(plane.agent._execution_context.session, fresh)
        plane.agent = SuccessfulAgent(fresh)
        bridge.run_turn({"turn_id": "new-workspace-turn", "text": "hello new workspace"})
        self.assertEqual(emitted(output)[-1]["type"], "turn_completed")
        stored = bridge.host.sessions.store.load(fresh.session_id)
        self.assertEqual(stored.workspace, str(other.resolve()))
        self.assertEqual([item.content for item in stored.items], ["hello new workspace", "done"])
        bridge._emit_sessions()
        self.assertEqual(emitted(output)[-1]["sessions"][0]["session_id"], fresh.session_id)

        bridge.open_session({**opening, "workspace": str(other), "session_id": original.session_id})
        self.assertEqual(bridge.host.sessions.workspace.path, root.resolve())
        self.assertEqual(bridge.host.sessions.session.items[0].content, "original history")
        self.assertEqual(bridge.host.sessions.session.permission_preset, PermissionPreset.FULL_ACCESS)
        self.assertEqual(bridge.host.sessions.plan.snapshot.goal, "original plan")

    def test_switching_sessions_reuses_resources_and_rebinds_all_session_state(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bridge, output = self.make_bridge([], root)
        opening = open_session_message(root)
        bridge.open_session(opening)
        first_session = bridge.host.sessions.session
        first_session.add_item("user", "first conversation")
        bridge.host.set_permission_preset(PermissionPreset.FULL_ACCESS)
        bridge.host.sessions.plan.update("first plan", (PlanStep("1", "first step", "pending"),))
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        first_agent, first_jobs = plane.agent, plane.jobs
        with (
            patch.object(plane.mcp, "start") as start_mcp,
            patch.object(plane.mcp, "close") as close_mcp,
            patch.object(first_jobs, "close", wraps=first_jobs.close) as close_jobs,
        ):
            bridge.open_session(opening)
            self.assertIsNone(plane.agent)
            self.assertIsNone(emitted(output)[-1]["context_window"])
            rebound = bridge.host.planes.ensure(bridge.host.sessions)
            self.assertIs(rebound, plane)
            self.assertIsNot(rebound.agent, first_agent)
            self.assertIsNot(rebound.jobs, first_jobs)
            context = rebound.agent._execution_context
            self.assertIs(context.session, bridge.host.sessions.session)
            self.assertEqual(context.session.items, [])
            self.assertIsNone(context.plan.snapshot)
            self.assertIs(rebound.agent._tools._policy, bridge.host.sessions.permissions)
            self.assertEqual(context.execution_router.authority, bridge.host.sessions.permissions.authority)
            self.assertNotEqual(context.execution_router.authority, FULL_ACCESS_AUTHORITY)
            close_jobs.assert_called_once()
            start_mcp.assert_not_called()
            close_mcp.assert_not_called()

        bridge.open_session({**opening, "session_id": first_session.session_id})
        restored = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIs(restored, plane)
        self.assertEqual(restored.agent._execution_context.session.items[0].content, "first conversation")
        self.assertEqual(restored.agent._execution_context.plan.snapshot.goal, "first plan")
        self.assertEqual(restored.execution_router.authority, FULL_ACCESS_AUTHORITY)

    def test_switching_sessions_rebuilds_resources_for_a_different_workspace_or_provider(self) -> None:
        for change in ("workspace", "provider"):
            with self.subTest(change=change):
                root = Path(self.enterContext(tempfile.TemporaryDirectory()))
                bridge, _ = self.make_bridge([], root)
                opening = open_session_message(root)
                bridge.open_session(opening)
                first = bridge.host.planes.ensure(bridge.host.sessions)
                other = root / "other"
                other.mkdir()
                opening[change] = str(other) if change == "workspace" else "second"
                bridge.open_session(opening)
                with patch.object(first.mcp, "close", wraps=first.mcp.close) as close:
                    second = bridge.host.planes.ensure(bridge.host.sessions)
                self.assertIsNot(first, second)
                close.assert_called_once()

    def test_repeated_open_session_commands_use_the_same_bridge(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        opening = open_session_message(root, session_id="first")
        bridge, output = self.make_bridge([
            json.dumps(opening),
            json.dumps({**opening, "session_id": "second"}),
            json.dumps({"type": "load_session"}),
            json.dumps({"type": "shutdown"}),
        ], root)
        bridge.serve()
        ready = [m for m in emitted(output) if m["type"] == "session_ready"]
        self.assertEqual([m["session_id"] for m in ready], ["first", "second"])
        self.assertEqual(bridge.host.sessions.session.session_id, "second")

    def test_binding_failure_closes_retained_resources(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bridge, _ = self.make_bridge([], root)
        opening = open_session_message(root)
        bridge.open_session(opening)
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        bridge.open_session(opening)
        with (
            patch("agent_runtime.plane_manager.Agent", side_effect=ValueError("binding failed")),
            patch.object(plane.mcp, "close", wraps=plane.mcp.close) as close,
        ):
            with self.assertRaisesRegex(ValueError, "binding failed"):
                bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNone(bridge.host.planes.current)
        self.assertIsNone(plane.jobs)
        close.assert_called_once()

    @unittest.skipUnless(sys.platform == "win32", "Windows journal ownership")
    def test_occupied_session_can_be_reselected_without_exiting_bridge(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        owner, _ = self.make_bridge([], root)
        opening = open_session_message(root, session_id="occupied")
        owner.open_session(opening)
        owner.host.sessions.session.add_item("user", "owner history")

        # Requests after the failure stay on the same stdio connection.
        contender, output = self.make_bridge([
            json.dumps(opening),
            json.dumps({"type": "list_sessions"}),
            json.dumps({**opening, "session_id": "available"}),
            json.dumps({"type": "load_session"}),
            json.dumps({"type": "shutdown"}),
        ], root)
        contender.serve()
        messages = emitted(output)
        self.assertEqual([item["type"] for item in messages], [
            "session_open_failed", "sessions_listed", "session_ready",
            "runtime_state", "session_items",
        ])
        self.assertEqual(messages[0]["session_id"], "occupied")
        self.assertEqual(messages[0]["error"]["type"], "SessionInUseError")
        self.assertNotIn("runtime", messages[0])
        self.assertEqual(messages[1]["sessions"][0]["session_id"], "occupied")
        self.assertEqual(messages[2]["session_id"], "available")
        owner.host.sessions.session.add_item("assistant", "owner continues")
        self.assertEqual(len(owner.host.sessions.store.load("occupied").items), 2)

    @unittest.skipUnless(sys.platform == "win32", "Windows journal ownership")
    def test_failed_open_leaves_no_session_and_can_retry_after_owner_closes(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        owner, _ = self.make_bridge([], root)
        opening = open_session_message(root, session_id="occupied")
        owner.open_session(opening)
        owner.host.sessions.session.add_item("user", "preserved")
        contender, _ = self.make_bridge([], root)
        with self.assertRaises(SessionInUseError):
            contender.open_session(opening)
        self.assertIsNone(contender.host.sessions.session)
        self.assertIsNone(contender.host.sessions.permissions)
        self.assertIsNone(contender.host.sessions.plan)
        owner.close()
        contender.open_session(opening)
        self.assertEqual(contender.host.sessions.session.items[0].content, "preserved")

    @unittest.skipUnless(sys.platform == "win32", "Windows journal ownership")
    def test_journal_protection_follows_workspace_and_session_lifecycle(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
        bridge.open_session(open_session_message(root))
        controller = bridge.host.sessions
        first = controller.session
        first.add_item("user", "keep this audit record")
        original = next((root / "sessions").rglob(f"{first.session_id}.jsonl"))
        with self.assertRaises(PermissionError):
            original.unlink()

        moved_workspace = root / "moved"
        moved_workspace.mkdir()
        bridge.host.change_workspace(moved_workspace)
        moved = next((root / "sessions").rglob(f"{first.session_id}.jsonl"))
        self.assertFalse(original.exists())
        with self.assertRaises(PermissionError):
            moved.unlink()
        first.add_item("assistant", "still persisted after move")

        bridge.host.open_session(root)
        with moved.open("a+b"):
            pass  # Switching sessions releases the former writer.
        second = controller.session
        current = next((root / "sessions").rglob(f"{second.session_id}.jsonl"))
        with self.assertRaises(PermissionError):
            current.unlink()
        bridge.close()
        current.unlink()
        self.assertEqual(len(controller.store.load(first.session_id).items), 2)

    @unittest.skipUnless(sys.platform == "win32", "Windows journal ownership")
    def test_failed_workspace_move_keeps_original_journal_protected(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
        bridge.open_session(open_session_message(root))
        controller = bridge.host.sessions
        session = controller.session
        path = next((root / "sessions").rglob(f"{session.session_id}.jsonl"))
        destination = root / "destination"
        destination.mkdir()
        with patch.object(controller.store, "bind_workspace", side_effect=OSError("move failed")):
            with self.assertRaisesRegex(OSError, "move failed"):
                bridge.host.change_workspace(destination)
        with self.assertRaises(PermissionError):
            path.unlink()
        session.add_item("user", "persisted after failed move")
        self.assertEqual(controller.store.load(session.session_id).items[0].content,
                         "persisted after failed move")

    def started_model(self, **extra: object) -> str:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, stdout = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        store = JsonlSessionStore(root / "sessions")
        store.set_provider("saved", "second", root)
        bridge, stdout = self.make_bridge([], root)

        bridge.open_session(open_session_message(root, session_id="saved"))

        self.assertEqual(emitted(stdout)[0]["provider"], "second")
        self.assertEqual(emitted(stdout)[0]["model"], "openai/second")

    def test_opening_a_session_does_not_initialize_the_agent_runtime(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, stdout = self.make_bridge([], root)

        bridge.open_session(open_session_message(root))

        self.assertIsNone(bridge.host.planes.current)
        self.assertEqual(
            [message["type"] for message in emitted(stdout)],
            ["session_ready", "runtime_state"],
        )
        self.assertEqual(emitted(stdout)[-1]["phase"], "inactive")

    def test_compression_uses_main_model_and_fast_model_only_serves_memory(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
        bridge.open_session(open_session_message(
            root,
            provider_config={
                "main_agent": {"provider": "first"},
                "fast_model": {"provider": "second"},
                "providers": {
                    "first": {"model": "openai/first", "max_context_tokens": 1000},
                    "second": {"model": "openai/second", "max_context_tokens": 200},
                },
            },
        ))
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIs(plane.agent._provider, plane.provider)
        self.assertIs(plane.agent._context._provider, plane.provider)
        self.assertEqual(plane.agent._context.limits.compression_threshold, 675)
        self.assertIs(plane.memory.reconciler._provider, plane.fast_provider)
        self.assertIsNot(plane.provider, plane.fast_provider)

        usage_files = list((root / "sessions").rglob("auxiliary_model_usage.jsonl"))
        self.assertEqual(usage_files, [])
        session = bridge.host.sessions.session
        for index in range(8):
            session.add_item("user", f"request {index}")
            session.add_item("assistant", f"answer {index}")
        with patch.object(
            plane.provider, "stream_cancellable",
            return_value=LLMResponse(
                content="- [durable] Prior work.", usage=TokenUsage(50, 8, 58, 30, 10),
            ),
        ):
            plane.agent._context.archive(plane.agent._context.build_request(plane.agent._tools.definitions))
        plane.memory.remember(MemoryCandidate("fact", "global", "A fact."))
        with patch.object(
            plane.fast_provider, "stream",
            return_value=LLMResponse(
                content='{"entries":[{"kind":"fact","content":"A fact."}]}',
                usage=TokenUsage(12, 6, 18, 0, None),
            ),
        ):
            plane.memory.reconcile_pending()
        stored = bridge.host.sessions.store.load(session.session_id)
        calls = [event for event in stored.journal
                 if event.event_type in {"compression_completed", "memory_completed"}]
        records = [event.payload for event in calls]
        self.assertEqual([(event.event_type, r["scope"], r["model"], r["status"])
                          for event, r in zip(calls, records)], [
            ("compression_completed", None, "openai/first", "completed"),
            ("memory_completed", "global", "openai/second", "completed"),
        ])
        self.assertEqual(list((root / "sessions").rglob("auxiliary_model_usage.jsonl")), [])
        self.assertEqual(records[0]["usage"], {
            "input_tokens": 50, "output_tokens": 8, "total_tokens": 58,
            "cache_read_tokens": 30, "cache_write_tokens": 10,
        })
        self.assertEqual(records[1]["usage"], {
            "input_tokens": 12, "output_tokens": 6, "total_tokens": 18,
            "cache_read_tokens": 0, "cache_write_tokens": None,
        })
        self.assertEqual(session.cache_usage, CacheUsage(50, 30))
        self.assertEqual(stored.cache_usage, CacheUsage(50, 30))
        compression = [event for event in stored.journal if event.event_type == "compression_completed"]
        self.assertEqual(len(compression), 1)
        self.assertEqual(compression[0].payload["usage"], records[0]["usage"])

    def test_disabled_memory_still_loads_the_fast_model(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
        bridge.open_session(open_session_message(
            root,
            agent_config={
                "max_same_tool_calls": 5,
                "output_reserve_tokens": 100,
                "workspace_instruction_files": [],
                "main_agent": {"tools": {}},
                "memory": {"enabled": False},
            },
            provider_config={
                "main_agent": {"provider": "first"},
                "fast_model": {"provider": "fast"},
                "providers": {
                    "first": {"model": "openai/first", "max_context_tokens": 1000},
                    "fast": {"model": "openai/fast", "max_context_tokens": 1000},
                },
            },
        ))
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIs(plane.agent._context._provider, plane.provider)
        self.assertIsNot(plane.fast_provider, plane.provider)
        self.assertEqual(plane.fast_provider.model_name, "openai/fast")
        self.assertIsNone(plane.memory)

    def test_subagent_compression_and_memory_are_logged_in_own_session(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
        bridge.open_session(open_session_message(
            root,
            agent_config={
                "max_same_tool_calls": 5,
                "output_reserve_tokens": 100,
                "workspace_instruction_files": [],
                "context": {"compression": {"keep_recent_units": 1}},
                "main_agent": {"tools": {"subagent": True}},
                "subagent_roles": {
                    "researcher": {
                        "description": "Reads files.",
                        "tools": {},
                    },
                },
            },
            provider_config={
                "main_agent": {"provider": "first"},
                "subagent_roles": {"researcher": {"provider": "second"}},
                "providers": {
                    "first": {"model": "openai/first", "max_context_tokens": 1000},
                    "second": {"model": "openai/second", "max_context_tokens": 1000},
                },
            },
        ))
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        context = plane.agent._tools._context
        subagents = context.subagents
        assert subagents is not None
        provider = subagents.roles.get("researcher").provider
        responses = [
            LLMResponse(content=None, tool_calls=(
                ToolCall("child-call", "remember", {
                    "kind": "fact", "scope": "global", "content": "A durable fact.",
                }),
            )),
            LLMResponse(content="- [durable] Inspected the directory.",
                        usage=TokenUsage(500, 10, 510, 450, None)),
            LLMResponse(content="Finished inspection.",
                        usage=TokenUsage(100, 10, 110, 50, None)),
        ]
        with (
            patch.object(
                provider, "count_input_tokens",
                side_effect=lambda request: (
                    800 if any(message.role == "tool" for message in request.messages)
                    else 1
                ),
            ),
            patch.object(provider, "stream_cancellable", side_effect=responses),
            patch.object(
                plane.fast_provider, "stream",
                return_value=LLMResponse(content=json.dumps({"entries": [
                    {"kind": "fact", "content": "A durable fact."},
                ]}), usage=TokenUsage(1000, 10, 1010, 0, None)),
            ),
        ):
            answer = subagents.run("researcher", "inspect", context)

        self.assertEqual(answer, "Finished inspection.")
        parent_session = bridge.host.sessions.session
        parent_directory = session_directory(
            root / "sessions", root, parent_session.session_id,
        )
        child_path, = (parent_directory / "subagents").glob("*/*.jsonl")
        child_store = JsonlSessionStore(parent_directory / "subagents", group_by_workspace=False)
        child = child_store.load(child_path.stem)
        calls = [event for event in child.journal
                 if event.event_type in {"compression_completed", "memory_completed"}]
        records = [event.payload for event in calls]
        self.assertEqual(len(records), 2)
        self.assertEqual(
            (calls[0].event_type, records[0]["model"], records[0]["status"]),
            ("compression_completed", "openai/second", "completed"),
        )
        self.assertEqual(
            (calls[1].event_type, records[1]["scope"], records[1]["model"], records[1]["status"]),
            ("memory_completed", "global", "openai/first", "completed"),
        )
        self.assertEqual(list((root / "sessions").rglob("auxiliary_model_usage.jsonl")), [])
        self.assertFalse(any(event.event_type in {"compression_completed", "memory_completed"}
                             for event in parent_session.journal))
        self.assertEqual(child.cache_usage, CacheUsage(600, 500))
        self.assertIsNone(parent_session.cache_usage)
        self.assertEqual(
            plane.memory.store.load(root).global_memory.facts,
            ("A durable fact.",),
        )

    def test_applies_provider_request_policy_to_all_chat_providers(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
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
            ModelProvider,
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
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
        bridge, stdout = self.make_bridge([], root)

        bridge.open_session(message)

        self.assertEqual(emitted(stdout)[0]["type"], "session_ready")
        with self.assertRaisesRegex(
            ValueError,
            "MISSING_NOSIS_TEST_KEY",
        ):
            bridge.host.planes.ensure(bridge.host.sessions)

    def test_injects_the_configured_image_generator(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
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
        bridge, stdout = self.make_bridge([], root)

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
        directory = self.enterContext(tempfile.TemporaryDirectory())
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
        bridge, stdout = self.make_bridge([], root)

        bridge.open_session(open_session_message(root, session_id="saved"))

        state = emitted(stdout)[-1]
        self.assertEqual(state["type"], "runtime_state")
        self.assertEqual(state["plan"]["goal"], "Ship plans")
        self.assertEqual(state["plan"]["revision"], 1)

    def test_rejects_an_unconfigured_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "not configured"):
            self.started_model(provider="unknown")

    def test_registers_discovered_skills_and_their_read_tool(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
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
        bridge, _ = self.make_bridge([], root)

        bridge.open_session(open_session_message(root))
        plane = bridge.host.planes.ensure(bridge.host.sessions)

        names = [item.name for item in plane.agent._tools.definitions]
        prompt = plane.agent._context._system_prompt

        self.assertIn("read_skill", names)
        self.assertIn("demo-skill: Does demo work.", prompt)
        self.assertNotIn("# Detailed instructions", prompt)

    def test_registers_configured_global_and_workspace_skill_paths(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        config = root / "config"
        workspace = root / "workspace"
        config.mkdir()
        workspace.mkdir()
        for skill_root, name in (
            (config / "shared-skills", "global-skill"),
            (workspace / ".project-skills", "workspace-skill"),
        ):
            skill = skill_root / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\n"
                f"name: {name}\n"
                f"description: {name} description.\n"
                "---\n",
                encoding="utf-8",
            )
        message = open_session_message(
            config,
            agent_config={
                "max_same_tool_calls": 5,
                "output_reserve_tokens": 100,
                "scratch_workspace_root": str(config / "scratch"),
                "workspace_instruction_files": ["AGENTS.md"],
                "skills": {
                    "global_paths": ["shared-skills"],
                    "workspace_paths": [
                        ".project-skills",
                        str(workspace / ".project-skills"),
                    ],
                },
                "main_agent": {"tools": {}},
            },
        )
        message["workspace"] = str(workspace)
        bridge, _ = self.make_bridge([], config)

        bridge.open_session(message)
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        skills = plane.agent._execution_context.skills

        assert skills is not None
        self.assertEqual(skills.names, ("global-skill", "workspace-skill"))
        self.assertEqual(plane.runtime_warnings, ())

    def test_registers_plugin_skills_with_the_plugin_namespace(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
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
        bridge, _ = self.make_bridge([], root)

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
        directory = self.enterContext(tempfile.TemporaryDirectory())
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
        bridge, stdout = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
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
        bridge, stdout = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
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
        bridge, _ = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        message = open_session_message(root)
        (root / "prompts" / "Soul.md").write_text(
            "Custom prompt for {{workspace}}",
            encoding="utf-8",
        )
        bridge, _ = self.make_bridge([], root)

        bridge.open_session(message)
        plane = bridge.host.planes.ensure(bridge.host.sessions)

        request = plane.agent._context.build_request()
        prompt = request.system_prompt
        runtime_context = request.messages[0].content

        self.assertTrue(prompt.startswith(f"Custom prompt for {root.resolve()}"))
        self.assertNotIn("## Long-term Memory", prompt)
        self.assertIn("## Long-term Memory", runtime_context)

    def test_injects_global_and_workspace_instructions_without_persisting_them(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        (root / "AGENTS.md").write_text("global rule", encoding="utf-8")
        (workspace / "CLAUDE.md").write_text("claude rule", encoding="utf-8")
        (workspace / "AGENTS.md").write_text("workspace rule", encoding="utf-8")
        bridge, _ = self.make_bridge([], root)

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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "PROJECT.md").write_text("project rule", encoding="utf-8")
        (workspace / "AGENTS.md").write_text("not configured", encoding="utf-8")
        bridge, _ = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        instructions_path = root / "AGENTS.md"
        instructions_path.write_text("first rule", encoding="utf-8")
        bridge, _ = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
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
        request = third_plane.agent._context.build_request()
        self.assertNotIn("Prefer concise responses.", request.system_prompt)
        runtime_context = request.messages[0].content
        self.assertIn("Prefer concise responses.", runtime_context)
        self.assertIn("current request has highest priority", runtime_context)

    def test_rebuilds_runtime_when_the_configured_instruction_list_changes(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        (root / "PROJECT.md").write_text("project rule", encoding="utf-8")
        bridge, _ = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
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

    def test_tool_settings_rebuild_plane_and_survive_session_rebinding(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bridge, _ = self.make_bridge([], root)
        opening = open_session_message(root)
        bridge.open_session(opening)
        first_plane = bridge.host.planes.ensure(bridge.host.sessions)
        config_path = root / "tool_config.json"
        config_path.write_text(json.dumps({
            "web_search": {
                "provider": "exa", "providers": {"exa": {"key": "${SEARCH_KEY}"}},
            },
            "web_fetch": {
                "provider": "deepseek", "providers": {"deepseek": {"key": "${SEARCH_KEY}"}},
            },
        }), encoding="utf-8")
        (root / ".env").write_text("SEARCH_KEY=first-key\n", encoding="utf-8")

        second_plane = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNot(second_plane, first_plane)
        settings = second_plane.agent._execution_context.tool_settings
        self.assertIs(settings, second_plane.tool_settings)
        self.assertEqual(settings.web_search.provider, "exa")
        self.assertEqual(settings.web_search.api_key, "first-key")
        self.assertEqual(settings.web_fetch.provider, "deepseek")
        self.assertEqual(settings.web_fetch.api_key, "first-key")

        bridge.open_session(opening)
        rebound = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIs(rebound, second_plane)
        self.assertIs(rebound.agent._execution_context.tool_settings, settings)

        (root / ".env").write_text("SEARCH_KEY=second-key\n", encoding="utf-8")
        third_plane = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNot(third_plane, second_plane)
        self.assertEqual(third_plane.tool_settings.web_search.api_key, "second-key")
        self.assertEqual(third_plane.tool_settings.web_fetch.api_key, "second-key")

    def test_rebuilds_the_execution_plane_when_dotenv_changes(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
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
        bridge, _ = self.make_bridge([], root)
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, _ = self.make_bridge([], root)
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
                "agent_core.mcp.manager.McpClientManager.close",
                autospec=True,
            ) as close_mcp,
        ):
            with self.assertRaisesRegex(RuntimeError, "assembly failed"):
                bridge.host.planes.ensure(bridge.host.sessions)

        self.assertIsNone(bridge.host.planes.current)
        close_jobs.assert_called_once()
        close_mcp.assert_called_once()

    def test_context_window_event_updates_the_execution_plane_snapshot(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        bridge, stdout = self.make_bridge([], root)
        bridge.open_session(open_session_message(root))
        plane = bridge.host.planes.ensure(bridge.host.sessions)

        bridge.host.publish_event(
            ContextWindowEvent(
                ContextWindow(
                    input_tokens=240,
                    system_prompt_tokens=60,
                    max_input_tokens=900,
                    max_context_tokens=1000,
                    output_reserve_tokens=100,
                    compression_threshold=720,
                    compression_count=3,
                    compression_summary="Archived context",
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
                "system_prompt_tokens": 60,
                "max_input_tokens": 900,
                "max_context_tokens": 1000,
                "output_reserve_tokens": 100,
                "compression_threshold": 720,
                "compression_count": 3,
                "compression_summary": "Archived context",
            },
        )

    def test_invalid_skill_does_not_prevent_bridge_startup(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
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
        bridge, stdout = self.make_bridge([], root)

        bridge.open_session(open_session_message(root))
        plane = bridge.host.planes.ensure(bridge.host.sessions)

        runtime_state = emitted(stdout)[-1]
        names = [item.name for item in plane.agent._tools.definitions]

        self.assertEqual(runtime_state["type"], "runtime_state")
        self.assertEqual(runtime_state["phase"], "starting")
        self.assertEqual(len(plane.runtime_warnings), 1)
        self.assertIn("Skipping skill at", plane.runtime_warnings[0])
        self.assertIn("read_skill", names)


class RuntimeRefreshTest(BridgeTestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bridge, self.stdout = self.make_bridge([], self.root)
        self.bridge.open_session(open_session_message(self.root))

    def test_refresh_rebuilds_unchanged_resources_and_preserves_session_state(self) -> None:
        host = self.bridge.host
        host.set_permission_preset(PermissionPreset.FULL_ACCESS)
        session = host.sessions.session
        session.add_item("user", "Keep this conversation")
        plan = host.sessions.plan
        plan.update("Ship", (PlanStep("one", "Inspect", "pending"),))
        first = host.planes.ensure(host.sessions)
        journal = tuple(session.journal)
        with patch.object(first.mcp, "close", wraps=first.mcp.close) as close:
            self.bridge._refresh_runtime()
        second = host.planes.current

        self.assertIsNot(first, second)
        close.assert_called_once_with()
        self.assertIs(host.sessions.session, session)
        self.assertIs(host.sessions.plan, plan)
        self.assertEqual(tuple(session.journal), journal)
        self.assertEqual(session.permission_preset, PermissionPreset.FULL_ACCESS)
        self.assertIs(second.agent._session, session)
        result = emitted(self.stdout)[-1]
        self.assertEqual(result["type"], "runtime_refreshed")
        self.assertEqual(result["runtime"]["phase"], "idle")
        self.assertIsNone(result["runtime"]["turn_id"])

    def test_refresh_loads_new_skills_and_prompt_without_a_turn(self) -> None:
        host = self.bridge.host
        first = host.planes.ensure(host.sessions)
        skill = self.root / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: demo\ndescription: New skill.\n---\nInstructions", encoding="utf-8",
        )
        (self.root / "prompts" / "Soul.md").write_text("Updated prompt", encoding="utf-8")
        plugin = self.root / "plugins" / "example"
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text(json.dumps({
            "name": "example", "components": {"agents": ["reviewer.md"]},
        }), encoding="utf-8")
        (plugin / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: Review sources.\n---\nInspect the changes.",
            encoding="utf-8",
        )
        self.assertIs(host.planes.ensure(host.sessions), first)

        self.bridge._refresh_runtime()

        self.assertEqual(host.planes.current.skills.names, ("demo",))
        self.assertIn("Updated prompt", host.planes.current.agent._context._system_prompt)
        self.assertEqual([role.name for role in host.planes.current.plugin_agents], ["example:reviewer"])
        self.assertEqual(host.sessions.session.items, [])
        self.assertEqual(host.sessions.session.turns, {})

    def test_refresh_reconnects_and_rediscovers_mcp_with_unchanged_configuration(self) -> None:
        config_path = self.root / "agent_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["mcp"] = {"enabled": True, "servers": {"fake": {
            "transport": "stdio", "command": sys.executable,
            "args": [str(Path(__file__).with_name("fake_mcp_server.py"))],
            "tools": {"enabled": ["echo"], "approval": "never"},
        }}}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        host = self.bridge.host
        first = host.planes.ensure(host.sessions)

        self.bridge._refresh_runtime()

        second = host.planes.current
        self.assertIsNot(second.mcp, first.mcp)
        self.assertEqual(second.mcp.config, first.mcp.config)
        statuses = [item["status"] for item in emitted(self.stdout) if item["type"] == "mcp_server_status"]
        self.assertEqual(statuses.count("ready"), 2)
        self.assertIn("closed", statuses)
        result = second.agent._tools.execute(ToolCall("echo", "mcp__fake__echo", {"text": "reconnected"}))
        self.assertIsNone(result.error)
        self.assertEqual(result.output["structured_content"], {"echo": "reconnected"})

    def test_failed_refresh_reports_error_and_can_be_retried(self) -> None:
        host = self.bridge.host
        host.planes.ensure(host.sessions)
        with patch("agent_runtime.plane_manager.Agent", side_effect=RuntimeError("bad assembly")):
            self.bridge._refresh_runtime()
        failure = emitted(self.stdout)[-1]
        self.assertEqual(failure["type"], "runtime_refresh_failed")
        self.assertEqual(failure["error"]["message"], "bad assembly")
        self.assertEqual(failure["runtime"]["phase"], "failed")
        self.assertIsNone(host.planes.current)

        self.bridge._refresh_runtime()

        self.assertEqual(emitted(self.stdout)[-1]["type"], "runtime_refreshed")
        self.assertIsNotNone(host.planes.current)

    def test_runtime_rejects_refresh_during_a_turn(self) -> None:
        host = self.bridge.host
        plane = host.planes.ensure(host.sessions)
        host.turns.turn_id = "busy"
        try:
            with self.assertRaisesRegex(RuntimeError, "during a turn"):
                host.refresh_runtime()
            self.assertIs(host.planes.current, plane)
        finally:
            host.turns.turn_id = None

    def test_refresh_command_is_dispatched_without_a_user_turn(self) -> None:
        with patch.object(self.bridge, "read_message", side_effect=[
            {"type": "open_session", "workspace": str(self.root), "session_id": None},
            decode('{"type":"runtime_refresh"}'), None,
        ]):
            self.bridge.serve()
        self.assertEqual(emitted(self.stdout)[-1]["type"], "runtime_refreshed")
        self.assertEqual(self.bridge.host.sessions.session.items, [])


class SubagentRoleStartTest(BridgeTestCase):
    """Only enabled roles reach the model's subagent schema."""

    def offered_roles(self, roles: dict) -> list[str]:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        bridge, _ = self.make_bridge([], Path(directory))
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        bridge, _ = self.make_bridge([], Path(directory))
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        bridge, _ = self.make_bridge([], Path(directory))
        bridge.open_session(
            open_session_message(
                Path(directory),
                agent_config={
                    "max_same_tool_calls": 5,
                    "output_reserve_tokens": 100,
                    "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
                    "main_agent": {"tools": {"subagent": True, "job_control": True}},
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
        self.assertIn("job_control", main_names)
        self.assertNotIn("ask_user", role_names)
        self.assertNotIn("update_plan", role_names)
        self.assertNotIn("job_control", role_names)
        self.assertNotIn("remember", role_names)


class PluginAgentStartTest(BridgeTestCase):
    def test_assembles_plugin_agents_with_prompts_tools_and_providers(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
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
        bridge, _ = self.make_bridge([], root)
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


class AnalyzeImageDerivationTest(BridgeTestCase):
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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        bridge, _ = self.make_bridge([], Path(directory))
        with patch.object(
            ModelProvider, "capabilities_for_model", capabilities_stub(VISION_MODEL)
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
                                "api": "chat_completions",
                                "max_context_tokens": 1000,
                            },
                            "seeing": {
                                "model": VISION_MODEL,
                                "api": "chat_completions",
                                "max_context_tokens": 1000,
                            },
                            "blind": {
                                "model": TEXT_MODEL,
                                "api": "chat_completions",
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


class CrossFileRoleValidationTest(BridgeTestCase):
    """The two config files must agree on which roles exist."""

    def start(self, provider_roles: dict, agent_roles: dict) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        bridge, _ = self.make_bridge([], Path(directory))
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

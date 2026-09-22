import threading
import unittest
from dataclasses import replace

from agent_core import (
    ExecutionRouter,
    PermissionController,
    PermissionPreset,
    Session,
    ShellApprovalPolicy,
    ToolCall,
    ToolExecutionContext,
    Workspace,
)
from pathlib import Path


class UnusedExecutor:
    def execute(self, command, timeout_seconds=60, cancellation=None):
        raise AssertionError("executor should not run")

    def close(self):
        pass


def context(
    session: Session | None = None,
    call: ToolCall | None = None,
) -> ToolExecutionContext:
    current = session or Session()
    router = ExecutionRouter(
        UnusedExecutor(),
        UnusedExecutor(),
        authority=lambda: current.permission_preset.authority,
    )
    result = ToolExecutionContext(
        Workspace(Path(__file__).parent),
        current,
        execution_router=router,
    )
    if call is not None:
        result = replace(result, execution=router.resolve(call))
    return result


class ShellApprovalPolicyTest(unittest.TestCase):
    def test_requests_approval_for_shell_command(self) -> None:
        requested_commands = []
        policy = ShellApprovalPolicy(
            lambda command: requested_commands.append(command) or True
        )

        policy.authorize(ToolCall("call-1", "shell", {"command": "pwd"}), context())

        self.assertEqual(requested_commands, ["pwd"])

    def test_rejects_shell_command_without_approval(self) -> None:
        policy = ShellApprovalPolicy(lambda command: False)

        with self.assertRaisesRegex(PermissionError, "not approved"):
            policy.authorize(
                ToolCall("call-1", "shell", {"command": "pwd"}), context()
            )

    def test_ignores_other_tools(self) -> None:
        requested_commands = []
        policy = ShellApprovalPolicy(
            lambda command: requested_commands.append(command) or False
        )

        policy.authorize(
            ToolCall("call-1", "read_file", {"path": "README.md"}), context()
        )

        self.assertEqual(requested_commands, [])


class PermissionControllerTest(unittest.TestCase):
    def test_full_access_defaults_to_host(self) -> None:
        self.assertEqual(
            PermissionPreset.FULL_ACCESS.default_scope.value,
            "host",
        )

    def test_ask_for_approval_delegates_to_the_approval_policy(self) -> None:
        calls = []

        class RecordingPolicy:
            def authorize(self, call: ToolCall, context) -> None:
                calls.append(call)

        controller = PermissionController(Session("s"), RecordingPolicy())
        call = ToolCall("call-1", "shell", {"command": "pwd"})

        controller.authorize(call, context(call=call))

        self.assertEqual(calls, [call])

    def test_records_an_approval_decision_as_a_user_anchor(self) -> None:
        class ApprovalPolicy:
            def authorize(self, call: ToolCall, context) -> bool:
                return True

        session = Session("s")
        session.begin_turn("turn-1")
        controller = PermissionController(session, ApprovalPolicy())

        call = ToolCall("call-1", "shell", {"command": "pwd"})
        controller.authorize(call, context(session, call))

        anchor = session.user_anchors[-1]
        self.assertEqual(anchor.source, "approval_response")
        self.assertIn('"command": "pwd"', anchor.content)
        self.assertIn("approved", anchor.content)

    def test_records_a_denied_approval_as_a_user_anchor(self) -> None:
        class DenialPolicy:
            def authorize(self, call: ToolCall, context) -> bool:
                raise PermissionError("not approved")

        session = Session("s")
        session.begin_turn("turn-1")
        controller = PermissionController(session, DenialPolicy())

        with self.assertRaises(PermissionError):
            call = ToolCall("call-1", "shell", {"command": "rm output"})
            controller.authorize(call, context(session, call))

        anchor = session.user_anchors[-1]
        self.assertEqual(anchor.source, "approval_response")
        self.assertIn("denied", anchor.content)

    def test_full_access_skips_approval(self) -> None:
        calls = []

        class RecordingPolicy:
            def authorize(self, call: ToolCall, context) -> None:
                calls.append(call)

        session = Session("s")
        controller = PermissionController(session, RecordingPolicy())
        controller.set_preset(PermissionPreset.FULL_ACCESS)

        call = ToolCall("call-1", "shell", {"command": "pwd"})
        controller.authorize(call, context(session, call))

        self.assertEqual(session.permission_preset, PermissionPreset.FULL_ACCESS)
        self.assertEqual(calls, [])

    def test_workspace_access_skips_workspace_scoped_operation(self) -> None:
        calls = []

        class WorkspacePolicy:
            def authorize(self, call: ToolCall, context) -> None:
                calls.append(call)

        session = Session("s")
        controller = PermissionController(session, WorkspacePolicy())
        controller.set_preset(PermissionPreset.WORKSPACE_ACCESS)

        call = ToolCall("call-1", "shell", {"command": "pwd"})
        controller.authorize(call, context(session, call))

        self.assertEqual(calls, [])

    def test_ask_for_approval_asks_for_workspace_scope(self) -> None:
        requested_commands = []
        controller = PermissionController(
            Session("s"),
            ShellApprovalPolicy(
                lambda command: requested_commands.append(command) or True
            ),
        )

        call = ToolCall("call-1", "shell", {"command": "pwd"})
        controller.authorize(call, context(call=call))

        self.assertEqual(requested_commands, ["pwd"])

    def test_workspace_access_still_asks_for_host_scope(self) -> None:
        requested_commands = []
        session = Session("s")
        controller = PermissionController(
            session,
            ShellApprovalPolicy(
                lambda command: requested_commands.append(command) or True
            ),
        )
        controller.set_preset(PermissionPreset.WORKSPACE_ACCESS)
        call = ToolCall(
            "call-1", "shell", {"command": "pwd", "scope": "host"}
        )
        controller.authorize(call, context(session, call))

        self.assertEqual(requested_commands, ["pwd"])
        self.assertEqual(
            session.permission_preset,
            PermissionPreset.WORKSPACE_ACCESS,
        )

    def test_full_access_skips_host_scope_approval(self) -> None:
        requested_commands = []
        session = Session("s")
        controller = PermissionController(
            session,
            ShellApprovalPolicy(
                lambda command: requested_commands.append(command) or True
            ),
        )
        controller.set_preset(PermissionPreset.FULL_ACCESS)

        call = ToolCall(
            "call-1", "shell", {"command": "pwd", "scope": "host"}
        )
        controller.authorize(call, context(session, call))

        self.assertEqual(requested_commands, [])

    def test_pending_approval_keeps_its_original_decision(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingPolicy:
            def authorize(self, call: ToolCall, context) -> None:
                entered.set()
                release.wait(2)
                raise PermissionError("not approved")

        controller = PermissionController(Session("s"), BlockingPolicy())
        errors = []

        def authorize() -> None:
            try:
                call = ToolCall("call-1", "shell", {"command": "pwd"})
                controller.authorize(call, context(call=call))
            except PermissionError as error:
                errors.append(str(error))

        thread = threading.Thread(target=authorize)
        thread.start()
        self.assertTrue(entered.wait(1))
        controller.set_preset(PermissionPreset.FULL_ACCESS)
        release.set()
        thread.join(2)

        self.assertEqual(errors, ["not approved"])


if __name__ == "__main__":
    unittest.main()

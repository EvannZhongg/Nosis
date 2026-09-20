import threading
import unittest

from agent_core import (
    ApprovalScope,
    HostCommandExecutor,
    PermissionController,
    PermissionPreset,
    Session,
    ShellApprovalPolicy,
    ToolCall,
    ToolExecutionContext,
    Workspace,
)
from pathlib import Path


def context(session: Session | None = None) -> ToolExecutionContext:
    return ToolExecutionContext(
        Workspace(Path(__file__).parent), session or Session()
    )


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
    def test_ask_for_approval_delegates_to_the_approval_policy(self) -> None:
        calls = []

        class RecordingPolicy:
            def approval_scope(self, call: ToolCall, context):
                return ApprovalScope.HOST

            def authorize(self, call: ToolCall, context) -> None:
                calls.append(call)

        controller = PermissionController(Session("s"), RecordingPolicy())
        call = ToolCall("call-1", "shell", {"command": "pwd"})

        controller.authorize(call, context())

        self.assertEqual(calls, [call])

    def test_records_an_approval_decision_as_a_user_anchor(self) -> None:
        class ApprovalPolicy:
            def approval_scope(self, call: ToolCall, context):
                return ApprovalScope.HOST

            def authorize(self, call: ToolCall, context) -> bool:
                return True

        session = Session("s")
        session.begin_turn("turn-1")
        controller = PermissionController(session, ApprovalPolicy())

        controller.authorize(
            ToolCall("call-1", "shell", {"command": "pwd"}),
            context(session),
        )

        anchor = session.user_anchors[-1]
        self.assertEqual(anchor.source, "approval_response")
        self.assertIn('"command": "pwd"', anchor.content)
        self.assertIn("approved", anchor.content)

    def test_records_a_denied_approval_as_a_user_anchor(self) -> None:
        class DenialPolicy:
            def approval_scope(self, call: ToolCall, context):
                return ApprovalScope.HOST

            def authorize(self, call: ToolCall, context) -> bool:
                raise PermissionError("not approved")

        session = Session("s")
        session.begin_turn("turn-1")
        controller = PermissionController(session, DenialPolicy())

        with self.assertRaises(PermissionError):
            controller.authorize(
                ToolCall("call-1", "shell", {"command": "rm output"}),
                context(session),
            )

        anchor = session.user_anchors[-1]
        self.assertEqual(anchor.source, "approval_response")
        self.assertIn("denied", anchor.content)

    def test_full_access_skips_approval(self) -> None:
        calls = []

        class RecordingPolicy:
            def approval_scope(self, call: ToolCall, context):
                return ApprovalScope.HOST

            def authorize(self, call: ToolCall, context) -> None:
                calls.append(call)

        session = Session("s")
        controller = PermissionController(session, RecordingPolicy())
        controller.set_preset(PermissionPreset.FULL_ACCESS)

        controller.authorize(
            ToolCall("call-1", "shell", {"command": "pwd"}), context()
        )

        self.assertEqual(session.permission_preset, PermissionPreset.FULL_ACCESS)
        self.assertEqual(calls, [])

    def test_workspace_access_skips_workspace_scoped_operation(self) -> None:
        calls = []

        class WorkspacePolicy:
            def approval_scope(self, call: ToolCall, context) -> ApprovalScope:
                return ApprovalScope.WORKSPACE

            def authorize(self, call: ToolCall, context) -> None:
                calls.append(call)

        session = Session("s")
        controller = PermissionController(session, WorkspacePolicy())
        controller.set_preset(PermissionPreset.WORKSPACE_ACCESS)

        controller.authorize(
            ToolCall("call-1", "write_file", {"path": "output.txt"}),
            context(session),
        )

        self.assertEqual(calls, [])

    def test_workspace_access_still_asks_for_host_executor(self) -> None:
        requested_commands = []
        session = Session("s")
        controller = PermissionController(
            session,
            ShellApprovalPolicy(
                lambda command: requested_commands.append(command) or True
            ),
        )
        controller.set_preset(PermissionPreset.WORKSPACE_ACCESS)
        tool_context = ToolExecutionContext(
            Workspace(Path(__file__).parent),
            session,
            command_executor=HostCommandExecutor(Path(__file__).parent),
        )

        controller.authorize(
            ToolCall("call-1", "shell", {"command": "pwd"}),
            tool_context,
        )

        self.assertEqual(requested_commands, ["pwd"])

    def test_pending_approval_keeps_its_original_decision(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingPolicy:
            def approval_scope(self, call: ToolCall, context):
                return ApprovalScope.HOST

            def authorize(self, call: ToolCall, context) -> None:
                entered.set()
                release.wait(2)
                raise PermissionError("not approved")

        controller = PermissionController(Session("s"), BlockingPolicy())
        errors = []

        def authorize() -> None:
            try:
                controller.authorize(
                    ToolCall("call-1", "shell", {"command": "pwd"}), context()
                )
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

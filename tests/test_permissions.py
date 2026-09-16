import threading
import unittest

from agent_core import (
    PermissionController,
    PermissionPreset,
    Session,
    ShellApprovalPolicy,
    ToolCall,
)


class ShellApprovalPolicyTest(unittest.TestCase):
    def test_requests_approval_for_shell_command(self) -> None:
        requested_commands = []
        policy = ShellApprovalPolicy(
            lambda command: requested_commands.append(command) or True
        )

        policy.authorize(ToolCall("call-1", "shell", {"command": "pwd"}))

        self.assertEqual(requested_commands, ["pwd"])

    def test_rejects_shell_command_without_approval(self) -> None:
        policy = ShellApprovalPolicy(lambda command: False)

        with self.assertRaisesRegex(PermissionError, "not approved"):
            policy.authorize(
                ToolCall("call-1", "shell", {"command": "pwd"})
            )

    def test_ignores_other_tools(self) -> None:
        requested_commands = []
        policy = ShellApprovalPolicy(
            lambda command: requested_commands.append(command) or False
        )

        policy.authorize(
            ToolCall("call-1", "read_file", {"path": "README.md"})
        )

        self.assertEqual(requested_commands, [])


class PermissionControllerTest(unittest.TestCase):
    def test_ask_for_approval_delegates_to_the_approval_policy(self) -> None:
        calls = []

        class RecordingPolicy:
            def authorize(self, call: ToolCall) -> None:
                calls.append(call)

        controller = PermissionController(Session("s"), RecordingPolicy())
        call = ToolCall("call-1", "shell", {"command": "pwd"})

        controller.authorize(call)

        self.assertEqual(calls, [call])

    def test_full_access_skips_approval(self) -> None:
        calls = []

        class RecordingPolicy:
            def authorize(self, call: ToolCall) -> None:
                calls.append(call)

        session = Session("s")
        controller = PermissionController(session, RecordingPolicy())
        controller.set_preset(PermissionPreset.FULL_ACCESS)

        controller.authorize(
            ToolCall("call-1", "shell", {"command": "pwd"})
        )

        self.assertEqual(session.permission_preset, PermissionPreset.FULL_ACCESS)
        self.assertEqual(calls, [])

    def test_pending_approval_keeps_its_original_decision(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingPolicy:
            def authorize(self, call: ToolCall) -> None:
                entered.set()
                release.wait(2)
                raise PermissionError("not approved")

        controller = PermissionController(Session("s"), BlockingPolicy())
        errors = []

        def authorize() -> None:
            try:
                controller.authorize(
                    ToolCall("call-1", "shell", {"command": "pwd"})
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

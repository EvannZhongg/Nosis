import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_core import (
    ApplyPatchTool,
    EditFileTool,
    ExecutionRouter,
    ExecutionScope,
    FileWriteApprovalPolicy,
    PermissionController,
    PermissionPreset,
    Session,
    ToolCall,
    ToolCatalog,
    ToolExecutionContext,
    Workspace,
    WriteFileTool,
)
from agent_core.tools.files import write_file_bytes
from agent_core.turn_control import AgentCancelled, TurnControl


def patch_text(body: str) -> str:
    return f"*** Begin Patch\n{body}\n*** End Patch"


def write_call(name: str) -> ToolCall:
    arguments = {
        "apply_patch": {"patch": patch_text("*** Update File: file.txt\n@@\n-old\n+new")},
        "edit_file": {"path": "file.txt", "old_text": "old", "new_text": "new"},
        "write_file": {"path": "file.txt", "content": "new\n", "overwrite": True},
    }
    return ToolCall("write", name, arguments[name])


class FileWritePermissionTest(unittest.TestCase):
    def test_presets_cover_all_file_writers_and_record_approval(self) -> None:
        for name in ("apply_patch", "edit_file", "write_file"):
            for preset in PermissionPreset:
                for approved in (False, True):
                    with self.subTest(tool=name, preset=preset, approved=approved):
                        with tempfile.TemporaryDirectory() as directory:
                            root = Path(directory)
                            target = root / "file.txt"
                            target.write_bytes(b"old\n")
                            session = Session()
                            session.set_permission_preset(preset)
                            session.begin_turn("turn")
                            request = Mock(return_value=approved)
                            controller = PermissionController(session, FileWriteApprovalPolicy(request))
                            router = ExecutionRouter(Mock(), Mock(), authority=lambda: controller.authority)
                            context = ToolExecutionContext(Workspace(root), session, execution_router=router)
                            tools = ToolCatalog([ApplyPatchTool(), EditFileTool(), WriteFileTool()]).select(
                                [name], context, policy=controller,
                            )
                            call = write_call(name)
                            self.assertIs(router.resolve(call).scope, ExecutionScope.WORKSPACE)

                            result = tools.execute(call)

                            if preset is PermissionPreset.ASK_FOR_APPROVAL:
                                request.assert_called_once()
                                self.assertEqual(request.call_args.kwargs, {"kind": "file", "tool_name": name})
                                self.assertIn(name, request.call_args.args[0])
                                if name == "apply_patch":
                                    self.assertIn(call.arguments["patch"], request.call_args.args[0])
                                self.assertEqual(session.user_anchors[-1].source, "approval_response")
                                self.assertIn(
                                    "approved" if approved else "denied",
                                    session.user_anchors[-1].content,
                                )
                            else:
                                request.assert_not_called()
                                self.assertFalse(session.user_anchors)
                            allowed = approved or preset is not PermissionPreset.ASK_FOR_APPROVAL
                            self.assertEqual(result.error is None, allowed)
                            self.assertEqual(result.executed, allowed)
                            self.assertEqual(target.read_bytes(), b"new\n" if allowed else b"old\n")

    def test_approval_denial_prevents_patch_creation_and_deletion(self) -> None:
        for body in (
            "*** Add File: new/deep/file.txt\n+new",
            "*** Delete File: file.txt",
        ):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "file.txt"
                target.write_bytes(b"old\n")
                session = Session()
                session.set_permission_preset(PermissionPreset.ASK_FOR_APPROVAL)
                controller = PermissionController(session, FileWriteApprovalPolicy(lambda *args, **kwargs: False))
                context = ToolExecutionContext(
                    Workspace(root), session,
                    execution_router=ExecutionRouter(Mock(), Mock(), authority=controller.authority),
                )
                tools = ToolCatalog([ApplyPatchTool()]).select(["apply_patch"], context, policy=controller)

                result = tools.execute(ToolCall("patch", "apply_patch", {"patch": patch_text(body)}))

                self.assertEqual(result.error.type, "PermissionError")
                self.assertEqual(target.read_bytes(), b"old\n")
                self.assertFalse((root / "new").exists())

    def test_file_approval_policy_ignores_other_tools(self) -> None:
        request = Mock(return_value=False)
        self.assertFalse(FileWriteApprovalPolicy(request).authorize(
            ToolCall("read", "read_file", {"path": "file.txt"}), None,
        ))
        request.assert_not_called()


class ApplyPatchRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.context = ToolExecutionContext(Workspace(self.root), Session())
        self.tools = ToolCatalog([ApplyPatchTool()]).select(["apply_patch"], self.context)

    def apply(self, body: str):
        return self.tools.execute(ToolCall("patch", "apply_patch", {"patch": patch_text(body)}))

    def test_append_keeps_lines_separate_without_adding_a_final_newline(self) -> None:
        for original in (b"old", b"first\r\nold"):
            with self.subTest(original=original):
                target = self.root / "file.txt"
                target.write_bytes(original)
                result = self.apply("*** Update File: file.txt\n@@\n old\n+new")
                self.assertIsNone(result.error)
                newline = b"\r\n" if b"\r\n" in original else b"\n"
                self.assertEqual(target.read_bytes(), original + newline + b"new")

    def test_preserves_mixed_line_endings_including_the_last_line(self) -> None:
        target = self.root / "file.txt"
        target.write_bytes(b"old\nkeep\r\n")
        result = self.apply("*** Update File: file.txt\n@@\n-old\n+new")
        self.assertIsNone(result.error)
        self.assertEqual(target.read_bytes(), b"new\nkeep\r\n")

    def test_numbered_insertions_support_start_middle_end_and_empty_file(self) -> None:
        cases = (
            (b"one\ntwo\n", "@@ -0,0 +1,1 @@", b"new\none\ntwo\n"),
            (b"one\ntwo\n", "@@ -1,0 +2,1 @@", b"one\nnew\ntwo\n"),
            (b"one\ntwo", "@@ -2,0 +3,1 @@", b"one\ntwo\nnew"),
            (b"", "@@ -0,0 +1,1 @@", b"new\n"),
        )
        for original, header, expected in cases:
            with self.subTest(header=header, original=original):
                target = self.root / "file.txt"
                target.write_bytes(original)
                result = self.apply(f"*** Update File: file.txt\n{header}\n+new")
                self.assertIsNone(result.error)
                self.assertEqual(target.read_bytes(), expected)

    def test_rejects_out_of_bounds_and_overlapping_ranges_before_any_write(self) -> None:
        bodies = (
            "@@ -99,0 +100,1 @@\n+new",
            "@@ -2,2 +2,1 @@\n-two\n-missing\n+new",
            "@@ -2 +2 @@\n-two\n+new\n@@ -1 +1 @@\n-one\n+new",
        )
        target = self.root / "file.txt"
        for body in bodies:
            with self.subTest(body=body):
                target.write_bytes(b"one\ntwo\n")
                result = self.apply("*** Add File: new/deep/file.txt\n+new\n*** Update File: file.txt\n" + body)
                self.assertIsNotNone(result.error)
                self.assertEqual(target.read_bytes(), b"one\ntwo\n")
                self.assertFalse((self.root / "new").exists())

    def test_multiple_numbered_hunks_account_for_insertions_and_deletions(self) -> None:
        target = self.root / "file.txt"
        target.write_bytes(b"one\ntwo\nthree\nfour\n")
        result = self.apply(
            "*** Update File: file.txt\n"
            "@@ -1,0 +2,1 @@\n+inserted\n"
            "@@ -3,1 +3,0 @@\n-three\n"
            "@@ -4,1 +4,1 @@\n-four\n+last"
        )
        self.assertIsNone(result.error)
        self.assertEqual(target.read_bytes(), b"one\ninserted\ntwo\nlast\n")

    def test_creates_shared_missing_parent_directories(self) -> None:
        result = self.apply("*** Add File: src/deep/a.txt\n+a\n*** Add File: src/deep/b.txt\n+b")
        self.assertIsNone(result.error)
        self.assertEqual((self.root / "src/deep/a.txt").read_bytes(), b"a\n")
        self.assertEqual((self.root / "src/deep/b.txt").read_bytes(), b"b\n")

    def test_rejects_parent_file_conflicts_without_creating_anything(self) -> None:
        for body in (
            "*** Add File: new\n+file\n*** Add File: new/file.txt\n+child",
            "*** Add File: new/file.txt\n+child\n*** Add File: new\n+file",
        ):
            with self.subTest(body=body):
                result = self.apply(body)
                self.assertIn("also used as a parent", result.error.message)
                self.assertFalse((self.root / "new").exists())
        (self.root / "blocker").write_bytes(b"file")
        result = self.apply("*** Add File: new/file.txt\n+new\n*** Add File: blocker/deep/file.txt\n+no")
        self.assertEqual(result.error.type, "NotADirectoryError")
        self.assertFalse((self.root / "new").exists())

    def test_deletes_binary_files_without_decoding(self) -> None:
        target = self.root / "file.bin"
        target.write_bytes(b"\xff\xfe\x00")
        result = self.apply("*** Delete File: file.bin")
        self.assertIsNone(result.error)
        self.assertFalse(target.exists())

    def test_reports_partial_failure_through_the_tool_result(self) -> None:
        for name in ("a", "b", "c"):
            (self.root / name).write_bytes(b"old\n")
        body = "\n".join(f"*** Update File: {name}\n@@\n-old\n+new" for name in ("a", "b", "c"))

        def fail_second(path, data):
            if path.name == "b":
                raise PermissionError("locked")
            write_file_bytes(path, data)

        with patch("agent_core.tools.builtin.apply_patch.write_file_bytes", side_effect=fail_second):
            result = self.apply(body)

        self.assertTrue(result.executed)
        self.assertEqual(result.error.type, "PatchApplyError")
        details = json.loads(result.to_content())["error"]["details"]
        self.assertEqual(details["completed"], [{"path": "a", "operation": "updated"}])
        self.assertEqual(details["failed"], {"path": "b", "operation": "updated"})
        self.assertEqual(details["pending"], [{"path": "c", "operation": "updated"}])
        self.assertEqual(details["cause"]["type"], "PermissionError")
        self.assertEqual((self.root / "a").read_bytes(), b"new\n")
        self.assertEqual((self.root / "b").read_bytes(), b"old\n")
        self.assertEqual((self.root / "c").read_bytes(), b"old\n")

    def test_failed_atomic_replace_keeps_original_file_and_cleans_temporary(self) -> None:
        target = self.root / "file.txt"
        target.write_bytes(b"old\n")
        with patch("agent_core.atomic.os.replace", side_effect=PermissionError("locked")):
            result = self.apply("*** Update File: file.txt\n@@\n-old\n+new")
        self.assertEqual(result.error.details["completed"], [])
        self.assertEqual(target.read_bytes(), b"old\n")
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_cancellation_after_preparation_prevents_writes(self) -> None:
        control = TurnControl()
        self.context = ToolExecutionContext(Workspace(self.root), Session(), cancellation=control)
        tools = ToolCatalog([ApplyPatchTool()]).select(["apply_patch"], self.context)

        def cancel_during_validation(*args):
            control.cancel()
            return []

        with patch("agent_core.tools.builtin.apply_patch.missing_parent_directories", side_effect=cancel_during_validation):
            with self.assertRaises(AgentCancelled):
                tools.execute(ToolCall("patch", "apply_patch", {
                    "patch": patch_text("*** Add File: new/file.txt\n+new"),
                }))
        self.assertFalse((self.root / "new").exists())

    def test_matching_diagnostics_include_hunk_and_expected_context(self) -> None:
        (self.root / "file.txt").write_bytes(b"one\ntwo\n")
        result = self.apply("*** Update File: file.txt\n@@\n-one\n+new\n@@\n-missing\n+new")
        self.assertIn("hunk 2", result.error.message)
        self.assertIn("missing", result.error.message)
        self.assertEqual((self.root / "file.txt").read_bytes(), b"one\ntwo\n")

    @unittest.skipIf(os.name == "nt", "POSIX file mode semantics")
    def test_all_file_writers_preserve_executable_permissions(self) -> None:
        for tool in (ApplyPatchTool(), EditFileTool(), WriteFileTool()):
            with self.subTest(tool=tool.name):
                target = self.root / "file.txt"
                target.write_bytes(b"old\n")
                target.chmod(0o751)
                tools = ToolCatalog([tool]).select([tool.name], self.context)
                result = tools.execute(write_call(tool.name))
                self.assertIsNone(result.error)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o751)

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_core import (
    CommandExecutionResult,
    ExecutionRouter,
    JobManager,
    Session,
    ShellTool,
    ToolCall,
    ToolCatalog,
    ToolExecutionContext,
    ToolResult,
    ToolResultNormalizer,
    Workspace,
)
from agent_core.execution.process import _read_spooled_output
from agent_core.session_paths import workspace_key
from agent_core.tools.budget import MAX_TOOL_RESULT_CHARS


class ToolResultNormalizerTest(unittest.TestCase):
    def test_returns_small_result_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            result = ToolResult(
                tool_call_id="call-1",
                name="echo",
                output={"text": "hello"},
            )
            normalizer = ToolResultNormalizer(
                workspace,
                "session-1",
                max_chars=1000,
                preview_chars=20,
                sessions_directory=workspace.path / ".nosis" / "sessions",
            )

            normalized = normalizer.normalize(result)

            self.assertEqual(normalized, result.to_content())
            self.assertFalse((workspace.path / ".nosis" / "sessions").exists())

    def test_does_not_render_the_payload_for_an_inline_result(self) -> None:
        """An inline result must not pay to render a body nobody reads.

        Every tool call takes this path, so rendering the artifact form
        speculatively would cost a second serialization of the output on
        the hot path.
        """
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            result = ToolResult(
                tool_call_id="call-1",
                name="echo",
                output={"text": "hello"},
            )
            normalizer = ToolResultNormalizer(
                workspace,
                "session-1",
                max_chars=1000,
                sessions_directory=workspace.path / ".nosis" / "sessions",
            )

            with patch(
                "agent_core.tool_result.artifact_body"
            ) as render_body:
                normalized = normalizer.normalize(result)

            render_body.assert_not_called()
            self.assertEqual(normalized, result.to_content())

    def test_writes_large_result_and_returns_artifact_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            result = ToolResult(
                tool_call_id="call/1",
                name="echo",
                output={"text": "abcdefghijklmnopqrstuvwxyz"},
            )
            normalizer = ToolResultNormalizer(
                workspace,
                "session-1",
                max_chars=20,
                preview_chars=12,
                sessions_directory=workspace.path / ".nosis" / "sessions",
            )

            normalized = normalizer.normalize(result)

            artifact_path = (
                f".nosis/sessions/{workspace_key(workspace.path)}"
                "/session-1/call%2F1.txt"
            )
            body = json.dumps(result.output, ensure_ascii=False, indent=2)
            self.assertEqual(
                (workspace.path / artifact_path).read_text(
                    encoding="utf-8"
                ),
                body,
            )
            self.assertEqual(
                json.loads(normalized),
                {
                    "artifact_path": artifact_path,
                    "size_chars": len(body),
                    "preview": body[:12],
                    "read_instruction": (
                        "Use read_file with path "
                        f"'{artifact_path}' to read the complete tool "
                        "result. It holds the raw result text, so page "
                        "through it with 'offset' when one read does not "
                        "reach the end."
                    ),
                },
            )

    def test_stores_a_text_result_verbatim(self) -> None:
        """A report is stored as itself, not as an escaped JSON string.

        Wrapping it would collapse every newline into a literal ``\\n``
        and leave the artifact a single line that no paging can walk.
        """
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            report = 'line one\nline two says "hi"\nline three'
            result = ToolResult(
                tool_call_id="call-1",
                name="subagent",
                output=report,
            )
            normalizer = ToolResultNormalizer(
                workspace,
                "session-1",
                max_chars=20,
                sessions_directory=workspace.path / ".nosis" / "sessions",
            )

            normalized = normalizer.normalize(result)

            artifact = (
                workspace.path / json.loads(normalized)["artifact_path"]
            )
            stored = artifact.read_text(encoding="utf-8")
            self.assertEqual(stored, report)
            self.assertNotIn('\\"', stored)
            self.assertEqual(len(stored.splitlines()), 3)

    def test_writes_complete_spooled_result_and_cleans_spool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_path = root / "workspace"
            workspace_path.mkdir()
            workspace = Workspace(workspace_path)
            spool = root / "stdout.txt"
            complete = "START-" + "x" * 70000 + "-END"
            spool.write_text(complete, encoding="utf-8")
            from agent_core.execution import CommandOutputSpool

            handle = CommandOutputSpool(spool, len(complete), "utf-8")
            result = ToolResult(
                tool_call_id="call-1",
                name="shell",
                output={"stdout": complete[:100]},
                artifact_writer=lambda path: path.write_text(
                    json.dumps({"stdout": complete}), encoding="utf-8"
                ) or len(complete),
                artifact_cleanup=handle.cleanup,
            )
            normalizer = ToolResultNormalizer(
                workspace,
                "session-1",
                max_chars=20,
                sessions_directory=root / "sessions",
            )

            normalizer.normalize(result)

            artifact = next((root / "sessions").rglob("call-1.txt"))
            self.assertEqual(
                json.loads(artifact.read_text(encoding="utf-8"))["stdout"],
                complete,
            )
            self.assertFalse(spool.exists())

    def test_cleans_spool_when_custom_preview_exceeds_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            cleanup = Mock()
            result = ToolResult(
                tool_call_id="oversized-preview",
                name="custom",
                artifact_writer=lambda path: path.write_text("full output", encoding="utf-8"),
                artifact_preview=lambda budget: "x" * (budget + 1),
                artifact_cleanup=cleanup,
            )
            normalizer = ToolResultNormalizer(
                workspace, "session", max_chars=1000,
                sessions_directory=workspace.path / "sessions",
            )

            with self.assertRaisesRegex(ValueError, "preview exceeds output limit"):
                normalizer.normalize(result)

            cleanup.assert_called_once()


class ShellArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(self.root)
        self.sessions = self.root / "sessions"
        self.normalizer = ToolResultNormalizer(
            self.workspace, "shell-test", sessions_directory=self.sessions
        )
        self.spool_paths = []

    def _execute(self, stdout, stderr="", *, background=False, command="test command"):
        streams = {}
        spools = {}
        for name, text in (("stdout", stdout), ("stderr", stderr)):
            path = self.root / f"{name}.txt"
            path.write_text(text, encoding="utf-8", newline="")
            streams[name], spools[name] = _read_spooled_output(path)
            if spools[name] is not None:
                self.spool_paths.append(path)
        execution = CommandExecutionResult(
            command=command, exit_code=7, timed_out=True, timeout_seconds=3,
            stdout=streams["stdout"], stderr=streams["stderr"],
            stdout_spool=spools["stdout"], stderr_spool=spools["stderr"],
        )
        executor = SimpleNamespace(execute=lambda *args, **kwargs: execution)
        session = Session("shell-test")
        turn = session.begin_turn("turn")
        jobs = JobManager(session) if background else None
        if jobs is not None:
            self.addCleanup(jobs.close)
            jobs.set_result_normalizer(self.normalizer)
        context = ToolExecutionContext(
            workspace=self.workspace, session=session, sessions_directory=self.sessions,
            execution_router=ExecutionRouter(executor, executor), jobs=jobs,
        )
        tools = ToolCatalog((ShellTool(),)).select(("shell",), context)
        result = tools.execute(ToolCall(
            "shell-call", "shell", {"command": command, "background": background}
        ))
        self.assertIsNone(result.error)
        if jobs is None:
            return result
        jobs.wait_for_turn(turn)
        update = jobs.drain_updates(turn)[-1]
        self.assertEqual(update.status, "completed", update.error)
        return update.output

    def _assert_summary(self, content, *, max_chars=MAX_TOOL_RESULT_CHARS):
        self.assertLessEqual(len(content), max_chars)
        summary = json.loads(content)
        preview = summary["preview"]
        self.assertEqual(preview["exit_code"], 7)
        self.assertTrue(preview["timed_out"])
        self.assertEqual(preview["timeout_seconds"], 3)
        artifact = next(self.sessions.rglob(Path(summary["artifact_path"]).name))
        stored = artifact.read_text(encoding="utf-8")
        self.assertEqual(len(stored), summary["size_chars"])
        self.assertTrue(stored.startswith("$ "))
        for path in self.spool_paths:
            self.assertFalse(path.exists())
        return summary, stored

    def _assert_ends(self, preview, original):
        self.assertTrue(preview.startswith(original[:10]))
        self.assertTrue(preview.endswith(original[-10:]))
        marker = re.compile(r"\n\.\.\. \[truncated (\d+) characters\] \.\.\.\n")
        matches = list(marker.finditer(preview))
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(marker.sub("", preview)) + int(matches[0][1]), len(original))

    def test_40k_without_spool_preserves_both_ends_and_pages_as_text(self) -> None:
        stdout = "OUT-HEAD\n" + "log line\n" * 4500 + "OUT-TAIL\n"
        result = self._execute(stdout, "important error")
        self.assertEqual(self.spool_paths, [])
        summary, stored = self._assert_summary(self.normalizer.normalize(result))
        self._assert_ends(summary["preview"]["stdout"], stdout)
        self.assertEqual(summary["preview"]["stderr"], "important error")
        self.assertIn(stdout, stored)

        from agent_core.tools.builtin.read_file import ReadFileTool

        context = ToolExecutionContext(
            workspace=self.workspace, session=Session(), sessions_directory=self.sessions
        )
        offset = 1
        recovered = []
        for _ in range(20):
            read = ReadFileTool().execute(
                {"path": summary["artifact_path"], "offset": offset}, context
            )
            inline = self.normalizer.normalize(ToolResult("read", "read_file", read))
            self.assertNotIn("artifact_path", json.loads(inline))
            lines = read["content"].splitlines()
            recovered.extend(
                line.split("| ", 1)[1] for line in lines
                if "| " in line and line.split("| ", 1)[0].isdigit()
            )
            if "End of file" in lines[-1]:
                break
            offset = int(lines[-1].split("offset=")[1].split()[0])
        else:
            self.fail("shell artifact paging did not finish")
        self.assertEqual(recovered, stored.splitlines())

    def test_60k_spool_is_summarized_from_original_output(self) -> None:
        stdout = "OUT-HEAD--" + "x" * (60 * 1024) + "--OUT-TAIL"
        result = self._execute(stdout, "important error")
        self.assertEqual(len(self.spool_paths), 1)
        # The final excerpt must not depend on the collector's preview.
        result.output["stdout"] = "collector preview replaced"
        summary, stored = self._assert_summary(self.normalizer.normalize(result))
        self._assert_ends(summary["preview"]["stdout"], stdout)
        self.assertEqual(summary["preview"]["stderr"], "important error")
        self.assertIn(stdout, stored)

    def test_two_20k_streams_share_budget_and_keep_their_tails(self) -> None:
        stdout = "OUT-HEAD--" + "o" * (20 * 1024) + "--OUT-TAIL"
        stderr = "ERR-HEAD--" + "e" * (20 * 1024) + "--ERR-TAIL"
        result = self._execute(stdout, stderr)
        self.assertEqual(self.spool_paths, [])
        summary, stored = self._assert_summary(self.normalizer.normalize(result))
        self._assert_ends(summary["preview"]["stdout"], stdout)
        self._assert_ends(summary["preview"]["stderr"], stderr)
        self.assertIn(stdout, stored)
        self.assertIn(stderr, stored)

    def test_short_stdout_survives_oversized_stderr(self) -> None:
        stderr = "ERR-HEAD--" + "e" * (60 * 1024) + "--ERR-TAIL"
        result = self._execute("short stdout", stderr)
        summary, stored = self._assert_summary(self.normalizer.normalize(result))
        self.assertEqual(summary["preview"]["stdout"], "short stdout")
        self._assert_ends(summary["preview"]["stderr"], stderr)
        self.assertIn(stderr, stored)

    def test_unicode_and_json_escaping_fit_the_final_budget(self) -> None:
        stdout = "OUT-HEAD--" + '中文"\\\n' * 9000 + "--OUT-TAIL"
        result = self._execute(stdout, "short error")
        normalizer = ToolResultNormalizer(
            self.workspace, "shell-test", max_chars=2000, sessions_directory=self.sessions
        )
        summary, stored = self._assert_summary(normalizer.normalize(result), max_chars=2000)
        self._assert_ends(summary["preview"]["stdout"], stdout)
        self.assertEqual(summary["preview"]["stderr"], "short error")
        self.assertIn(stdout, stored)

    def test_long_command_does_not_crowd_out_streams_or_status(self) -> None:
        command = "CMD-HEAD--" + "c" * 40000 + "--CMD-TAIL"
        stdout = "OUT-HEAD--" + "o" * 40000 + "--OUT-TAIL"
        result = self._execute(stdout, "short error", command=command)
        summary, stored = self._assert_summary(self.normalizer.normalize(result))
        self._assert_ends(summary["preview"]["command"], command)
        self._assert_ends(summary["preview"]["stdout"], stdout)
        self.assertEqual(summary["preview"]["stderr"], "short error")
        self.assertIn(command, stored)

    def test_background_shell_keeps_structured_preview_and_cleans_spools(self) -> None:
        stdout = "OUT-HEAD--" + "o" * (60 * 1024) + "--OUT-TAIL"
        stderr = "ERR-HEAD--" + "e" * (60 * 1024) + "--ERR-TAIL"
        summary, stored = self._assert_summary(self._execute(stdout, stderr, background=True))
        self._assert_ends(summary["preview"]["stdout"], stdout)
        self._assert_ends(summary["preview"]["stderr"], stderr)
        self.assertIn(stdout, stored)
        self.assertIn(stderr, stored)


class ArtifactRoundTripTest(unittest.TestCase):
    """A spilled result must be readable back in full.

    An artifact is only useful if the model can reach all of it. These
    cover the round trip end to end: spill a large result, then page it
    back with the same read_file the read_instruction points at.
    """

    def _normalizer(self, workspace: Workspace, sessions: Path):
        return ToolResultNormalizer(
            workspace, "session-1", sessions_directory=sessions
        )

    def _context(self, workspace: Workspace, sessions: Path):
        from agent_core.session import Session
        from agent_core.tools.context import ToolExecutionContext

        return ToolExecutionContext(
            workspace=workspace,
            session=Session(),
            sessions_directory=sessions,
        )

    def test_reading_an_artifact_does_not_spill_again(self) -> None:
        from agent_core.tools.builtin.read_file import ReadFileTool

        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            sessions = workspace.path / ".nosis" / "sessions"
            report = "\n".join(
                f'段落 {index}：报告说 "重要" 的结论。'
                for index in range(2000)
            )
            normalizer = self._normalizer(workspace, sessions)
            spilled = normalizer.normalize(
                ToolResult(
                    tool_call_id="call-1", name="subagent", output=report
                )
            )
            artifact_path = json.loads(spilled)["artifact_path"]

            read = ReadFileTool().execute(
                {"path": artifact_path},
                self._context(workspace, sessions),
            )
            normalized = normalizer.normalize(
                ToolResult(
                    tool_call_id="call-2", name="read_file", output=read
                )
            )

            # The read stays inline, so the recursion stops here.
            self.assertNotIn("artifact_path", json.loads(normalized))
            # And it carries the report's own text, not a re-escaped copy.
            self.assertNotIn('\\"', read["content"])
            self.assertIn('报告说 "重要" 的结论。', read["content"])

    def test_paging_reaches_the_end_of_an_artifact(self) -> None:
        from agent_core.tools.builtin.read_file import ReadFileTool

        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            sessions = workspace.path / ".nosis" / "sessions"
            lines = [f"结论 {index}: finding" for index in range(2000)]
            report = "\n".join(lines)
            spilled = self._normalizer(workspace, sessions).normalize(
                ToolResult(
                    tool_call_id="call-1", name="subagent", output=report
                )
            )
            artifact_path = json.loads(spilled)["artifact_path"]
            tool = ReadFileTool()
            context = self._context(workspace, sessions)

            recovered: list[str] = []
            offset = 1
            for _ in range(100):
                read = tool.execute(
                    {"path": artifact_path, "offset": offset}, context
                )
                body = read["content"].splitlines()
                recovered += [
                    line.split("| ", 1)[1]
                    for line in body
                    if "| " in line and line.split("| ", 1)[0].isdigit()
                ]
                status = body[-1]
                if "End of file" in status:
                    break
                offset = int(status.split("offset=")[1].split()[0])
            else:
                self.fail("paging never reached the end of the artifact")

            self.assertEqual(recovered, lines)


if __name__ == "__main__":
    unittest.main()

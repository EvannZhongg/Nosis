import json
import tempfile
import unittest
from pathlib import Path

from agent_core import (
    ToolResult,
    ToolResultNormalizer,
    Workspace,
)
from agent_core.session_paths import workspace_key


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

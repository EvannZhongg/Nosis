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
            self.assertEqual(
                (workspace.path / artifact_path).read_text(
                    encoding="utf-8"
                ),
                result.to_content(),
            )
            self.assertEqual(
                json.loads(normalized),
                {
                    "artifact_path": artifact_path,
                    "size_chars": len(result.to_content()),
                    "preview": result.to_content()[:12],
                    "read_instruction": (
                        "Use read_file with path "
                        f"'{artifact_path}' to read the complete tool result."
                    ),
                },
            )


if __name__ == "__main__":
    unittest.main()

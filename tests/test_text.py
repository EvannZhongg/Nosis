import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_core.tools.text import MAX_ARTIFACT_LINE_CHARS, write_text_artifact


class TextArtifactTest(unittest.TestCase):
    def test_wraps_only_overlong_lines_and_preserves_line_boundaries(self) -> None:
        cases = (
            ("", ""),
            ("abcd", "abcd"),
            ("abcd\n", "abcd\n"),
            ("abcde", "abcd\ne"),
            ("abcdefgh", "abcd\nefgh"),
            ("abcdefgh\n", "abcd\nefgh\n"),
            ("abcdefghi\n\nlast\n", "abcd\nefgh\ni\n\nlast\n"),
            ('"\\\t\x00中文', '"\\\t\x00\n中文'),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.txt"
            for original, expected in cases:
                with self.subTest(original=original), patch(
                    "agent_core.tools.text.MAX_ARTIFACT_LINE_CHARS", 4
                ):
                    size = write_text_artifact(path, StringIO(original))
                    self.assertEqual(path.read_text(encoding="utf-8"), expected)
                    self.assertEqual(size, len(expected))

    def test_streams_large_input_with_bounded_reads(self) -> None:
        class BoundedSource(StringIO):
            def readline(self, size=-1):
                if not 0 < size <= MAX_ARTIFACT_LINE_CHARS + 1:
                    raise AssertionError("artifact reads must be bounded")
                return super().readline(size)

            def read(self, size=-1):
                raise AssertionError("artifact must stream by bounded lines")

        original = "HEAD" + "界" * 200000 + "TAIL"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.txt"
            size = write_text_artifact(path, BoundedSource(original))
            stored = path.read_text(encoding="utf-8")
            self.assertEqual(stored.replace("\n", ""), original)
            self.assertLessEqual(max(map(len, stored.split("\n"))), MAX_ARTIFACT_LINE_CHARS)
            self.assertEqual(size, len(stored))


if __name__ == "__main__":
    unittest.main()

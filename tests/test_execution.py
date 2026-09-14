import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_core import SubprocessCommandExecutor
from agent_core.execution import (
    MAX_COMMAND_OUTPUT_CHARS,
    CommandOutputSpool,
    _decode_output,
)


def _python_script_command(working_directory: Path, script: str) -> str:
    """Write a helper script into the workspace and run it by name.

    Running a script file keeps the command line free of the quoting that
    differs between shells.
    """
    (working_directory / "command.py").write_text(script, encoding="utf-8")
    return f'"{sys.executable}" command.py'


class SubprocessCommandExecutorTest(unittest.TestCase):
    # Commands below use POSIX syntax on purpose: the shell is /bin/sh on
    # macOS and Linux, and Git Bash on Windows.
    def test_executes_command_in_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = SubprocessCommandExecutor(working_directory)

            result = executor.execute(
                "printf 'hello'; "
                "printf 'warning' >&2; "
                "printf 'marker' > command-output.txt"
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout, "hello")
            self.assertEqual(result.stderr, "warning")
            self.assertFalse(result.timed_out)
            self.assertEqual(result.timeout_seconds, 60)
            self.assertEqual(
                (working_directory / "command-output.txt").read_text(
                    encoding="utf-8"
                ),
                "marker",
            )

    def test_returns_nonzero_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = SubprocessCommandExecutor(Path(directory))

            result = executor.execute("printf 'failed' >&2; exit 7")

            self.assertEqual(result.exit_code, 7)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "failed")

    def test_times_out_and_kills_the_command_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = SubprocessCommandExecutor(working_directory)
            command = _python_script_command(
                working_directory,
                "import time\n"
                "from pathlib import Path\n"
                "time.sleep(3)\n"
                "Path('child-output.txt').write_text('alive')\n",
            )

            result = executor.execute(command, timeout_seconds=1)
            time.sleep(3.5)

            self.assertTrue(result.timed_out)
            self.assertEqual(result.timeout_seconds, 1)
            self.assertFalse(
                (working_directory / "child-output.txt").exists()
            )

    def test_limits_each_output_stream_and_keeps_both_ends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = SubprocessCommandExecutor(working_directory)
            command = _python_script_command(
                working_directory,
                "import sys\n"
                "sys.stdout.write('START-OUT' + 'o' * 70000 + 'END-OUT')\n"
                "sys.stderr.write('START-ERR' + 'e' * 70000 + 'END-ERR')\n",
            )

            result = executor.execute(command)

            self.assertLessEqual(len(result.stdout), MAX_COMMAND_OUTPUT_CHARS)
            self.assertLessEqual(len(result.stderr), MAX_COMMAND_OUTPUT_CHARS)
            self.assertTrue(result.stdout.startswith("START-OUT"))
            self.assertTrue(result.stdout.endswith("END-OUT"))
            self.assertTrue(result.stderr.startswith("START-ERR"))
            self.assertTrue(result.stderr.endswith("END-ERR"))
            self.assertRegex(
                result.stdout,
                r"\[truncated \d+ characters\]",
            )
            self.assertRegex(
                result.stderr,
                r"\[truncated \d+ characters\]",
            )
            if result.stdout_spool is not None:
                result.stdout_spool.cleanup()
            if result.stderr_spool is not None:
                result.stderr_spool.cleanup()

    def test_retains_oversized_streams_in_spools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = SubprocessCommandExecutor(working_directory)
            command = _python_script_command(
                working_directory,
                "import sys\n"
                "sys.stdout.write('START-' + 'o' * 70000 + '-END')\n",
            )

            result = executor.execute(command)

            self.assertIsNotNone(result.stdout_spool)
            assert result.stdout_spool is not None
            self.assertEqual(
                result.stdout_spool.path.read_text(encoding="utf-8"),
                "START-" + "o" * 70000 + "-END",
            )
            result.stdout_spool.cleanup()
            self.assertFalse(result.stdout_spool.path.exists())

    def test_spool_cleanup_ignores_windows_sharing_violation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "locked.txt"
            path.write_text("data", encoding="utf-8")
            spool = CommandOutputSpool(path, 4, "utf-8")
            original_unlink = Path.unlink

            def locked_unlink(self, missing_ok=False):
                if self == path:
                    raise PermissionError(13, "sharing violation")
                return original_unlink(self, missing_ok=missing_ok)

            from unittest.mock import patch

            with patch.object(Path, "unlink", locked_unlink):
                spool.cleanup()

            self.assertTrue(path.exists())

    def test_decodes_output_that_is_not_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = SubprocessCommandExecutor(working_directory)
            command = _python_script_command(
                working_directory,
                "import sys\n"
                "sys.stdout.buffer.write(b'\\xd6\\xd0\\xce\\xc4')\n"
                "sys.stderr.buffer.write(b'\\xd2\\xbb')\n",
            )

            result = executor.execute(command)

            self.assertEqual(result.exit_code, 0)
            self.assertNotEqual(result.stdout, "")
            self.assertNotEqual(result.stderr, "")

    def test_decodes_a_missing_stream_as_empty_text(self) -> None:
        self.assertEqual(_decode_output(None), "")
        self.assertEqual(_decode_output(b""), "")


if __name__ == "__main__":
    unittest.main()

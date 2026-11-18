import os
import sys
import platform
import locale
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import (
    APPROVAL_REQUIRED_AUTHORITY,
    TurnControl,
    CommandCancelled,
    CommandExecutionResult,
    ExecutionRouter,
    FilesystemAccess,
    FULL_ACCESS_AUTHORITY,
    HostCommandExecutor,
    LinuxSandboxBackend,
    MacOSSandboxBackend,
    NetworkAccess,
    ProcessIsolation,
    SandboxBackend,
    SandboxedCommandExecutor,
    SandboxPolicy,
    TemporaryDirectoryMode,
    ToolCall,
    WORKSPACE_ACCESS_AUTHORITY,
    WindowsSandboxBackend,
    platform_workspace_sandbox_backend,
)
from agent_core.execution.process import (
    MAX_COMMAND_OUTPUT_CHARS,
    CommandOutputSpool,
    _SPOOL_PATHS,
    _cleanup_registered_spools,
    _execute_process,
)
from agent_core.execution.sandbox.base import _sandbox_environment


def _python_script_command(working_directory: Path, script: str) -> str:
    """Write a helper script into the workspace and run it by name.

    Running a script file keeps the command line free of the quoting that
    differs between shells.
    """
    (working_directory / "command.py").write_text(script, encoding="utf-8")
    if os.name == "nt":
        return f'& "{sys.executable}" command.py'
    return f'"{sys.executable}" command.py'


class HostCommandExecutorTest(unittest.TestCase):
    def test_rejects_incompatible_helper_before_sending_command(self) -> None:
        real_popen = subprocess.Popen
        for greeting in ('{"protocol_version": 999}', 'not json'):
            with self.subTest(greeting=greeting), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                script = (
                    "import sys\n"
                    "from pathlib import Path\n"
                    f"print({greeting!r}, flush=True)\n"
                    "if sys.stdin.readline(): Path('executed').touch()\n"
                )

                def launch(_argv, **kwargs):
                    return real_popen([sys.executable, "-c", script], cwd=root, **kwargs)

                with patch("agent_core.execution.process._sandbox_executable", return_value=Path("wrong-helper")), patch(
                    "agent_core.execution.process.subprocess.Popen", side_effect=launch,
                ):
                    with self.assertRaisesRegex(RuntimeError, "protocol mismatch.*force-reinstall"):
                        _execute_process(
                            ["unused"], command="unused", working_directory=root,
                            timeout_seconds=10, cancellation=None,
                        )
                self.assertFalse((root / "executed").exists())

    def test_helper_without_handshake_cannot_execute_on_stdin_eof(self) -> None:
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # An old helper waits for EOF before decoding and executing input.
            script = (
                "import sys\nfrom pathlib import Path\n"
                "sys.stdin.read()\nPath('executed').touch()\n"
            )

            def launch(_argv, **kwargs):
                return real_popen([sys.executable, "-c", script], cwd=root, **kwargs)

            with patch("agent_core.execution.process._sandbox_executable", return_value=Path("old-helper")), patch(
                "agent_core.execution.process.subprocess.Popen", side_effect=launch,
            ), patch("agent_core.execution.process._HELPER_STARTUP_TIMEOUT_SECONDS", 0.1):
                with self.assertRaisesRegex(RuntimeError, "missing handshake"):
                    _execute_process(
                        ["unused"], command="unused", working_directory=root,
                        timeout_seconds=10, cancellation=None,
                    )
            self.assertFalse((root / "executed").exists())

    # Shell syntax follows the platform launcher selected by the executor.
    def test_uses_the_injected_shell_launcher(self) -> None:
        calls = []

        def launcher(command: str, working_directory: Path) -> list[str]:
            calls.append((command, working_directory))
            return [sys.executable, "-c", "print('launcher')"]

        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            result = HostCommandExecutor(
                working_directory,
                shell_launcher=launcher,
            ).execute("ignored")

        self.assertEqual(result.stdout.strip(), "launcher")
        self.assertEqual(calls, [("ignored", working_directory)])

    def test_executes_command_in_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = HostCommandExecutor(working_directory)

            command = (
                "[Console]::Write('hello'); "
                "[Console]::Error.Write('warning'); "
                "Set-Content -NoNewline -Path command-output.txt -Value marker"
                if os.name == "nt"
                else "printf 'hello'; printf 'warning' >&2; "
                "printf 'marker' > command-output.txt"
            )
            result = executor.execute(command)

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

    @unittest.skipUnless(platform.system() == "Windows", "Windows shell test")
    def test_host_powershell_output_is_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = HostCommandExecutor(Path(directory)).execute(
                "Get-Process | Select-Object -First 1 | Format-Table"
            )

        self.assertNotIn("\x1b[", result.stdout)
        self.assertNotIn("\x1b[", result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows shell test")
    def test_powershell_stops_when_working_directory_initialization_fails(self) -> None:
        from agent_core.execution.process import _windows_powershell_argv

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = _windows_powershell_argv(
                "Set-Content -LiteralPath 'command-reached' -Value yes",
                root / "missing-workspace",
            )
            result = subprocess.run(argv, cwd=root, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "command-reached").exists())
            self.assertTrue(result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows shell test")
    def test_powershell_working_directory_is_a_literal_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace's [literal]"
            root.mkdir()
            result = HostCommandExecutor(root).execute("Set-Content -NoNewline -LiteralPath result.txt -Value ok")
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual((root / "result.txt").read_text(encoding="utf-8"), "ok")

    @unittest.skipUnless(os.name == "nt", "Windows shell test")
    def test_powershell_stops_before_cleanup_after_setup_errors(self) -> None:
        # Only a marker follows the failing setup; never replay the incident's
        # destructive command against the actual PowerShell HOME variable.
        commands = (
            "$home = Join-Path $PWD 'task-temp'",
            "Get-Item -Path './missing-setup-file'",
            "$cleanupTarget = $undefinedCleanupTarget.FullName",
            f'& "{sys.executable}" -c "import sys; sys.exit(9)"',
        )
        for setup in commands:
            with self.subTest(setup=setup), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = HostCommandExecutor(root).execute(
                    setup + "\nSet-Content -Path 'cleanup-reached' -Value yes"
                )
                self.assertNotEqual(result.exit_code, 0)
                self.assertFalse(result.timed_out)
                self.assertFalse((root / "cleanup-reached").exists())
                self.assertTrue(result.stderr)

    def test_returns_nonzero_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = HostCommandExecutor(Path(directory))

            command = (
                "[Console]::Error.Write('failed'); exit 7"
                if os.name == "nt"
                else "printf 'failed' >&2; exit 7"
            )
            result = executor.execute(command)

            self.assertEqual(result.exit_code, 7)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "failed")

    def test_times_out_and_kills_the_command_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = HostCommandExecutor(working_directory)
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

    def test_cancellation_kills_the_command_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = HostCommandExecutor(working_directory)
            cancellation = TurnControl()
            command = _python_script_command(
                working_directory,
                "import time\n"
                "from pathlib import Path\n"
                "Path('child-ready').touch()\n"
                "time.sleep(3)\n"
                "Path('child-output.txt').write_text('alive')\n",
            )
            errors = []

            def execute() -> None:
                try:
                    executor.execute(command, cancellation=cancellation)
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=execute)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not (working_directory / "child-ready").exists() and thread.is_alive():
                    self.assertLess(time.monotonic(), deadline, "child process did not start")
                    time.sleep(0.01)
                self.assertTrue((working_directory / "child-ready").exists())
            finally:
                cancellation.cancel()
                thread.join(5)
            time.sleep(3.2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], CommandCancelled)
            self.assertFalse(
                (working_directory / "child-output.txt").exists()
            )

    def test_limits_each_output_stream_and_keeps_both_ends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = HostCommandExecutor(working_directory)
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
            executor = HostCommandExecutor(working_directory)
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
            executor = HostCommandExecutor(working_directory)
            command = _python_script_command(
                working_directory,
                "import sys\n"
                "sys.stdout.buffer.write(b'\\xd6\\xd0\\xce\\xc4')\n"
                "sys.stderr.buffer.write(b'\\xd6\\xd0')\n",
            )

            result = executor.execute(command)

            self.assertEqual(result.exit_code, 0)
            encoding = locale.getpreferredencoding(False)
            expected_stdout = b"\xd6\xd0\xce\xc4".decode(encoding, errors="replace")
            expected_stderr = b"\xd6\xd0".decode(encoding, errors="replace")
            self.assertEqual(result.stdout, expected_stdout)
            self.assertEqual(result.stderr, expected_stderr)

    def test_registered_spools_are_removed_at_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nosis-shell-output"
            path.write_text("pending", encoding="utf-8")
            _SPOOL_PATHS.add(path)

            _cleanup_registered_spools()

            self.assertFalse(path.exists())
            self.assertNotIn(path, _SPOOL_PATHS)

    def test_spool_excerpt_uses_detected_locale_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nosis-shell-output"
            path.write_bytes(b"\xd6\xd0\xce\xc4")
            spool = CommandOutputSpool(path, 2, "GBK")

            excerpt = spool.excerpt(10)

            self.assertEqual(excerpt.head, "中文")
            self.assertEqual(excerpt.size_chars, 2)
            spool.cleanup()


class ExecutionAuthorityTest(unittest.TestCase):
    def test_intersection_caps_full_access_to_workspace_access(self) -> None:
        authority = FULL_ACCESS_AUTHORITY.intersect(
            WORKSPACE_ACCESS_AUTHORITY
        )

        self.assertEqual(authority.default_scope.value, "workspace")
        self.assertEqual(authority.maximum_scope.value, "host")
        self.assertEqual(authority.unattended_scope.value, "workspace")

    def test_resolved_route_keeps_its_original_executor_and_authority(self) -> None:
        class Executor:
            def execute(self, command, timeout_seconds=60, cancellation=None):
                raise AssertionError("executor should not run")

            def close(self):
                pass

        authority = FULL_ACCESS_AUTHORITY
        workspace_executor = Executor()
        host_executor = Executor()
        router = ExecutionRouter(
            workspace_executor,
            host_executor,
            authority=lambda: authority,
        )

        route = router.resolve(
            ToolCall("call-1", "shell", {"command": "pwd"})
        )
        authority = APPROVAL_REQUIRED_AUTHORITY

        self.assertIs(route.executor, host_executor)
        self.assertIs(route.authority, FULL_ACCESS_AUTHORITY)


class SandboxedCommandExecutorTest(unittest.TestCase):
    def test_command_hooks_keep_argv_stdin_and_read_paths_inside_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            plugin = root / "plugin"
            workspace.mkdir()
            plugin.mkdir()
            command = ("python", str(plugin / "check.py"))
            for backend_type in (LinuxSandboxBackend, MacOSSandboxBackend):
                with self.subTest(backend=backend_type.__name__):
                    backend = backend_type()
                    self.addCleanup(backend.close)
                    with patch(
                        "agent_core.execution.process._execute_process",
                        return_value=CommandExecutionResult("hook", 0, "{}", ""),
                    ) as execute:
                        SandboxedCommandExecutor(workspace, backend).execute(
                            command, stdin=b"{}", read_only_paths=(plugin,),
                        )
                    argv = execute.call_args.args[0]
                    self.assertEqual(argv, list(command))
                    self.assertEqual(execute.call_args.kwargs["stdin"], b"{}")
                    policy = execute.call_args.kwargs["policy"]
                    self.assertEqual(policy["platform"], "linux" if backend_type is LinuxSandboxBackend else "macos")
                    self.assertEqual(policy["workspace"], str(workspace.resolve()))
                    self.assertEqual(policy["read_only_paths"], [str(plugin.resolve())])

    def test_platform_policies_describe_their_filesystem_boundary(self) -> None:
        for backend_type, filesystem, network in (
            (WindowsSandboxBackend, FilesystemAccess.WRITE_RESTRICTED, NetworkAccess.ALLOW),
            (MacOSSandboxBackend, FilesystemAccess.DENIED, NetworkAccess.DENY),
            (LinuxSandboxBackend, FilesystemAccess.DENIED, NetworkAccess.DENY),
        ):
            with self.subTest(backend=backend_type.__name__):
                backend = backend_type()
                try:
                    executor = SandboxedCommandExecutor(Path.cwd(), backend)
                    self.assertEqual(executor.policy.host_filesystem, filesystem)
                    self.assertEqual(executor.policy.network, network)
                finally:
                    backend.close()

    def test_windows_rejects_strict_host_protection_before_launch(self) -> None:
        backend = WindowsSandboxBackend()
        self.addCleanup(backend.close)
        for access in (FilesystemAccess.READ_ONLY, FilesystemAccess.DENIED):
            with self.subTest(host_filesystem=access), patch(
                "agent_core.execution.process._execute_process"
            ) as execute:
                executor = SandboxedCommandExecutor(
                    Path.cwd(), backend,
                    SandboxPolicy(host_filesystem=access, network=NetworkAccess.ALLOW),
                )
                with self.assertRaisesRegex(ValueError, "does not support"):
                    executor.execute("should not run")
                execute.assert_not_called()

    def test_sandbox_environment_excludes_only_named_variables(self) -> None:
        with patch.dict(
            os.environ,
            {
                "NOSIS_PROVIDER_KEY": "control-plane-secret",
                "USER_API_KEY": "developer-value",
            },
            clear=True,
        ):
            environment = _sandbox_environment({"NOSIS_PROVIDER_KEY"})

        self.assertNotIn("NOSIS_PROVIDER_KEY", environment)
        self.assertEqual(environment["USER_API_KEY"], "developer-value")

    def test_delegates_policy_and_lifecycle_to_backend(self) -> None:
        class RecordingBackend(SandboxBackend):
            def __init__(self) -> None:
                self.calls = []
                self.closed = False

            def execute(
                self,
                command,
                *,
                working_directory,
                policy,
                timeout_seconds,
                cancellation=None,
                excluded_environment_names=(),
                stdin=None,
                read_only_paths=(),
            ):
                self.calls.append(
                    (
                        command,
                        working_directory,
                        policy,
                        timeout_seconds,
                        cancellation,
                    )
                )
                return CommandExecutionResult(command, 0, "ok", "")

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            backend = RecordingBackend()
            executor = SandboxedCommandExecutor(working_directory, backend)

            result = executor.execute("pwd", timeout_seconds=12)
            executor.close()

        self.assertEqual(result.stdout, "ok")
        self.assertEqual(
            executor.policy,
            SandboxPolicy(
                workspace_filesystem=FilesystemAccess.READ_WRITE,
                host_filesystem=FilesystemAccess.DENIED,
                network=NetworkAccess.DENY,
                temporary_directory=TemporaryDirectoryMode.PRIVATE,
                processes=ProcessIsolation.ISOLATED,
            ),
        )
        self.assertEqual(
            backend.calls[0][:4],
            ("pwd", working_directory, executor.policy, 12),
        )
        self.assertTrue(backend.closed)

if __name__ == "__main__":
    unittest.main()

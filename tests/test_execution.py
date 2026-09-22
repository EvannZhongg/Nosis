import os
import sys
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_core import (
    APPROVAL_REQUIRED_AUTHORITY,
    CancellationToken,
    CommandCancelled,
    CommandExecutionResult,
    ExecutionRouter,
    FilesystemAccess,
    FULL_ACCESS_AUTHORITY,
    HostCommandExecutor,
    MacOSSandboxBackend,
    NetworkAccess,
    ProcessIsolation,
    SandboxBackend,
    SandboxedCommandExecutor,
    SandboxPolicy,
    TemporaryDirectoryMode,
    ToolCall,
    WORKSPACE_ACCESS_AUTHORITY,
    platform_workspace_sandbox_backend,
)
from agent_core.execution import MAX_COMMAND_OUTPUT_CHARS, CommandOutputSpool


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
            cancellation = CancellationToken()
            command = _python_script_command(
                working_directory,
                "import time\n"
                "from pathlib import Path\n"
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
            time.sleep(0.2)
            cancellation.cancel()
            thread.join(2)
            time.sleep(3.2)

            self.assertFalse(thread.is_alive())
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
                "sys.stderr.buffer.write(b'\\xd2\\xbb')\n",
            )

            result = executor.execute(command)

            self.assertEqual(result.exit_code, 0)
            self.assertNotEqual(result.stdout, "")
            self.assertNotEqual(result.stderr, "")


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
    @unittest.skipUnless(platform.system() == "Darwin", "macOS Seatbelt test")
    def test_macos_profile_limits_process_interaction_to_same_sandbox(self) -> None:
        profile = MacOSSandboxBackend._PROFILE
        self.assertIn("(allow process-exec)", profile)
        self.assertIn("(allow process-fork)", profile)
        self.assertIn("(allow signal (target same-sandbox))", profile)
        self.assertIn(
            "(allow process-info* (target same-sandbox))",
            profile,
        )
        self.assertNotIn("(allow process*)", profile)

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

    @unittest.skipUnless(platform.system() == "Darwin", "macOS Seatbelt test")
    def test_macos_backend_enforces_workspace_boundary_and_private_tmp(self) -> None:
        probe = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-p",
                "(version 1) (allow default)",
                "/usr/bin/true",
            ],
            capture_output=True,
        )
        if probe.returncode == 71 and b"Operation not permitted" in probe.stderr:
            self.skipTest("the test runner already forbids nested Seatbelt")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            outside = root / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            executor = SandboxedCommandExecutor(
                workspace,
                platform_workspace_sandbox_backend(),
            )
            try:
                clean_result = executor.execute("true")
                write_result = executor.execute(
                    "printf workspace > inside.txt; "
                    "printf temporary > \"$TMPDIR/value.txt\"; "
                    "printf '%s' \"$TMPDIR\""
                )
                private_tmp = Path(write_result.stdout)
                self.assertEqual(
                    (private_tmp / "value.txt").read_text(encoding="utf-8"),
                    "temporary",
                )
                read_result = executor.execute(f'cat "{outside}"')
                network_result = executor.execute(
                    "/usr/bin/curl --connect-timeout 1 http://127.0.0.1:9"
                )
            finally:
                executor.close()

            self.assertEqual(write_result.exit_code, 0)
            self.assertEqual(clean_result.exit_code, 0)
            self.assertEqual(clean_result.stderr, "")
            self.assertEqual(
                (workspace / "inside.txt").read_text(encoding="utf-8"),
                "workspace",
            )
            self.assertNotEqual(private_tmp, Path(tempfile.gettempdir()))
            self.assertFalse(private_tmp.exists())
            self.assertNotEqual(read_result.exit_code, 0)
            self.assertNotIn("secret", read_result.stdout)
            self.assertNotEqual(network_result.exit_code, 0)

    @unittest.skipUnless(platform.system() == "Windows", "Windows ACL test")
    def test_windows_backend_allows_workspace_writes_and_host_reads(self) -> None:
        test_root = Path.cwd() / f".sandbox-test-{time.time_ns()}"
        workspace = test_root / "workspace"
        outside = test_root / "outside.txt"
        blocked = test_root / "blocked.txt"
        world_writable = test_root / "world-writable"
        workspace.mkdir(parents=True)
        world_writable.mkdir()
        self.addCleanup(shutil.rmtree, test_root, ignore_errors=True)
        outside.write_text("host-readable", encoding="utf-8")
        subprocess.run(
            ["icacls", str(world_writable), "/grant", "*S-1-1-0:(OI)(CI)M"],
            capture_output=True,
            text=True,
            check=True,
        )
        acl_before = subprocess.run(
            ["icacls", str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        backend = platform_workspace_sandbox_backend()
        executor = SandboxedCommandExecutor(workspace, backend)
        self.assertEqual(
            executor.policy.host_filesystem, FilesystemAccess.READ_ONLY
        )
        self.assertEqual(executor.policy.network, NetworkAccess.ALLOW)
        try:
            allowed = executor.execute(
                f"Get-Content -LiteralPath '{outside}'; "
                "Set-Content -LiteralPath inside.txt workspace; "
                "Set-Content -LiteralPath "
                "(Join-Path $env:TEMP temp.txt) temporary; "
                "Set-Content chain-a.txt a && Set-Content chain-b.txt b"
            )
            denied = executor.execute(
                "$ErrorActionPreference = 'Stop'; "
                f"Set-Content -LiteralPath '{blocked}' blocked"
            )
            world_denied = executor.execute(
                "$ErrorActionPreference = 'Stop'; "
                f"Set-Content -LiteralPath '{world_writable / 'blocked.txt'}' blocked"
            )
            private_tmp = backend._temporary_directory
            capability_sid = backend._sandbox._workspace_sid_value
            acl_during = subprocess.run(
                ["icacls", str(workspace)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            workspace_text = (workspace / "inside.txt").read_text(
                encoding="utf-8"
            ).strip()
            temporary_text = (private_tmp / "temp.txt").read_text(
                encoding="utf-8"
            ).strip()
            blocked_exists = blocked.exists()
        finally:
            executor.close()
        acl_after = subprocess.run(
            ["icacls", str(workspace)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        child_acl_after = subprocess.run(
            ["icacls", str(workspace / "inside.txt")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        self.assertEqual(allowed.exit_code, 0)
        self.assertIn("host-readable", allowed.stdout)
        self.assertEqual(workspace_text, "workspace")
        self.assertTrue((workspace / "chain-a.txt").exists())
        self.assertTrue((workspace / "chain-b.txt").exists())
        self.assertIsNotNone(private_tmp)
        self.assertEqual(temporary_text, "temporary")
        self.assertNotEqual(denied.exit_code, 0)
        # World SID is required for CLR/Pwsh startup on Windows; a directory
        # explicitly writable by Everyone remains a documented exception.
        self.assertEqual(world_denied.exit_code, 0)
        self.assertTrue((world_writable / "blocked.txt").exists())
        self.assertFalse(blocked_exists)
        self.assertFalse(private_tmp.exists())
        self.assertNotEqual(private_tmp.parent, workspace)
        self.assertIn(capability_sid, acl_during)
        self.assertNotIn(capability_sid, acl_after)
        self.assertNotIn(capability_sid, child_acl_after)
        self.assertEqual(acl_after, acl_before)

    @unittest.skipUnless(platform.system() == "Windows", "Windows ACL test")
    def test_workspace_capability_lease_survives_another_session_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first_backend = platform_workspace_sandbox_backend()
            second_backend = platform_workspace_sandbox_backend()
            first = SandboxedCommandExecutor(workspace, first_backend)
            second = SandboxedCommandExecutor(workspace, second_backend)
            try:
                self.assertEqual(first.execute("Set-Content first.txt one").exit_code, 0)
                self.assertEqual(second.execute("Set-Content second.txt two").exit_code, 0)
                first.close()
                result = second.execute("Set-Content after.txt after")
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(
                    (workspace / "after.txt").read_text(encoding="utf-8"),
                    "after\n",
                )
            finally:
                first.close()
                second.close()

if __name__ == "__main__":
    unittest.main()

"""Real bubblewrap boundaries; run on Linux with user namespaces enabled."""

import os
import platform
import shlex
import shutil
import socket
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import (
    CancellationToken,
    CommandCancelled,
    CommandExecutionResult,
    LinuxSandboxBackend,
    SandboxedCommandExecutor,
    platform_workspace_sandbox_backend,
)


class LinuxSandboxFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.backend = LinuxSandboxBackend()
        self.addCleanup(self.backend.close)
        self.executor = SandboxedCommandExecutor(self.workspace, self.backend)

    def test_missing_bwrap_fails_without_launching_a_command(self) -> None:
        with (
            patch(
                "agent_core.execution.sandbox.linux.shutil.which",
                return_value=None,
            ),
            patch(
                "agent_core.execution.process._execute_process"
            ) as execute,
        ):
            with self.assertRaisesRegex(RuntimeError, "requires bubblewrap"):
                self.executor.execute("touch escaped")
        execute.assert_not_called()
        self.assertFalse((self.workspace / "escaped").exists())

    def test_namespace_failure_preserves_diagnostic_without_host_retry(self) -> None:
        diagnostic = (
            "bwrap: Creating new namespace failed: Operation not permitted\n"
        )
        failure = CommandExecutionResult("touch escaped", 1, "", diagnostic)
        with (
            patch(
                "agent_core.execution.sandbox.linux.shutil.which",
                return_value="/usr/bin/bwrap",
            ),
            patch(
                "agent_core.execution.process._execute_process",
                return_value=failure,
            ) as execute,
        ):
            result = self.executor.execute("touch escaped")
        self.assertIs(result, failure)
        execute.assert_called_once()
        argv = execute.call_args.args[0]
        for namespace in ("user", "pid", "net", "ipc", "uts"):
            self.assertIn(f"--unshare-{namespace}", argv)
            self.assertNotIn(f"--unshare-{namespace}-try", argv)
        self.assertNotIn("--unshare-all", argv)
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertFalse((self.workspace / "escaped").exists())


@unittest.skipUnless(
    platform.system() == "Linux", "Linux bubblewrap integration test"
)
class LinuxSandboxIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        executable = shutil.which("bwrap")
        if executable is None:
            raise unittest.SkipTest("bubblewrap (bwrap) is not installed")
        # Probe the environment independently of the backend. A backend
        # regression after this succeeds must fail, never turn into a skip.
        probe = subprocess.run(
            [
                executable,
                "--unshare-user", "--unshare-pid", "--unshare-net",
                "--unshare-ipc", "--unshare-uts",
                "--ro-bind", "/", "/", "/bin/true",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if probe.returncode:
            if (
                "Operation not permitted" in probe.stderr
                or "No permissions" in probe.stderr
            ):
                # Restricted runners also exercise the backend's failure path:
                # a failed bwrap launch must not execute the command on host.
                with tempfile.TemporaryDirectory() as directory:
                    workspace = Path(directory)
                    backend = LinuxSandboxBackend()
                    try:
                        executor = SandboxedCommandExecutor(workspace, backend)
                        result = executor.execute(
                            "printf escaped > escaped", timeout_seconds=10
                        )
                        if result.exit_code == 0 or (workspace / "escaped").exists():
                            raise AssertionError("namespace failure executed the command")
                        if "bwrap:" not in result.stderr:
                            raise AssertionError(
                                "missing bubblewrap diagnostic: " + result.stderr
                            )
                    finally:
                        backend.close()
                raise unittest.SkipTest(
                    "required namespaces are forbidden: " + probe.stderr.strip()
                )
            raise AssertionError(probe.stderr)
        # The test runner may live in ~/.pyenv or ~/.local, which must not be
        # mounted just to run a probe. Use the system Python inside bwrap.
        cls.sandbox_python = "/usr/bin/python3"
        if not Path(cls.sandbox_python).is_file():
            raise unittest.SkipTest("integration probes require /usr/bin/python3")

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="nosis-boundary-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.backend = platform_workspace_sandbox_backend()
        self.assertIsInstance(self.backend, LinuxSandboxBackend)
        self.addCleanup(self.backend.close)
        self.executor = SandboxedCommandExecutor(self.workspace, self.backend)

    def run_python(self, script: str) -> CommandExecutionResult:
        (self.workspace / "probe.py").write_text(
            textwrap.dedent(script), encoding="utf-8"
        )
        result = self.executor.execute(
            f"{self.sandbox_python} probe.py", timeout_seconds=10
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertFalse(result.timed_out)
        return result

    def test_workspace_rw_and_host_files_denied_including_symlinks_and_home(self) -> None:
        outside = self.root / "secret"
        outside.write_text("host-secret", encoding="utf-8")
        fake_home = self.root / "home"
        tool = fake_home / ".local/bin/private-tool"
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/sh\necho private\n", encoding="utf-8")
        tool.chmod(0o755)
        (self.workspace / "escape").symlink_to(outside)
        (self.workspace / "input").write_text("original", encoding="utf-8")
        with patch.dict(
            os.environ,
            {"HOME": str(fake_home), "PATH": f"{tool.parent}:/usr/bin:/bin"},
        ):
            self.run_python(f"""
                import os
                from pathlib import Path
                assert Path.cwd() == Path({str(self.workspace)!r})
                assert Path('input').read_text() == 'original'
                Path('input').write_text('updated')
                Path('created').write_text('workspace')
                for path in [{str(outside)!r}, 'escape', {str(tool)!r}, '/etc/passwd']:
                    try:
                        Path(path).read_text()
                    except OSError:
                        pass
                    else:
                        raise AssertionError('host file readable: ' + path)
                # This path's parent is recreated in private tmpfs. Writing
                # it may create a shadow file, but must not change the host.
                Path({str(outside)!r}).write_text('shadow')
                assert os.environ['HOME'] == '/tmp'
                assert not Path({str(fake_home)!r}).exists()
            """)
        self.assertEqual(outside.read_text(), "host-secret")
        self.assertEqual((self.workspace / "input").read_text(), "updated")
        self.assertEqual((self.workspace / "created").read_text(), "workspace")

    def test_runtime_mounts_are_read_only(self) -> None:
        self.run_python("""
            import errno
            import os
            from pathlib import Path
            for path in ['/usr/bin/env', '/bin/sh']:
                assert Path(path).read_bytes()
                assert os.statvfs(path).f_flag & os.ST_RDONLY, path
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
                except OSError as error:
                    assert error.errno in (errno.EROFS, errno.EACCES), error
                else:
                    os.close(fd)
                    raise AssertionError('runtime file writable: ' + path)
        """)

    def test_tmp_is_private_from_host_and_between_commands(self) -> None:
        with tempfile.NamedTemporaryFile(prefix="nosis-host-tmp-") as host_file:
            host_tmp = Path(host_file.name)
            private_name = host_tmp.name + "-sandbox"
            self.run_python(f"""
                import os
                from pathlib import Path
                for name in ['HOME', 'TMPDIR', 'TMP', 'TEMP']:
                    assert os.environ[name] == '/tmp'
                assert not Path({str(host_tmp)!r}).exists()
                Path('/tmp/{private_name}').write_text('private')
                assert Path('/tmp/{private_name}').read_text() == 'private'
            """)
            self.assertFalse((Path("/tmp") / private_name).exists())
            self.run_python(f"""
                from pathlib import Path
                assert not Path('/tmp/{private_name}').exists()
            """)

    def test_network_cannot_reach_live_host_tcp_or_abstract_unix_socket(self) -> None:
        with socket.socket() as tcp, socket.socket(socket.AF_UNIX) as unix:
            tcp.bind(("127.0.0.1", 0))
            tcp.listen()
            address = tcp.getsockname()
            abstract = "\0nosis-" + self.root.name
            unix.bind(abstract)
            unix.listen()
            # Both destinations must actually accept host connections.
            with socket.create_connection(address, timeout=1):
                pass
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(abstract)
            self.run_python(f"""
                import socket
                for family, address in [(socket.AF_INET, {address!r}),
                                        (socket.AF_UNIX, {abstract!r})]:
                    with socket.socket(family) as client:
                        client.settimeout(1)
                        try:
                            client.connect(address)
                        except OSError:
                            pass
                        else:
                            raise AssertionError('connected to host socket')
            """)

    def test_namespaces_capabilities_and_host_process_are_isolated(self) -> None:
        namespaces = {
            name: os.readlink(f"/proc/self/ns/{name}")
            for name in ("mnt", "user", "pid", "net", "ipc", "uts")
        }
        host_pid = os.getpid()
        os.kill(host_pid, 0)
        self.run_python(f"""
            import os
            from pathlib import Path
            for name, host in {namespaces!r}.items():
                assert os.readlink('/proc/self/ns/' + name) != host, name
            status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
            for field in ['CapInh', 'CapPrm', 'CapEff', 'CapAmb']:
                assert int(status[field], 16) == 0, (field, status[field])
            assert int(status['NoNewPrivs']) == 1
            assert not Path('/proc/{host_pid}').exists()
            try:
                os.kill({host_pid}, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError('host PID visible')
        """)

    def test_command_errors_are_not_reclassified_as_bwrap_setup_errors(self) -> None:
        result = self.executor.execute(
            "printf 'bwrap: user diagnostic\\n' >&2; exit 7"
        )
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.stderr, "bwrap: user diagnostic\n")

    def test_timeout_and_cancellation_kill_detached_descendants(self) -> None:
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                ready = self.workspace / f"ready-{cancel}"
                escaped = self.workspace / f"escaped-{cancel}"
                child = (
                    "import os, time; from pathlib import Path; os.setsid(); "
                    f"Path({str(ready)!r}).touch(); time.sleep(3); "
                    f"Path({str(escaped)!r}).touch()"
                )
                command = f"{self.sandbox_python} -c {shlex.quote(child)} & wait"
                if cancel:
                    token = CancellationToken()
                    errors = []

                    def execute() -> None:
                        try:
                            self.executor.execute(
                                command, timeout_seconds=10, cancellation=token
                            )
                        except BaseException as error:
                            errors.append(error)

                    thread = threading.Thread(target=execute)
                    thread.start()
                    try:
                        deadline = time.monotonic() + 5
                        while (
                            not ready.exists()
                            and thread.is_alive()
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.01)
                    finally:
                        token.cancel()
                        thread.join(12)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], CommandCancelled)
                else:
                    result = self.executor.execute(command, timeout_seconds=1)
                    self.assertTrue(result.timed_out)
                self.assertTrue(ready.exists())
                time.sleep(3.2)
                self.assertFalse(escaped.exists())


if __name__ == "__main__":
    unittest.main()

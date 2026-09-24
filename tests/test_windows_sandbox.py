import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import (
    ExecutionRouter, HostCommandExecutor, SandboxedCommandExecutor, Session,
    ShellTool, ToolCall, ToolCatalog, ToolExecutionContext, Workspace,
)
from agent_core import windows_sandbox as windows
from agent_core.execution.sandbox.windows import WindowsSandboxBackend


@unittest.skipUnless(os.name == "nt", "Windows ACL tests")
class WindowsSandboxLifecycleTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.lease_directory = self.root / "leases"
        patcher = patch.object(windows, "_CAPABILITY_LEASE_DIRECTORY", self.lease_directory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = windows._WindowsApi()
        self.leases = windows._CapabilityLeases(self.api)
        self.sid_value = windows._capability_sid(str(self.workspace), temporary=False)
        self.sid = self.api.convert_sid(self.sid_value)
        self.addCleanup(self.api.local_free, self.sid)

    def acquire(self):
        return self.leases.acquire(
            self.workspace, self.sid, self.sid_value, capability=True, temporary=False
        )

    def acl(self, path):
        return subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, check=True
        ).stdout

    def expire(self, marker):
        timestamp = time.time() - windows._CAPABILITY_LEASE_TTL_SECONDS - 10
        os.utime(marker, (timestamp, timestamp))

    def test_workspace_validation_checks_write_dac_without_mutating_acl(self):
        before = self.acl(self.workspace)
        self.api.validate_workspace(self.workspace)
        self.assertEqual(self.acl(self.workspace), before)
        with patch.object(
            self.api.kernel32, "CreateFileW", return_value=ctypes.c_void_p(-1).value
        ) as create_file, patch.object(self.api, "error", return_value=OSError(5, "denied")):
            with self.assertRaises(windows.WindowsSandboxWorkspaceError) as caught:
                self.api.validate_workspace(self.workspace)
        self.assertTrue(create_file.call_args.args[1] & windows.WRITE_DAC)
        self.assertIn("Select a directory you own", str(caught.exception))

    def test_denied_workspace_returns_structured_error_and_does_not_start(self):
        backend = WindowsSandboxBackend()
        self.addCleanup(backend.close)
        router = ExecutionRouter(
            SandboxedCommandExecutor(self.workspace, backend),
            HostCommandExecutor(self.workspace),
        )
        context = ToolExecutionContext(
            workspace=Workspace(self.workspace), session=Session(), execution_router=router
        )
        tools = ToolCatalog([ShellTool()]).select(["shell"], context)
        error = windows.WindowsSandboxWorkspaceError(self.workspace, OSError(5, "denied"))
        created = []
        mkdtemp = tempfile.mkdtemp

        def record_tmp(**kwargs):
            result = mkdtemp(**kwargs)
            created.append(Path(result))
            return result

        with patch.object(windows._WindowsApi, "validate_workspace", side_effect=error), \
                patch.object(windows.WindowsWriteRestrictedSandbox, "start") as start, \
                patch("agent_core.execution.sandbox.windows.tempfile.mkdtemp", side_effect=record_tmp):
            result = tools.execute(ToolCall("denied", "shell", {
                "command": "Set-Content should-not-exist.txt bad", "scope": "workspace",
            }))
        payload = json.loads(result.to_content())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["type"], "WindowsSandboxWorkspaceError")
        self.assertIn("command was not executed", payload["error"]["message"])
        start.assert_not_called()
        self.assertFalse((self.workspace / "should-not-exist.txt").exists())
        self.assertTrue(created)
        self.assertTrue(all(not path.exists() for path in created))
        self.assertFalse(list(self.lease_directory.glob("*.lease")))

    def test_startup_reclaims_crash_in_a_different_workspace_and_temp(self):
        before = self.acl(self.workspace)
        script = """
import os
import tempfile
from pathlib import Path
from agent_core import windows_sandbox as windows
windows._CAPABILITY_LEASE_DIRECTORY = Path(os.environ['TEST_LEASES'])
private = Path(tempfile.mkdtemp(prefix='nosis-sandbox-tmp-'))
sandbox = windows.WindowsWriteRestrictedSandbox(Path(os.environ['TEST_WORKSPACE']), private)
Path(os.environ['TEST_PRIVATE']).write_text(str(private))
os._exit(0)
"""
        private_record = self.root / "private.txt"
        environment = dict(os.environ, TEST_LEASES=str(self.lease_directory),
                           TEST_WORKSPACE=str(self.workspace), TEST_PRIVATE=str(private_record))
        subprocess.run([sys.executable, "-c", script], env=environment, check=True)
        private = Path(private_record.read_text())
        self.assertTrue(private.exists())
        self.assertIn(self.sid_value, self.acl(self.workspace))
        backend = WindowsSandboxBackend()
        backend.close()
        self.assertEqual(self.acl(self.workspace), before)
        self.assertFalse(private.exists())
        self.assertFalse(list(self.lease_directory.glob("*.lease")))

    def test_reused_pid_is_stale_even_before_ttl(self):
        marker = self.acquire()
        record = json.loads(marker.read_text())
        with patch.object(self.api, "process_identity", return_value=record["process_started"] + 1):
            self.leases.cleanup_stale()
        self.assertFalse(marker.exists())
        self.assertNotIn(self.sid_value, self.acl(self.workspace))

    def test_dead_process_is_stale_even_before_ttl(self):
        marker = self.acquire()
        with patch.object(self.api, "process_identity", return_value=None):
            self.leases.cleanup_stale()
        self.assertFalse(marker.exists())
        self.assertNotIn(self.sid_value, self.acl(self.workspace))

    def test_ttl_renews_verified_live_session(self):
        marker = self.acquire()
        self.expire(marker)
        self.leases.cleanup_stale()
        self.assertTrue(marker.exists())
        self.assertLess(time.time() - marker.stat().st_mtime, 10)
        self.assertIn(self.sid_value, self.acl(self.workspace))
        self.leases.release(marker)

    def test_unknown_process_keeps_recent_lease_but_expires_after_ttl(self):
        marker = self.acquire()
        with patch.object(self.api, "process_identity", side_effect=OSError(5, "denied")):
            self.leases.cleanup_stale()
            self.assertTrue(marker.exists())
            self.expire(marker)
            self.leases.cleanup_stale()
        self.assertFalse(marker.exists())
        self.assertNotIn(self.sid_value, self.acl(self.workspace))

    def test_active_shared_lease_preserves_grant_when_peer_dies(self):
        first = self.acquire()
        second = self.acquire()
        record = json.loads(first.read_text())
        record["process_started"] += 1
        first.write_text(json.dumps(record))
        self.leases.cleanup_stale()
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertIn(self.sid_value, self.acl(self.workspace))
        self.leases.release(second)
        self.assertNotIn(self.sid_value, self.acl(self.workspace))

    def test_failed_revoke_preserves_recovery_record(self):
        marker = self.acquire()
        with patch.object(self.api, "revoke_write", side_effect=OSError(5, "denied")):
            with self.assertRaises(OSError):
                self.leases.release(marker)
        self.assertTrue(marker.exists())
        self.leases.release(marker)
        self.assertFalse(marker.exists())

    def test_marker_failure_never_grants_access(self):
        with patch.object(Path, "replace", side_effect=OSError(112, "disk full")), \
                patch.object(self.api, "grant_write") as grant:
            with self.assertRaises(OSError):
                self.acquire()
        grant.assert_not_called()
        self.assertEqual(list(self.lease_directory.iterdir()), [])

    def test_temp_removal_failure_preserves_recovery_record(self):
        private = Path(tempfile.mkdtemp(prefix="nosis-sandbox-tmp-")).resolve()
        self.addCleanup(lambda: private.exists() and shutil.rmtree(private))
        sid_value = windows._capability_sid(str(private), temporary=True)
        sid = self.api.convert_sid(sid_value)
        self.addCleanup(self.api.local_free, sid)
        marker = self.leases.acquire(
            private, sid, sid_value, capability=True, temporary=True
        )
        with patch.object(windows.shutil, "rmtree", side_effect=OSError(32, "in use")):
            with self.assertRaises(OSError):
                self.leases.release(marker)
        self.assertTrue(marker.exists())
        self.assertTrue(private.exists())
        self.leases.release(marker)
        self.assertFalse(marker.exists())
        self.assertFalse(private.exists())

    def test_workspace_with_temp_name_is_never_deleted(self):
        workspace = Path(tempfile.mkdtemp(prefix="nosis-sandbox-tmp-")).resolve()
        self.addCleanup(shutil.rmtree, workspace)
        sid_value = windows._capability_sid(str(workspace), temporary=False)
        sid = self.api.convert_sid(sid_value)
        self.addCleanup(self.api.local_free, sid)
        marker = self.leases.acquire(
            workspace, sid, sid_value, capability=True, temporary=False
        )
        self.leases.release(marker)
        self.assertTrue(workspace.exists())

    def test_process_death_between_grant_and_return_remains_recoverable(self):
        def grant_then_exit(*args):
            original_grant(*args)
            raise SystemExit("interrupted")

        original_grant = self.api.grant_write
        with patch.object(self.api, "grant_write", side_effect=grant_then_exit), \
                patch.object(self.api, "revoke_write", side_effect=OSError(5, "denied")), \
                self.assertLogs(windows.__name__, level="WARNING"):
            with self.assertRaises(SystemExit):
                self.acquire()
        self.assertEqual(len(list(self.lease_directory.glob("*.lease"))), 1)
        with patch.object(self.api, "process_identity", return_value=None):
            self.leases.cleanup_stale()
        self.assertFalse(list(self.lease_directory.glob("*.lease")))
        self.assertNotIn(self.sid_value, self.acl(self.workspace))

    def test_constructor_rolls_back_partial_grants_and_temp(self):
        before = self.acl(self.workspace)
        backend = WindowsSandboxBackend()
        self.addCleanup(backend.close)
        with patch.object(windows.WindowsWriteRestrictedSandbox, "_create_restricted_token",
                          side_effect=RuntimeError("token failed")):
            with self.assertRaisesRegex(RuntimeError, "token failed"):
                backend.execute("$null = 1", working_directory=self.workspace,
                                policy=backend.default_policy, timeout_seconds=1)
        self.assertEqual(self.acl(self.workspace), before)
        self.assertIsNone(backend._temporary_directory)
        self.assertFalse(list(self.lease_directory.glob("*.lease")))


if __name__ == "__main__":
    unittest.main()

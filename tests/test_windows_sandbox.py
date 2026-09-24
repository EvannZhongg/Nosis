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
from unittest.mock import Mock, patch

from agent_core import (
    ExecutionRouter, HostCommandExecutor, SandboxedCommandExecutor, Session,
    ShellTool, ToolCall, ToolCatalog, ToolExecutionContext, Workspace,
)
from agent_core import windows_sandbox as windows
from agent_core.execution.sandbox.windows import WindowsSandboxBackend
from agent_runtime.execution_plane import ExecutionPlane


@unittest.skipUnless(os.name == "nt", "Windows ACL tests")
class WindowsSandboxLifecycleTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.user = "*" + subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, check=True,
        ).stdout.split(b",")[-1].strip(b'"\r\n ').decode("ascii")
        # Give the test workspace ordinary user access independent of the
        # elevated runner's system-temp DACL and OWNER RIGHTS entries.
        subprocess.run(
            ["icacls", str(self.workspace), "/grant", f"{self.user}:(OI)(CI)F"],
            capture_output=True, check=True,
        )
        self.lease_directory = self.root / "leases"
        patcher = patch.object(windows, "_CAPABILITY_LEASE_DIRECTORY", self.lease_directory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = windows._WindowsApi()
        self.leases = windows._CapabilityLeases(self.api)
        self.sid_value = windows._capability_sid(str(self.workspace), temporary=False)
        self.sid = self.api.convert_sid(self.sid_value)
        self.addCleanup(self.api.local_free, self.sid)
        self.private = self.root / "private"
        self.private.mkdir()
        self.private_sid_value = windows._capability_sid(str(self.private), temporary=True)
        self.private_sid = self.api.convert_sid(self.private_sid_value)
        self.addCleanup(self.api.local_free, self.private_sid)

    def acquire(self):
        return self.leases.acquire(
            self.private, self.private_sid, self.private_sid_value,
            capability=True, temporary=True,
        )

    def acl(self, path):
        return subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, errors="replace", check=True
        ).stdout

    def acl_entries(self, path):
        acl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = self.api.advapi32.GetNamedSecurityInfoW(
            str(path), windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(acl), None, ctypes.byref(descriptor),
        )
        self.assertEqual(result, 0)
        try:
            entries = []
            count = ctypes.cast(acl, ctypes.POINTER(windows.ACL)).contents.AceCount
            for index in range(count):
                pointer = ctypes.c_void_p()
                self.assertTrue(self.api.advapi32.GetAce(acl, index, ctypes.byref(pointer)))
                ace = ctypes.cast(pointer, ctypes.POINTER(windows.ACCESS_ALLOWED_ACE)).contents
                entries.append((
                    ace.Header.AceType, ace.Header.AceFlags, ace.Mask,
                    self.api.sid_to_string(ctypes.c_void_p(pointer.value + 8)),
                ))
            return entries
        finally:
            self.api.local_free(descriptor)

    def expire(self, marker):
        timestamp = time.time() - windows._CAPABILITY_LEASE_TTL_SECONDS - 10
        os.utime(marker, (timestamp, timestamp))

    def test_unparseable_markers_are_removed_without_repeated_warnings(self):
        valid = self.acquire()
        old_record = json.loads(valid.read_text())
        old_record.pop("released")
        old_record["temporary"] = True
        invalid_contents = [
            b"0", b"1", b"{", b"null", b"{}", bytes.fromhex("ff"),
            json.dumps(old_record).encode("utf-8"),
        ]
        invalid_markers = []
        for index, content in enumerate(invalid_contents):
            marker = self.lease_directory / f"invalid-{index}-deadbeef.lease"
            marker.write_bytes(content)
            invalid_markers.append(marker)
        try:
            with self.assertNoLogs(windows.__name__, level="WARNING"):
                self.leases.cleanup_stale()
                self.assertTrue(all(not marker.exists() for marker in invalid_markers))
                self.leases.cleanup_stale()
            self.assertEqual(list(self.lease_directory.glob("*.lease")), [valid])
        finally:
            self.leases.release(valid)

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

    def test_startup_reclaims_crashed_temp_and_workspace_grant(self):
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
        self.assertNotIn(self.private_sid_value, self.acl(self.private))

    def test_dead_process_is_stale_even_before_ttl(self):
        marker = self.acquire()
        with patch.object(self.api, "process_identity", return_value=None):
            self.leases.cleanup_stale()
        self.assertFalse(marker.exists())
        self.assertNotIn(self.private_sid_value, self.acl(self.private))

    def test_ttl_renews_verified_live_session(self):
        marker = self.acquire()
        self.expire(marker)
        self.leases.cleanup_stale()
        self.assertTrue(marker.exists())
        self.assertLess(time.time() - marker.stat().st_mtime, 10)
        self.assertIn(self.private_sid_value, self.acl(self.private))
        self.leases.release(marker)

    def test_unknown_process_keeps_recent_lease_but_expires_after_ttl(self):
        marker = self.acquire()
        with patch.object(self.api, "process_identity", side_effect=OSError(5, "denied")):
            self.leases.cleanup_stale()
            self.assertTrue(marker.exists())
            self.expire(marker)
            self.leases.cleanup_stale()
        self.assertFalse(marker.exists())
        self.assertNotIn(self.private_sid_value, self.acl(self.private))

    def test_active_shared_lease_preserves_grant_when_peer_dies(self):
        first = self.acquire()
        second = self.acquire()
        record = json.loads(first.read_text())
        record["process_started"] += 1
        first.write_text(json.dumps(record))
        self.leases.cleanup_stale()
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertIn(self.private_sid_value, self.acl(self.private))
        self.leases.release(second)
        self.assertNotIn(self.private_sid_value, self.acl(self.private))

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
        sandbox = windows.WindowsWriteRestrictedSandbox(workspace, self.private)
        sandbox.close()
        self.leases.cleanup_stale()
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
        self.assertNotIn(self.private_sid_value, self.acl(self.private))

    def test_constructor_failure_cleans_temp_and_workspace_grant(self):
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

    def test_execution_plane_grants_once_and_revokes_on_close(self):
        before = self.acl(self.workspace)
        backend = WindowsSandboxBackend()
        executor = SandboxedCommandExecutor(self.workspace, backend)
        plane = ExecutionPlane(
            workspace=Workspace(self.workspace), provider_name="test",
            agent_config=Mock(), configuration_fingerprint="test",
            instructions=Mock(), memory=None, agent=Mock(),
            execution_router=ExecutionRouter(executor, Mock()),
            jobs=Mock(), mcp=Mock(), context_window=Mock(),
        )
        self.addCleanup(plane.close)
        set_acl = self.api.advapi32.SetNamedSecurityInfoW
        with patch.object(windows, "_WindowsApi", return_value=self.api), \
                patch.object(self.api.advapi32, "SetNamedSecurityInfoW", wraps=set_acl) as writes:
            for index in range(3):
                result = executor.execute(f"Set-Content command-{index}.txt value")
                self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual(sum(
                Path(call.args[0]) == self.workspace for call in writes.call_args_list
            ), 1)
            self.assertIn(self.sid_value, self.acl(self.workspace))
            plane.close()
            self.assertEqual(sum(
                Path(call.args[0]) == self.workspace for call in writes.call_args_list
            ), 2)
            writes.reset_mock()
            plane.close()
            writes.assert_not_called()
        self.assertEqual(self.acl(self.workspace), before)
        for child in self.workspace.iterdir():
            self.assertNotIn(self.sid_value, self.acl(child))
        self.assertFalse(list(self.lease_directory.glob("*.lease")))

    def test_workspace_grant_only_adds_capability_ace(self):
        subprocess.run(
            ["icacls", str(self.workspace), "/remove:g", self.user],
            capture_output=True, check=True,
        )
        child = self.workspace / "child"
        child.mkdir()
        before = {path: self.acl_entries(path) for path in (self.workspace, child)}
        sandbox = windows.WindowsWriteRestrictedSandbox(self.workspace, self.private)
        self.addCleanup(sandbox.close)
        for path, entries in before.items():
            during = self.acl_entries(path)
            added = [entry for entry in during if entry not in entries]
            self.assertEqual(len(added), 1)
            self.assertEqual(added[0][-1], self.sid_value)
            self.assertEqual([entry for entry in during if entry not in added], entries)
        sandbox.close()
        for path, entries in before.items():
            self.assertEqual(self.acl_entries(path), entries)

    def test_workspace_close_preserves_unrelated_acl_changes(self):
        sandbox = windows.WindowsWriteRestrictedSandbox(self.workspace, self.private)
        self.addCleanup(sandbox.close)
        other_sid_value = "S-1-4-112233-445566"
        other_sid = self.api.convert_sid(other_sid_value)
        self.addCleanup(self.api.local_free, other_sid)
        self.api.grant_write(self.workspace, other_sid)
        sandbox.close()
        after = self.acl(self.workspace)
        self.assertIn(other_sid_value, after)
        self.assertNotIn(self.sid_value, after)

    def test_capability_does_not_make_original_readonly_children_writable(self):
        child = self.workspace / "readonly"
        child.mkdir()
        existing = child / "existing.txt"
        existing.write_text("original", encoding="utf-8")
        # The root is writable, but only read access is inherited by children.
        subprocess.run(
            ["icacls", str(self.workspace), "/inheritance:r", "/grant:r",
             f"{self.user}:(OI)(CI)(RX)"], capture_output=True, check=True,
        )
        subprocess.run(
            ["icacls", str(self.workspace), "/grant", f"{self.user}:(F)"],
            capture_output=True, check=True,
        )
        self.addCleanup(
            subprocess.run,
            ["icacls", str(self.workspace), "/grant", f"{self.user}:(OI)(CI)F"],
            capture_output=True, check=True,
        )
        self.api.validate_workspace(self.workspace)
        with self.assertRaises(PermissionError):
            existing.write_text("host write", encoding="utf-8")
        before = self.acl_entries(child)
        backend = WindowsSandboxBackend()
        self.addCleanup(backend.close)
        executor = SandboxedCommandExecutor(self.workspace, backend)
        allowed = executor.execute("Set-Content root.txt allowed")
        self.assertEqual(allowed.exit_code, 0, allowed.stderr)
        self.assertIn(self.sid_value, self.acl(child))
        for target in ("readonly/existing.txt", "readonly/new.txt"):
            denied = executor.execute(
                "$ErrorActionPreference = 'Stop'; "
                f"Set-Content -LiteralPath '{target}' denied"
            )
            self.assertNotEqual(denied.exit_code, 0)
        self.assertEqual(existing.read_text(encoding="utf-8"), "original")
        self.assertFalse((child / "new.txt").exists())
        backend.close()
        self.assertEqual(self.acl_entries(child), before)

    def test_failed_workspace_propagation_rolls_back_grant(self):
        child = self.workspace / "child"
        child.mkdir()
        before = {path: self.acl(path) for path in (self.workspace, child)}
        set_acl = self.api.advapi32.SetNamedSecurityInfoW
        failed = False

        def write_root_then_report_failure(path, *args):
            nonlocal failed
            result = set_acl(path, *args)
            if Path(path) == self.workspace and not failed:
                failed = True
                self.assertEqual(result, 0)
                return 5
            return result

        with patch.object(windows, "_WindowsApi", return_value=self.api), \
                patch.object(self.api.advapi32, "SetNamedSecurityInfoW",
                             side_effect=write_root_then_report_failure):
            with self.assertRaises(windows.WindowsSandboxWorkspaceError):
                windows.WindowsWriteRestrictedSandbox(self.workspace, self.private)
        for path, acl in before.items():
            self.assertEqual(self.acl(path), acl)
        self.assertFalse(list(self.lease_directory.glob("*.lease")))

    def test_workspace_lease_failure_prevents_acl_write(self):
        set_acl = self.api.advapi32.SetNamedSecurityInfoW
        with patch.object(windows, "_WindowsApi", return_value=self.api), \
                patch.object(Path, "replace", side_effect=OSError(112, "disk full")), \
                patch.object(self.api.advapi32, "SetNamedSecurityInfoW", wraps=set_acl) as writes:
            with self.assertRaises(windows.WindowsSandboxWorkspaceError):
                windows.WindowsWriteRestrictedSandbox(self.workspace, self.private)
        writes.assert_not_called()
        self.assertFalse(self.api.has_write_grant(self.workspace, self.sid))

    def test_failed_workspace_revoke_finishes_propagation_without_root_ace(self):
        marker = self.leases.acquire(
            self.workspace, self.sid, self.sid_value, capability=True, temporary=False,
        )
        self.addCleanup(self.leases.release, marker)
        set_acl = self.api.advapi32.SetNamedSecurityInfoW

        def write_then_report_failure(*args):
            self.assertEqual(set_acl(*args), 0)
            return 5

        with patch.object(self.api.advapi32, "SetNamedSecurityInfoW",
                          side_effect=write_then_report_failure):
            with self.assertRaises(OSError):
                self.leases.release(marker)
        self.assertNotIn(self.sid_value, self.acl(self.workspace))
        self.assertTrue(marker.exists())
        with patch.object(self.api.advapi32, "SetNamedSecurityInfoW", wraps=set_acl) as writes:
            self.leases.release(marker)
        writes.assert_called_once()
        self.assertFalse(marker.exists())

    def test_live_workspace_lease_is_shared_across_processes(self):
        before = self.acl(self.workspace)
        first = windows.WindowsWriteRestrictedSandbox(self.workspace, self.private)
        self.addCleanup(first.close)
        child_private = self.root / "child-private"
        child_private.mkdir()
        script = """
import os
from pathlib import Path
from unittest.mock import patch
from agent_core import windows_sandbox as windows
windows._CAPABILITY_LEASE_DIRECTORY = Path(os.environ['TEST_LEASES'])
workspace = Path(os.environ['TEST_WORKSPACE'])
api = windows._WindowsApi()
set_acl = api.advapi32.SetNamedSecurityInfoW
def check_write(path, *args):
    if Path(path) == workspace:
        raise AssertionError('a live workspace lease must not rewrite its ACL')
    return set_acl(path, *args)
with patch.object(windows, '_WindowsApi', return_value=api):
    with patch.object(api.advapi32, 'SetNamedSecurityInfoW', side_effect=check_write):
        sandbox = windows.WindowsWriteRestrictedSandbox(workspace, Path(os.environ['TEST_PRIVATE']))
        sandbox.close()
"""
        subprocess.run(
            [sys.executable, "-c", script], check=True,
            env=dict(os.environ, TEST_LEASES=str(self.lease_directory),
                     TEST_WORKSPACE=str(self.workspace), TEST_PRIVATE=str(child_private)),
        )
        self.assertIn(self.sid_value, self.acl(self.workspace))
        first.close()
        self.assertEqual(self.acl(self.workspace), before)
        self.assertFalse(list(self.lease_directory.glob("*.lease")))

    def test_active_workspace_is_not_writable_by_another_workspace_token(self):
        sandbox = windows.WindowsWriteRestrictedSandbox(self.workspace, self.private)
        self.addCleanup(sandbox.close)
        other = self.root / "other-workspace"
        other.mkdir()
        subprocess.run(
            ["icacls", str(other), "/grant", f"{self.user}:(OI)(CI)F"],
            capture_output=True, check=True,
        )
        executor = SandboxedCommandExecutor(other, WindowsSandboxBackend())
        try:
            allowed = executor.execute("Set-Content own.txt allowed")
            self.assertEqual(allowed.exit_code, 0, allowed.stderr)
            result = executor.execute(
                "$ErrorActionPreference = 'Stop'; "
                f"Set-Content -LiteralPath '{self.workspace / 'blocked.txt'}' blocked"
            )
        finally:
            executor.close()
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse((self.workspace / "blocked.txt").exists())

    def test_router_close_logs_real_sharing_violation_and_startup_recovers(self):
        private = Path(tempfile.mkdtemp(prefix="nosis-sandbox-tmp-")).resolve()
        self.addCleanup(lambda: private.exists() and shutil.rmtree(private))
        backend = WindowsSandboxBackend()
        backend._temporary_directory = private
        backend._sandbox = windows.WindowsWriteRestrictedSandbox(self.workspace, private)
        self.addCleanup(backend.close)
        host = Mock()
        router = ExecutionRouter(SandboxedCommandExecutor(self.workspace, backend), host)
        locked = (private / "spool.txt").open("wb")
        try:
            with self.assertLogs("agent_core.execution.sandbox.windows", level="WARNING") as logs:
                router.close()
            host.close.assert_called_once()
            self.assertIn("cleanup incomplete", logs.output[0])
            self.assertIsNone(backend._sandbox)
            self.assertIsNone(backend._temporary_directory)
            markers = list(self.lease_directory.glob("*.lease"))
            self.assertTrue(markers)
            self.assertTrue(all(json.loads(marker.read_text())["released"] for marker in markers))
            self.assertTrue(private.exists())
            with self.assertLogs(windows.__name__, level="WARNING"):
                another = WindowsSandboxBackend()
            another.close()
        finally:
            locked.close()
        # The PID is still alive, but the durable release permits reclamation.
        another = WindowsSandboxBackend()
        another.close()
        self.assertFalse(private.exists())
        self.assertFalse(list(self.lease_directory.glob("*.lease")))
        result = backend.execute(
            "Write-Output restarted", working_directory=self.workspace,
            policy=backend.default_policy, timeout_seconds=10,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "restarted")
        self.assertNotEqual(backend._temporary_directory, private)
        backend.close()
        self.assertIsNone(backend._sandbox)
        self.assertIsNone(backend._temporary_directory)
        self.assertEqual(backend._pending_cleanup, [])

    def test_backend_close_logs_revoke_failure_and_retries_remaining_lease(self):
        backend = WindowsSandboxBackend()
        backend._temporary_directory = self.private
        backend._sandbox = windows.WindowsWriteRestrictedSandbox(self.workspace, self.private)
        self.addCleanup(backend.close)
        with patch.object(backend._sandbox._api, "revoke_write", side_effect=OSError(5, "denied")), \
                self.assertLogs("agent_core.execution.sandbox.windows", level="WARNING"):
            backend.close()
        self.assertTrue(list(self.lease_directory.glob("*.lease")))
        self.assertTrue(self.private.exists())
        backend.close()
        self.assertFalse(list(self.lease_directory.glob("*.lease")))
        self.assertFalse(self.private.exists())

    def test_execute_rebuilds_after_failed_sandbox_close(self):
        backend = WindowsSandboxBackend()
        self.addCleanup(backend.close)
        executor = SandboxedCommandExecutor(self.workspace, backend)
        self.assertEqual(executor.execute("Write-Output first").exit_code, 0)
        old_sandbox = backend._sandbox
        old_tmp = backend._temporary_directory
        locked = (old_tmp / "spool.txt").open("wb")
        try:
            with self.assertLogs("agent_core.execution.sandbox.windows", level="WARNING"):
                backend.close()
            self.assertIsNone(backend._sandbox)
            self.assertIsNone(backend._temporary_directory)
            result = executor.execute(
                "Set-Content -LiteralPath (Join-Path $env:TEMP new.txt) new; "
                "Write-Output $env:TEMP"
            )
            self.assertEqual(result.exit_code, 0)
            new_tmp = backend._temporary_directory
            self.assertNotEqual(new_tmp, old_tmp)
            self.assertIsNot(backend._sandbox, old_sandbox)
            self.assertEqual(Path(result.stdout.strip()), new_tmp)
            self.assertTrue((new_tmp / "new.txt").exists())
            # One stale resource must not prevent closing the new sandbox.
            with self.assertLogs("agent_core.execution.sandbox.windows", level="WARNING"):
                backend.close()
            self.assertFalse(new_tmp.exists())
            self.assertTrue(old_tmp.exists())
        finally:
            locked.close()
        backend.close()
        self.assertFalse(old_tmp.exists())
        self.assertEqual(backend._pending_cleanup, [])
        self.assertFalse(list(self.lease_directory.glob("*.lease")))

    def test_unleased_temp_cleanup_failure_is_detached_before_next_execute(self):
        for reclaimed in (False, True):
            with self.subTest(reclaimed=reclaimed):
                old_tmp = self.root / f"unleased-{reclaimed}"
                old_tmp.mkdir()
                backend = WindowsSandboxBackend()
                self.addCleanup(backend.close)
                backend._temporary_directory = old_tmp
                with patch("agent_core.execution.sandbox.windows.shutil.rmtree",
                           side_effect=OSError(32, "in use")), \
                        self.assertLogs("agent_core.execution.sandbox.windows", level="WARNING"):
                    backend.close()
                self.assertIsNone(backend._temporary_directory)
                if reclaimed:
                    shutil.rmtree(old_tmp)
                result = backend.execute(
                    "Write-Output $env:TEMP", working_directory=self.workspace,
                    policy=backend.default_policy, timeout_seconds=10,
                )
                self.assertEqual(result.exit_code, 0)
                new_tmp = backend._temporary_directory
                self.assertNotEqual(new_tmp, old_tmp)
                self.assertEqual(Path(result.stdout.strip()), new_tmp)
                self.assertEqual(old_tmp.exists(), not reclaimed)
                backend.close()
                self.assertFalse(old_tmp.exists())
                self.assertFalse(new_tmp.exists())
                self.assertEqual(backend._pending_cleanup, [])

    def test_setup_error_survives_cleanup_failure(self):
        backend = WindowsSandboxBackend()
        self.addCleanup(backend.close)
        with patch.object(windows.WindowsWriteRestrictedSandbox, "_create_restricted_token",
                          side_effect=RuntimeError("token failed")), \
                patch.object(windows._WindowsApi, "revoke_write", side_effect=OSError(5, "denied")), \
                self.assertLogs(level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "token failed"):
                backend.execute("$null = 1", working_directory=self.workspace,
                                policy=backend.default_policy, timeout_seconds=1)
        self.assertTrue(list(self.lease_directory.glob("*.lease")))
        self.leases.cleanup_stale()
        self.assertFalse(list(self.lease_directory.glob("*.lease")))


if __name__ == "__main__":
    unittest.main()

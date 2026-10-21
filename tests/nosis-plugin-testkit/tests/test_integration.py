"""Black-box tests: only MCP, OpenAI-compatible HTTP and Bridge NDJSON.

Run with the Python environment in which Nosis is installed. Bridge tests use a
Windows Job to reclaim all test processes, including independently owned servers.
The plugin itself and its MCP test are platform independent.
"""

import asyncio
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
TOOL = "mcp__repo-review-kit_analyzer__analyze_code"
SOURCE = "value = eval(user_input)\n"


def call(tool_name, **arguments):
    return {"name": tool_name, "arguments": arguments}


class LocalProvider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        scripts = self.server.scripts[body["model"]]
        if not scripts:
            self.send_error(500, "No scripted response remains")
            return
        response = scripts.pop(0)
        if isinstance(response, str):
            delta, finish = {"content": response}, "stop"
        else:
            delta = {"tool_calls": [{"index": 0, "id": f"call-{len(self.server.requests)}", "type": "function", "function": {
                "name": response["name"], "arguments": json.dumps(response["arguments"]),
            }}]}
            finish = "tool_calls"
        event = {"id": "test-response", "object": "chat.completion.chunk", "created": 1,
                 "model": body["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        payload = ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ProcessJob:
    """Generic Windows process isolation, independent of Nosis internals."""

    def __init__(self, process):
        from ctypes import wintypes as w

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [w.LPVOID, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            class Accounting(ctypes.Structure):
                _fields_ = [("times", ctypes.c_int64 * 4), ("counts", ctypes.c_uint32 * 4)]

            self.kernel.TerminateJobObject(self.handle, 1)
            deadline = time.monotonic() + 10
            try:
                while True:
                    state = Accounting()
                    if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(state), ctypes.sizeof(state), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if state.counts[2] == 0:  # ActiveProcesses
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Test processes did not exit")
                    time.sleep(0.01)
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


class BridgeProcess:
    def __init__(self, home, workspace):
        self.log = (home / "bridge-stderr.log").open("w+b")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "interfaces.bridge"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, encoding="utf-8",
            env={
                **os.environ, "USERPROFILE": str(home), "HOME": str(home),
                "PYTHONUTF8": "1", "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            },
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.job = ProcessJob(self.process)
        self.queue = queue.Queue()
        self.messages = []
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        self.send(type="open_session", workspace=str(workspace), session_id=None)
        try:
            self.until("session_ready")
        except BaseException:
            self.close()
            raise

    def read(self):
        try:
            for line in self.process.stdout:
                self.queue.put(json.loads(line))
        finally:
            self.queue.put(None)

    def send(self, **message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def until(self, wanted, *, approved=True):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            message = self.queue.get(timeout=max(0.01, deadline - time.monotonic()))
            if message is None:
                self.log.seek(0)
                raise AssertionError(self.log.read().decode("utf-8", errors="replace"))
            self.messages.append(message)
            if message["type"] == "approval_request":
                self.send(type="approval_response", request_id=message["request_id"], approved=approved)
            if message["type"] in ("fatal", "turn_failed", "turn_cancelled"):
                raise AssertionError(message)
            if message["type"] == wanted:
                return message
        raise AssertionError(f"Timed out waiting for {wanted}")

    def turn(self, *, approved=True):
        self.send(type="user_turn", turn_id="review", text="Review sample.py using the code-review Skill.")
        return self.until("turn_completed", approved=approved)

    def close(self):
        try:
            if self.process.poll() is None:
                self.send(type="shutdown")
                self.process.wait(timeout=15)
        finally:
            self.job.close()
            self.process.wait(timeout=15)
            self.reader.join(timeout=5)
            self.process.stdin.close()
            self.process.stdout.close()
            self.log.close()


class McpServerTest(unittest.TestCase):
    def test_discovery_call_and_error_isolation(self):
        async def exercise():
            parameters = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp" / "server.py")])
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({tool.name for tool in tools.tools}, {"analyze_code", "fail_review", "internal_probe"})
                    failed = await session.call_tool("fail_review", {})
                    self.assertTrue(failed.is_error)
                    result = await session.call_tool("analyze_code", {"source": SOURCE})
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content["count"], 1)
                    self.assertEqual(result.structured_content["findings"][0]["line"], 1)
        asyncio.run(exercise())


@unittest.skipUnless(os.name == "nt", "Bridge process cleanup in this test harness uses Windows Jobs")
class BridgeIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        (self.workspace / "sample.py").write_text(SOURCE, encoding="utf-8")
        self.config = self.home / ".nosis"
        self.plugin = self.config / "plugins" / "repo-review-kit"
        shutil.copytree(ROOT, self.plugin, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"))
        servers = self.plugin / "mcp" / "servers.json"
        document = json.loads(servers.read_text(encoding="utf-8"))
        document["analyzer"]["command"] = sys.executable
        servers.write_text(json.dumps(document), encoding="utf-8")
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), LocalProvider)
        self.http.requests = []
        self.http.scripts = {
            "gpt-4": [call("read_skill", name="repo-review-kit:code-review"),
                      call("subagent", role="repo-review-kit:reviewer", task="Review sample.py"), "Review delivered."],
            "gpt-3.5-turbo": [call("read_file", path="sample.py"), call(TOOL, source=SOURCE), "Reviewer findings."],
        }
        thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        providers = {name: {"model": model, "api": "chat_completions", "key": "local-test",
                            "url": f"http://127.0.0.1:{self.http.server_port}/v1", "max_context_tokens": 128000}
                     for name, model in (("main", "gpt-4"), ("reviewer", "gpt-3.5-turbo"))}
        self.json_file(self.config / "provider_config.json", {
            "main_agent": {"provider": "main"}, "subagent": {"provider": "reviewer"}, "providers": providers,
        })
        self.json_file(self.config / "agent_config.json", {
            "max_same_tool_calls": 5, "output_reserve_tokens": 1000, "workspace_instruction_files": [],
            "provider": {"request_timeout_seconds": 15, "max_retries": 0},
            "skills": {"global_paths": [], "workspace_paths": []},
            "memory": {"enabled": False}, "main_agent": {"tools": {"subagent": True}}, "mcp": {"enabled": True},
        })
        self.json_file(self.config / "tool_config.json", {"web_search": {"provider": "duckduckgo"}, "web_fetch": {"provider": "direct"}})
        prompts = self.config / "prompts"
        prompts.mkdir()
        for name, content in {
            "Soul.md": "Review sources in {{workspace}}.",
            "SubAgent.md": "Role {{role}}: {{role_description}} in {{workspace}}.",
            "Consolidator.md": "Consolidate.", "GlobalMemory.md": "Global memory.", "WorkspaceMemory.md": "Workspace memory.",
        }.items():
            (prompts / name).write_text(content, encoding="utf-8")

    @staticmethod
    def json_file(path, document):
        path.write_text(json.dumps(document), encoding="utf-8")

    def bridge(self):
        bridge = BridgeProcess(self.home, self.workspace)
        self.addCleanup(bridge.close)
        return bridge

    def test_skill_reviewer_mcp_and_approval(self):
        bridge = self.bridge()
        bridge.send(type="settings_get", request_id="components")
        snapshot = bridge.until("settings_snapshot")["settings"]
        self.assertIn("repo-review-kit:code-review", {skill["id"] for skill in snapshot["skills"]})
        self.assertIn("repo-review-kit:reviewer", {agent["name"] for agent in snapshot["plugin_agents"]})
        self.assertIn("repo-review-kit:analyzer", {server["id"] for server in snapshot["mcp_servers"]})
        bridge.turn()
        self.assertTrue(any(message["type"] == "mcp_server_status" and message["server"] == "repo-review-kit:analyzer" and message["status"] == "ready" for message in bridge.messages))
        approvals = [message for message in bridge.messages if message["type"] == "approval_request"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["tool_name"], "analyze_code")
        child = [request for request in self.http.requests if request["model"] == "gpt-3.5-turbo"]
        names = {tool["function"]["name"] for tool in child[0]["tools"]}
        self.assertIn(TOOL, names)
        self.assertNotIn("subagent", names)
        self.assertNotIn("mcp__repo-review-kit_analyzer__internal_probe", names)
        self.assertNotIn("mcp__repo-review-kit_analyzer__fail_review", names)
        self.assertIn('"count": 1', str(child[-1]["messages"]))

    def test_denied_child_call_is_returned_to_reviewer(self):
        bridge = self.bridge()
        bridge.turn(approved=False)
        child = [request for request in self.http.requests if request["model"] == "gpt-3.5-turbo"]
        self.assertIn("PermissionError", str(child[-1]["messages"]))
        self.assertNotIn('"count": 1', str(child[-1]["messages"]))

    def test_disabled_plugin_contributes_no_components(self):
        manifest = self.plugin / "plugin.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["enabled"] = False
        self.json_file(manifest, document)
        self.http.scripts["gpt-4"] = ["No plugin available."]
        bridge = self.bridge()
        bridge.turn()
        names = {tool["function"]["name"] for tool in self.http.requests[0].get("tools", [])}
        self.assertFalse(any(name.startswith("mcp__repo-review-kit_") for name in names))
        self.assertNotIn("subagent", names)
        self.assertNotIn("read_skill", names)
        self.assertFalse(any(
            message["type"] == "mcp_server_status"
            and message["server"].startswith("repo-review-kit:")
            for message in bridge.messages
        ))

    def test_invalid_role_and_failed_server_do_not_block_healthy_plugin(self):
        broken = self.config / "plugins" / "broken"
        (broken / "agents").mkdir(parents=True)
        self.json_file(broken / "plugin.json", {"name": "broken", "components": {"agents": ["agents/bad.md"], "mcp": ["servers.json"]}})
        (broken / "agents" / "bad.md").write_text("---\nname: bad\ndescription: Invalid role\ntools: [subagent]\n---\nNo recursion.\n", encoding="utf-8")
        self.json_file(broken / "servers.json", {"unavailable": {"command": str(self.home / "missing-python")}})
        bridge = self.bridge()
        bridge.turn()
        self.assertTrue(any(message["type"] == "mcp_server_status" and message["server"] == "broken:unavailable" and message["status"] == "unavailable" for message in bridge.messages))
        self.assertTrue(any("unknown agent tool(s): subagent" in str(message.get("runtime_warnings", [])) for message in bridge.messages))

    def test_mcp_tool_error_does_not_abort_next_call(self):
        self.http.scripts["gpt-4"] = [call("mcp__repo-review-kit_analyzer__fail_review"), call(TOOL, source=SOURCE), "Recovered review."]
        bridge = self.bridge()
        bridge.turn()
        transcript = str(self.http.requests[-1]["messages"])
        self.assertIn("Deliberate review failure", transcript)
        self.assertIn('"count": 1', transcript)

    def test_filtered_tool_skips_reviewer_and_keeps_skill(self):
        path = self.plugin / "mcp" / "servers.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["analyzer"]["tools"]["enabled"] = ["fail_review"]
        self.json_file(path, document)
        self.http.scripts["gpt-4"] = ["Reviewer unavailable."]
        bridge = self.bridge()
        bridge.turn()
        names = {tool["function"]["name"] for tool in self.http.requests[0]["tools"]}
        self.assertIn("read_skill", names)
        self.assertNotIn("subagent", names)
        self.assertNotIn(TOOL, names)
        self.assertTrue(any("after Tool Catalog assembly" in str(message.get("runtime_warnings", [])) for message in bridge.messages))


if __name__ == "__main__":
    unittest.main()

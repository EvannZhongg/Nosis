"""Installed CLI + packaged SDK black-box tests. No production imports or patches.

Prerequisites: npm install, Python runtime dependencies, pip and setuptools>=68.
Run: python -m unittest tests.test_bridge_client -v
"""

import importlib.util
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest


ROOT = Path(__file__).resolve().parents[1]
NPM = shutil.which("npm.cmd" if os.name == "nt" else "npm")


class Peer:
    def __init__(self, command, cwd, env):
        self.process = subprocess.Popen(
            [str(command)], cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        self.events = Queue()
        self.messages = []
        self.stderr = []
        self.reader = Thread(target=self._read, daemon=True)
        self.diagnostics = Thread(target=lambda: self.stderr.extend(self.process.stderr), daemon=True)
        self.reader.start()
        self.diagnostics.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.events.put(json.loads(line))
        except Exception as error:
            self.events.put(error)
        finally:
            self.events.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def until(self, kind):
        while True:
            event = self.events.get(timeout=20)
            if event is None:
                raise AssertionError(f"Bridge closed before {kind}: {self.messages}; {self.stderr}")
            if isinstance(event, Exception):
                raise event
            self.messages.append(event)
            if event["type"] == kind:
                return event
            if event["type"] == "fatal":
                raise AssertionError(event)

    def close(self):
        if self.process.poll() is None:
            self.send({"type": "shutdown"})
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self.reader.join(5)
            self.diagnostics.join(5)
            self.process.stdout.close()
            self.process.stderr.close()


@unittest.skipUnless(NPM and shutil.which("node") and importlib.util.find_spec("setuptools"),
                     "external tests require Node/npm and Python setuptools")
class ExternalBridgeClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="nosis-client-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.site = cls.root / "python-site"
        cls.external = cls.root / "external-app"
        cls.external.mkdir()
        source = cls.root / "source"
        source.mkdir()
        shutil.copyfile(ROOT / "pyproject.toml", source / "pyproject.toml")
        for directory in ("agent_core", "agent_runtime", "interfaces"):
            shutil.copytree(
                ROOT / directory, source / directory,
                ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".DS_Store"),
            )

        def run(args, cwd=source):
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                    encoding="utf-8", timeout=120, env={
                                        **os.environ,
                                        "PATH": str(ROOT / "node_modules/.bin") + os.pathsep + os.environ["PATH"],
                                    })
            if result.returncode:
                raise AssertionError(f"{args}:\n{result.stdout}\n{result.stderr}")
            return result

        run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
             "--wheel-dir", str(cls.root), str(source)])
        wheel = next(cls.root.glob("nosis_agent-*.whl"))
        run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-compile",
             "--target", str(cls.site), str(wheel)])
        cls.command = next(cls.site.rglob("nosis-bridge.exe" if os.name == "nt" else "nosis-bridge"))
        # Pack from private sources: prepack must not race frontend builds in
        # the checkout. Only the client's type dependencies are needed here.
        for package in ("@types/node", "undici-types"):
            shutil.copytree(ROOT / "node_modules" / package, source / "node_modules" / package)
        for package in ("protocol", "client"):
            package_source = source / "interfaces" / package
            run([NPM, "pack", "--pack-destination", str(cls.root)], cwd=package_source)
            if package == "protocol":
                shutil.copytree(package_source, source / "node_modules/@nosis/protocol")
        (cls.external / "package.json").write_text('{"private":true,"type":"module"}', encoding="utf-8")
        run([NPM, "install", "--ignore-scripts", "--no-audit", "--no-fund", "--offline",
             *(str(path) for path in sorted(cls.root.glob("*.tgz")))], cwd=cls.external)
        shutil.copyfile(ROOT / "tests/bridge_client/integration.mjs", cls.external / "integration.mjs")

    def setUp(self):
        # A fixed, short home keeps journal paths clear of the Windows
        # 260-character limit; each test starts from an empty one.
        self.home = self.root / "home"
        if self.home.exists():
            shutil.rmtree(self.home)
        self.home.mkdir()
        self.env = {
            **os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
            "PYTHONPATH": str(self.site), "PYTHONUTF8": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "NOSIS_TEST_BRIDGE": str(self.command),
        }

    def peer(self):
        peer = Peer(self.command, self.external, self.env)
        self.addCleanup(peer.close)
        return peer

    def open(self, peer, session_id=None):
        peer.send({"type": "open_session", "workspace": str(self.external), "session_id": session_id})
        ready = peer.until("session_ready")
        self.assertEqual(ready["protocol_version"], 2)
        self.assertEqual(peer.until("runtime_state")["phase"], "inactive")
        return ready

    def test_installed_native_estimator_uses_packaged_resources_without_litellm(self):
        result = subprocess.run(
            [sys.executable, "-c", """
import json, socket, sys
from pathlib import Path
def reject_network(*args, **kwargs):
    raise AssertionError('estimator accessed the network')
socket.socket.connect = reject_network
from agent_core import LLMRequest
from agent_core.providers import OpenAIProvider
import agent_core.providers.openai_tokens as local_tokens
counts = [OpenAIProvider(model).count_input_tokens(LLMRequest('Hello 世界', ()))
          for model in ('gpt-5', 'gpt-6-astra')]
assert 'litellm' not in sys.modules
print(json.dumps({'file': str(Path(local_tokens.__file__).resolve()), 'counts': counts}))
"""],
            cwd=self.external, env=self.env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(Path(report["file"]).is_relative_to(self.site))
        self.assertTrue(all(value > 0 for value in report["counts"]))

    def test_installed_cli_first_use_rebind_resume_and_eof(self):
        # Verify imports resolve to the wheel, rather than an editable checkout.
        imported = subprocess.run(
            [sys.executable, "-c", "import interfaces; print(interfaces.__file__)"],
            cwd=self.external, env=self.env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertTrue(Path(imported.stdout.strip()).is_relative_to(self.site))
        peer = self.peer()
        ready = self.open(peer)
        self.assertFalse(ready["resumed"])
        self.assertEqual(ready["transcript"]["items"], [])
        config = self.home / ".nosis"
        for filename in ("agent_config.json", "provider_config.json", "tool_config.json", "prompts/Soul.md"):
            self.assertTrue((config / filename).is_file(), filename)
        preserved = (config / "provider_config.json").read_bytes()
        peer.send({"type": "permission_set", "preset": "workspace_access"})
        self.assertEqual(peer.until("permission_changed")["runtime"]["permission_preset"], "workspace_access")
        peer.send({"type": "list_sessions"})
        self.assertIsInstance(peer.until("sessions_listed")["sessions"], list)
        peer.send({"type": "load_session"})
        self.assertEqual(peer.until("session_items")["items"], [])
        rebound = self.open(peer, ready["session_id"])
        self.assertTrue(rebound["resumed"])
        self.assertEqual(rebound["permission_preset"], "workspace_access")
        self.assertEqual([m["event_sequence"] for m in peer.messages], list(range(1, len(peer.messages) + 1)))
        peer.close()
        self.assertEqual(peer.process.returncode, 0)
        next_peer = self.peer()
        resumed = self.open(next_peer, ready["session_id"])
        self.assertEqual(resumed["event_sequence"], 1)
        self.assertEqual((config / "provider_config.json").read_bytes(), preserved)
        next_peer.process.stdin.close()  # EOF has the same close semantics as shutdown.
        next_peer.process.wait(timeout=10)
        self.assertEqual(next_peer.process.returncode, 0)

    def test_installed_cli_protocol_errors(self):
        for line in ('[]\n', '{broken\n', '{"type":"unknown_command"}\n', '{"type":"load_session"}\n'):
            with self.subTest(line=line):
                result = subprocess.run(
                    [str(self.command)], input=line, cwd=self.external, env=self.env,
                    capture_output=True, text=True, encoding="utf-8", timeout=20,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                message = json.loads(result.stdout)
                self.assertEqual(message["type"], "fatal")
                self.assertEqual(message["error"]["type"], "ProtocolError")

    def test_packaged_sdk_turn_interactions_steer_cancel_and_failure(self):
        bootstrap = self.peer()
        self.open(bootstrap)
        bootstrap.close()
        outputs = Queue()
        requests = []

        class ProviderFixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                output = outputs.get(timeout=10)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = []
                if output[0]["type"] == "message":
                    events.append({"type": "response.output_text.delta", "delta": "完成🙂"})
                events.append({"type": "response.completed", "sequence_number": 1, "response": {
                    "id": "resp_fixture", "object": "response", "created_at": 1, "model": "gpt-4o",
                    "status": "completed", "output": output,
                    "usage": {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17,
                              "input_tokens_details": {"cached_tokens": 0}},
                }})
                self.wfile.write("".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n"
                                        for e in events).encode("utf-8"))
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderFixture)
        server.daemon_threads = True
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = self.home / ".nosis"
        provider_path = config / "provider_config.json"
        provider = json.loads(provider_path.read_text(encoding="utf-8"))
        provider["main_agent"] = {"provider": "fixture", "vision_provider": ""}
        provider["providers"] = {"fixture": {
            "model": "gpt-4o", "url": f"http://127.0.0.1:{server.server_port}/v1",
            "key": "fixture-key", "api": "responses", "max_context_tokens": 128000,
        }}
        provider_path.write_text(json.dumps(provider), encoding="utf-8")
        agent_path = config / "agent_config.json"
        agent = json.loads(agent_path.read_text(encoding="utf-8"))
        agent["memory"]["enabled"] = False
        agent["generate_session_title"] = False
        agent["mcp"]["enabled"] = False
        agent["provider"]["max_retries"] = 0
        agent["skills"] = {"global_paths": [], "workspace_paths": []}
        for tool in agent["main_agent"]["tools"]:
            agent["main_agent"]["tools"][tool] = tool == "shell"
        agent_path.write_text(json.dumps(agent), encoding="utf-8")

        def tool(name, arguments, call_id):
            return [{"id": "fc_" + call_id, "type": "function_call", "status": "completed",
                     "call_id": call_id, "name": name, "arguments": json.dumps(arguments)}]

        outputs.put(tool("shell", {"command": "echo denied", "scope": "host"}, "shell-1"))
        question = {"question": "Proceed?", "options": [{"id": "yes", "label": "Yes"}]}
        outputs.put(tool("ask_user", question, "question-1"))
        outputs.put([{"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                      "content": [{"type": "output_text", "text": "完成🙂", "annotations": []}]}])
        outputs.put(tool("ask_user", question, "question-2"))
        result = subprocess.run(
            [shutil.which("node"), "integration.mjs"], cwd=self.external, env=self.env,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreater(json.loads(result.stdout)["events"], 20)
        self.assertEqual(len(requests), 4)
        self.assertTrue(outputs.empty())


if __name__ == "__main__":
    unittest.main()

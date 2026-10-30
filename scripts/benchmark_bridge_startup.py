"""Compare per-session processes with the resident Bridge using real model calls.

Run with the project's Python: scripts/benchmark_bridge_startup.py --provider deepseek.
Credentials are read locally and copied only into temporary configuration files.
Each sample requests a tiny reply with tools, memory and title generation disabled.
"""

import argparse
import io
import json
import os
from pathlib import Path
from queue import Queue
import re
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import time
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def child(args):
    sys.path.insert(0, str(args.source))
    from interfaces.bridge.__main__ import _claim_stdout
    output = _claim_stdout()
    from agent_runtime import config
    config.default_config_directory = lambda: args.config
    config.initialize_config_directory(args.config)

    original = config.ModelConfig.provider_class.fget
    instrumented = False
    bridge = None

    def provider_class(model_config):
        nonlocal instrumented
        cls = original(model_config)
        if cls.__name__ == "OpenAIProvider" and not instrumented:
            import httpx
            import agent_core.providers.openai_provider as module
            native_client = module.OpenAI

            def request_started(request):
                marker = re.search(r"nosis-benchmark-[a-zA-Z0-9_-]+", request.content.decode())
                if marker:
                    bridge.emit(type="benchmark_request", marker=marker.group(), at=time.perf_counter())

            def make_client(**kwargs):
                if "http_client" in kwargs:
                    kwargs["http_client"].event_hooks["request"].append(request_started)
                    return native_client(**kwargs)
                return native_client(**kwargs, http_client=httpx.Client(event_hooks={"request": [request_started]}))

            module.OpenAI = make_client
            instrumented = True
        return cls

    config.ModelConfig.provider_class = property(provider_class)
    if args.multiplex:
        from interfaces.bridge.server import BridgeServer
        bridge = BridgeServer(sys.stdin, output, config_directory=args.config)
    else:
        from interfaces.bridge.bridge import Bridge
        bridge = Bridge(sys.stdin, output)
    try:
        bridge.serve()
    finally:
        bridge.close()


class Peer:
    def __init__(self, source, config, multiplex):
        command = [sys.executable, str(Path(__file__).resolve()), "--child", "--source", str(source), "--config", str(config)]
        if multiplex:
            command.append("--multiplex")
        self.multiplex = multiplex
        self.events = Queue()
        self.started = time.perf_counter()
        self.process = subprocess.Popen(
            command, cwd=source, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"},
        )
        self.stderr = []
        self.reader = Thread(target=self.read, daemon=True)
        self.diagnostics = Thread(target=lambda: self.stderr.extend(self.process.stderr), daemon=True)
        self.reader.start()
        self.diagnostics.start()

    def read(self):
        try:
            for line in self.process.stdout:
                packet = json.loads(line)
                event = packet["event"] if packet["type"] == "channel_event" else packet
                self.events.put((time.perf_counter(), event))
        finally:
            self.events.put((time.perf_counter(), None))

    def send(self, message, channel="sample"):
        if self.multiplex and message["type"] != "shutdown":
            message = {"type": "channel_command", "channel_id": channel, "command": message}
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def until(self, kind):
        collected = []
        while True:
            at, event = self.events.get(timeout=90)
            if event is None:
                raise RuntimeError("Benchmark process exited before " + kind)
            collected.append((at, event))
            if event["type"] in {"fatal", "turn_failed", "session_open_failed"}:
                # Do not print upstream error bodies: some services echo credentials.
                error = event.get("error", {})
                details = error.get("details", {})
                safe = {key: details[key] for key in ("phase", "reason", "status_code", "finish_reason") if key in details}
                raise RuntimeError("Benchmark failed: " + error.get("type", event["type"]) + " " + json.dumps(safe))
            if event["type"] == kind:
                return at, event, collected

    def close(self):
        if self.process.poll() is None:
            self.send({"type": "shutdown"})
        self.process.stdin.close()
        self.process.wait(timeout=20)
        self.reader.join(2)
        self.diagnostics.join(2)
        self.process.stdout.close()
        self.process.stderr.close()


def sample(peer, workspace, marker, assembly_only=False):
    opening = time.perf_counter()
    peer.send({"type": "open_session", "workspace": str(workspace), "session_id": marker}, marker)
    ready, _, _ = peer.until("session_ready")
    peer.until("runtime_state")
    turn = time.perf_counter()
    if assembly_only:
        peer.send({"type": "runtime_refresh"}, marker)
        finished, _, events = peer.until("runtime_refreshed")
        return {
            "open_ms": round((ready - opening) * 1000, 1),
            "assembly_ms": round((finished - turn) * 1000, 1),
            "mcp_ready": sum(event.get("status") == "ready" for _, event in events if event["type"] == "mcp_server_status"),
            "mcp_unavailable": sum(event.get("status") == "unavailable" for _, event in events if event["type"] == "mcp_server_status"),
        }
    peer.send({"type": "user_turn", "turn_id": marker, "text": f"{marker}\nReply exactly OK. Do not use tools."}, marker)
    finished, _, events = peer.until("turn_completed")
    request = next(event["at"] for _, event in events if event["type"] == "benchmark_request")
    first_text = next((at for at, event in events if event["type"] == "assistant_delta"), None)
    running = next((at for at, event in events if event["type"] == "runtime_state" and event["phase"] == "running"), None)
    ms = lambda seconds: round(seconds * 1000, 1)
    return {
        "open_ms": ms(ready - opening),
        "turn_to_running_ms": ms(running - turn) if running else None,
        "turn_to_request_ms": ms(request - turn),
        "request_to_first_text_ms": ms(first_text - request) if first_text else None,
        "turn_to_first_text_ms": ms(first_text - turn) if first_text else None,
        "turn_total_ms": ms(finished - turn),
    }


def benchmark(args):
    with tempfile.TemporaryDirectory(prefix="nosis-benchmark-") as directory:
        root = Path(directory)
        source = root / "baseline"
        source.mkdir()
        archive = subprocess.check_output(["git", "archive", "--format=zip", args.baseline], cwd=ROOT)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            zipped.extractall(source)
        original = json.loads((args.config / "provider_config.json").read_text(encoding="utf-8"))
        provider = args.provider or original["main_agent"]["provider"]
        chosen = original["providers"][provider]
        route = chosen["model"].partition("/")[0]
        if not chosen.get("api") and "/" in chosen["model"] and route not in {"openai", "deepseek"}:
            raise ValueError("This request-timing probe requires a native OpenAI-compatible provider")
        commit = subprocess.check_output(["git", "rev-parse", args.baseline], cwd=ROOT, text=True).strip()
        results = {"provider": provider, "model": chosen["model"], "baseline_commit": commit, "baseline": [], "resident": []}
        for mode, code in (("baseline", source), ("resident", ROOT)):
            config = root / mode / "config"
            workspace = root / mode / "workspace"
            workspace.mkdir(parents=True)
            config.mkdir(exist_ok=True)
            shutil.copyfile(ROOT / "agent_runtime/defaults/agent_config.json", config / "agent_config.json")
            if args.assembly_only:
                workspace = ROOT
                agent = json.loads((args.config / "agent_config.json").read_text(encoding="utf-8"))
                for name in ("prompts", "plugins", "skills"):
                    if (args.config / name).is_dir():
                        shutil.copytree(args.config / name, config / name, ignore=shutil.ignore_patterns("__pycache__", ".git"))
                for name in ("AGENTS.md", "tool_config.json"):
                    if (args.config / name).is_file():
                        shutil.copyfile(args.config / name, config / name)
                provider_document = original
            else:
                agent = json.loads((config / "agent_config.json").read_text(encoding="utf-8"))
                agent.update(generate_session_title=False, max_generation_tokens=256, subagent_roles={})
                agent["provider"] = {"request_timeout_seconds": 60, "max_retries": 0}
                agent["memory"]["enabled"] = False
                agent["mcp"]["enabled"] = False
                agent["skills"] = {"global_paths": [], "workspace_paths": []}
                agent["main_agent"]["tools"] = {name: False for name in agent["main_agent"]["tools"]}
                provider_document = {
                    "main_agent": {"provider": provider}, "fast_model": {"provider": provider},
                    "subagent": {"provider": provider}, "providers": {provider: chosen},
                }
            (config / "agent_config.json").write_text(json.dumps(agent), encoding="utf-8")
            (config / "provider_config.json").write_text(json.dumps(provider_document), encoding="utf-8")
            if (args.config / ".env").is_file():
                shutil.copyfile(args.config / ".env", config / ".env")
            peer = None
            try:
                for index in range(args.samples):
                    if mode == "baseline" or peer is None:
                        peer = Peer(code, config, mode == "resident")
                        if mode == "resident":
                            ready, _, _ = peer.until("bridge_ready")
                            results["resident_process_ready_ms"] = round((ready - peer.started) * 1000, 1)
                    record = sample(peer, workspace, f"nosis-benchmark-{mode}-{index}", args.assembly_only)
                    results[mode].append(record)
                    print(json.dumps({"mode": mode, "sample": index + 1, **record}), flush=True)
                    if mode == "baseline":
                        peer.close()
                        peer = None
            finally:
                if peer is not None:
                    peer.close()
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--baseline", default="HEAD")
    parser.add_argument("--assembly-only", action="store_true", help="Use configured tools/plugins/MCP without sending a model request")
    parser.add_argument("--config", type=Path, default=Path.home() / ".nosis")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--multiplex", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    child(args) if args.child else benchmark(args)

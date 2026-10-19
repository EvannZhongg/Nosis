"""Short-lived real Broker processes for MCP integration tests."""

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from agent_runtime.mcp_client import _existing_connection


@contextmanager
def running_broker(config_directory: Path):
    directory = config_directory / "mcp"
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "test-broker.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", (
                "import asyncio, sys; from pathlib import Path; "
                "from agent_runtime.mcp_broker import serve; "
                "asyncio.run(serve(Path(sys.argv[1]), idle_seconds=1))"
            ), str(directory)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    deadline = time.monotonic() + 15
    keeper = None
    try:
        while not (directory / "broker.json").is_file():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace"))
            time.sleep(0.02)
        keeper = _existing_connection(directory)
        yield process
        keeper[0].shutdown(2)
        keeper[1].close()
        keeper[0].close()
        keeper = None
        process.wait(timeout=15)
        if process.returncode != 0:
            raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace"))
    finally:
        if keeper is not None:
            keeper[0].shutdown(2)
            keeper[1].close()
            keeper[0].close()
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()

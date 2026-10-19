import os
import time
import asyncio
import json
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer


# Lets a test charge this server a startup delay, which is how the
# concurrent startup of several servers becomes observable.
_delay = float(os.environ.get("TEST_MCP_DELAY_SECONDS", "0"))
if _delay > 0:
    time.sleep(_delay)


_log = os.environ.get("TEST_MCP_LOG")


class TestServer(MCPServer):
    async def list_tools(self):
        if _log:
            with open(_log, "a", encoding="utf-8") as output:
                output.write(json.dumps({"event": "discover", "pid": os.getpid()}) + "\n")
        return await super().list_tools()


server = TestServer("nosis-test")


@server.tool(description="Return the physical server identity")
def identity() -> dict[str, str | int | None]:
    return {"pid": os.getpid(), "cwd": str(Path.cwd()), "token": os.environ.get("TEST_MCP_TOKEN")}


@server.tool(description="Wait until released by a test file")
async def wait(started: str, release: str) -> str:
    Path(started).touch()
    try:
        while not Path(release).exists():
            await asyncio.sleep(0.02)
        return "released"
    finally:
        Path(started + ".stopped").touch()


@server.tool(description="Close the transport unexpectedly")
def crash() -> str:
    os._exit(0)


@server.tool(description="Echo text from the fake MCP server")
def echo(text: str) -> dict[str, str]:
    return {"echo": text}


@server.tool(description="A tool excluded by the integration allowlist")
def hidden() -> str:
    return "hidden"


if __name__ == "__main__":
    if len(sys.argv) > 1:
        server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
    else:
        server.run("stdio")

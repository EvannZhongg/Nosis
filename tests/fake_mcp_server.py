import os
import time

from mcp.server.mcpserver import MCPServer


# Lets a test charge this server a startup delay, which is how the
# concurrent startup of several servers becomes observable.
_delay = float(os.environ.get("TEST_MCP_DELAY_SECONDS", "0"))
if _delay > 0:
    time.sleep(_delay)


server = MCPServer("nosis-test")


@server.tool(description="Echo text from the fake MCP server")
def echo(text: str) -> dict[str, str]:
    return {"echo": text}


@server.tool(description="A tool excluded by the integration allowlist")
def hidden() -> str:
    return "hidden"


if __name__ == "__main__":
    server.run("stdio")

"""Private local IPC endpoint for the MCP service shared by Bridge processes."""

import asyncio
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from agent_core.atomic import atomic_write_text
from .wire import MESSAGE_LIMIT, encode


SERVICE_IDLE_SECONDS = 300


class _Peer:
    def __init__(self, service, writer: asyncio.StreamWriter):
        self.service = service
        self.writer = writer
        self.leases = {}
        self.bindings = {}
        self.binding_lock = asyncio.Lock()
        self.requests: dict[str, asyncio.Task] = {}

    def send(self, message: dict) -> None:
        if not self.writer.is_closing():
            try:
                self.writer.write(encode(message))
            except OSError:
                self.writer.close()

    async def dispatch(self, message: dict) -> None:
        request_id = message["id"]
        try:
            if message["method"] == "acquire":
                result = await self.bind(message["servers"])
            elif message["method"] == "call":
                lease = self.leases[message["lease"]]
                result = await self.service.call_tool(
                    lease, message["tool"], message["arguments"], message["timeout"],
                )
            else:
                raise ValueError("unknown MCP broker request")
            self.send({"id": request_id, "result": result})
        except asyncio.CancelledError:
            self.send({"id": request_id, "error": {"type": "cancelled", "message": "MCP request cancelled"}})
        except Exception as error:
            self.send({"id": request_id, "error": {
                "type": "connection" if isinstance(error, ConnectionError) else
                        "timeout" if isinstance(error, TimeoutError) else "tool",
                "message": str(error) or type(error).__name__,
            }})
        finally:
            try:
                await self.writer.drain()
            except OSError:
                pass
            self.requests.pop(request_id, None)

    async def bind(self, servers: list[dict]) -> list[dict]:
        async with self.binding_lock:
            bindings = {}

            async def acquire(server):
                previous = self.bindings.get(server["server"])
                if (previous is not None and previous[0] == server["identity"]
                        and previous[2].resource.connection.failure is None):
                    binding = previous
                else:
                    try:
                        binding = await self.acquire(server)
                    except Exception as error:
                        return {"server": server["server"], "error": str(error) or type(error).__name__}
                bindings[server["server"]] = binding
                return {"server": server["server"], "lease": binding[1], "tools": binding[2].tools}

            tasks = [asyncio.create_task(acquire(server)) for server in servers]
            try:
                results = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for _, token, lease in bindings.values():
                    if token not in self.leases:
                        self.service.release(lease)
                raise
            retained = {binding[1]: binding[2] for binding in bindings.values()}
            for token, lease in self.leases.items():
                if token not in retained:
                    self.service.release(lease)
            self.bindings = bindings
            self.leases = retained
            return results

    async def acquire(self, server: dict):
        token = uuid4().hex
        lease = await self.service.acquire(
            server["identity"], server["startup_timeout"],
            lambda error: self.send({"event": "unavailable", "lease": token, "error": error}),
        )
        return server["identity"], token, lease

    async def close(self) -> None:
        requests = tuple(self.requests.values())
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        for lease in self.leases.values():
            self.service.release(lease)
        self.leases.clear()
        self.bindings.clear()
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass


async def serve(directory: Path, *, idle_seconds: float = SERVICE_IDLE_SECONDS) -> None:
    from .service import McpService

    service = McpService(idle_seconds)
    token = secrets.token_hex(32)
    peers: set[_Peer] = set()
    connections: set[asyncio.Task] = set()
    last_connection = time.monotonic()
    endpoint = directory / "broker.json"

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal last_connection
        task = asyncio.current_task()
        connections.add(task)
        peer = _Peer(service, writer)
        peers.add(peer)
        try:
            async with asyncio.timeout(5):
                greeting = json.loads(await reader.readline())
                if not hmac.compare_digest(str(greeting.get("token", "")), token):
                    return
                peer.send({"authenticated": True})
                await writer.drain()
            while line := await reader.readline():
                message = json.loads(line)
                if message["method"] == "cancel":
                    pending = peer.requests.get(message["request"])
                    if pending is not None:
                        pending.cancel()
                else:
                    request_id = message["id"]
                    if request_id in peer.requests:
                        raise ValueError("duplicate MCP request id")
                    peer.requests[request_id] = asyncio.create_task(peer.dispatch(message))
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                await peer.close()
            finally:
                peers.discard(peer)
                connections.discard(task)
                last_connection = time.monotonic()

    server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=MESSAGE_LIMIT)
    port = server.sockets[0].getsockname()[1]
    atomic_write_text(endpoint, json.dumps({"port": port, "token": token}), mode=0o600)
    try:
        async with server:
            while True:
                await asyncio.sleep(min(1, idle_seconds))
                await service.reap()
                if not peers and not service.resources and time.monotonic() - last_connection >= idle_seconds:
                    break
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(connections):
            task.cancel()
        await asyncio.gather(*tuple(connections), return_exceptions=True)
        await service.close()
        if endpoint.is_file() and json.loads(endpoint.read_text(encoding="utf-8")).get("token") == token:
            endpoint.unlink()


def launch(directory: Path, idle_seconds: float) -> None:
    # The launcher exits before any Bridge owns a connection. In particular,
    # Windows taskkill /T on that Bridge must not include the shared service.
    with (directory / "broker.log").open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "agent_runtime.mcp.broker", str(directory), str(idle_seconds)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                           if os.name == "nt" else 0),
            start_new_session=os.name != "nt",
        )


if __name__ == "__main__":
    if sys.argv[1] == "--launch":
        launch(Path(sys.argv[2]), float(sys.argv[3]))
    else:
        asyncio.run(serve(Path(sys.argv[1]), idle_seconds=float(sys.argv[2])))

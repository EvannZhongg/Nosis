"""Reference ownership and discovery caching for physical MCP connections."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_core.mcp.connection import McpConnection, McpConnectionParameters


@dataclass(eq=False)
class _Resource:
    key: str
    connection: McpConnection
    task: asyncio.Task[None]
    references: int = 0
    idle_since: float = 0
    leases: set["McpLease"] = field(default_factory=set)


@dataclass(eq=False)
class McpLease:
    resource: _Resource
    tools: tuple[dict[str, Any], ...]
    on_failure: Callable[[str], None]
    active: bool = True


class McpService:
    def __init__(self, idle_seconds: float = 300) -> None:
        self.idle_seconds = idle_seconds
        self.resources: dict[str, _Resource] = {}

    async def acquire(
        self, identity: dict[str, Any], startup_timeout: float,
        on_failure: Callable[[str], None],
    ) -> McpLease:
        key = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        async with asyncio.timeout(startup_timeout):
            resource = self.resources.get(key)
            while resource is not None and resource.connection.stop.is_set():
                # A new owner must not race the previous subprocess's teardown.
                await asyncio.shield(asyncio.gather(resource.task, return_exceptions=True))
                if self.resources.get(key) is resource:
                    self.resources.pop(key)
                resource = self.resources.get(key)
            if resource is None or resource.connection.failure is not None:
                connection = McpConnection(McpConnectionParameters(**identity["connection"]))
                resource = _Resource(key, connection, asyncio.create_task(connection.run()))
                self.resources[key] = resource
                resource.task.add_done_callback(lambda task: self._finished(resource, task))
                # A client may detach before initialization reports its failure.
                connection.ready.add_done_callback(
                    lambda future: future.exception() if not future.cancelled() else None
                )
            resource.references += 1
            try:
                tools = await asyncio.shield(resource.connection.ready)
                if resource.connection.failure is not None:
                    raise ConnectionError("MCP connection is unavailable")
            except BaseException:
                self._release_reference(resource)
                raise
            lease = McpLease(resource, tools, on_failure)
            resource.leases.add(lease)
            return lease

    def release(self, lease: McpLease) -> None:
        if lease.active:
            lease.active = False
            lease.resource.leases.discard(lease)
            self._release_reference(lease.resource)

    def _release_reference(self, resource: _Resource) -> None:
        resource.references -= 1
        if resource.references == 0:
            resource.idle_since = time.monotonic()
            # Only waiting owners determine the initialization lifetime. A short
            # deadline must not cancel initialization awaited by another owner.
            if not resource.connection.ready.done():
                resource.connection.stop.set()
                resource.task.cancel()

    def _finished(self, resource: _Resource, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.result()
        if not resource.connection.ready.done():
            resource.connection.ready.set_exception(ConnectionError("MCP service closed during initialization"))
        if resource.connection.failure is not None:
            if self.resources.get(resource.key) is resource:
                self.resources.pop(resource.key)
            for lease in tuple(resource.leases):
                lease.on_failure("MCP connection closed")

    async def call_tool(
        self, lease: McpLease, tool: str, arguments: dict[str, Any], timeout: float,
    ) -> dict[str, Any]:
        if not lease.active:
            raise ConnectionError("MCP handle has been released")
        if tool not in {item["name"] for item in lease.tools}:
            raise ValueError(f"unknown MCP tool '{tool}'")
        lease.resource.references += 1
        try:
            async with asyncio.timeout(timeout):
                return await lease.resource.connection.call_tool(tool, arguments)
        finally:
            self._release_reference(lease.resource)

    async def reap(self) -> None:
        now = time.monotonic()
        expired = [
            resource for resource in self.resources.values()
            if resource.references == 0 and now - resource.idle_since >= self.idle_seconds
        ]
        for resource in expired:
            resource.connection.stop.set()
            if not resource.connection.ready.done():
                resource.task.cancel()
        if expired:
            await asyncio.gather(*(item.task for item in expired), return_exceptions=True)
            for resource in expired:
                if self.resources.get(resource.key) is resource:
                    self.resources.pop(resource.key)

    async def close(self) -> None:
        resources = tuple(self.resources.values())
        self.resources.clear()
        for resource in resources:
            resource.connection.stop.set()
            if not resource.connection.ready.done():
                resource.task.cancel()
        await asyncio.gather(*(item.task for item in resources), return_exceptions=True)

"""Keep each MCP transport's lifetime inside the task that opened it."""

import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .config import McpServerConfig


class McpConnection:
    def __init__(self, config: McpServerConfig, workspace: Path) -> None:
        self.ready: asyncio.Future[ClientSession] = (
            asyncio.get_running_loop().create_future()
        )
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(config, workspace))

    async def _run(self, config: McpServerConfig, workspace: Path) -> None:
        try:
            async with AsyncExitStack() as stack:
                if config.transport == "stdio":
                    cwd = Path(config.cwd) if config.cwd is not None else workspace
                    if not cwd.is_absolute():
                        cwd = workspace / cwd
                    parameters = StdioServerParameters(
                        command=config.command or "",
                        args=list(config.args),
                        cwd=str(cwd),
                        env={**os.environ, **config.env},
                    )
                    read, write = await stack.enter_async_context(stdio_client(parameters))
                else:
                    client = await stack.enter_async_context(
                        httpx2.AsyncClient(headers=config.headers)
                    )
                    read, write = await stack.enter_async_context(
                        streamable_http_client(config.url or "", http_client=client)
                    )
                session = await stack.enter_async_context(ClientSession(
                    read, write,
                    read_timeout_seconds=float(config.call_timeout_seconds),
                ))
                await session.initialize()
                self.ready.set_result(session)
                await self._stop.wait()
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(error)
            elif not self._stop.is_set():
                raise

    async def close(self) -> None:
        self._stop.set()
        if not self.ready.done() or self.ready.cancelled():
            self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

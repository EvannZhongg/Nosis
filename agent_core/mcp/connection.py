"""One physical MCP connection, owned and closed by one async task."""

import asyncio
import json
import ssl
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from urllib.request import proxy_bypass_environment

import httpx2
from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CONNECTION_CLOSED, PaginatedRequestParams


@dataclass(frozen=True)
class McpConnectionParameters:
    transport: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


async def discover_tools(session: ClientSession) -> tuple[dict[str, Any], ...]:
    tools: dict[str, dict[str, Any]] = {}
    cursor = None
    while True:
        result = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
        for remote in result.tools:
            if remote.name in tools:
                raise ValueError(f"duplicate MCP tool name '{remote.name}'")
            tools[remote.name] = {
                "name": remote.name,
                "description": remote.description,
                "input_schema": remote.input_schema,
            }
        cursor = result.next_cursor
        if not cursor:
            return tuple(tools[name] for name in sorted(tools))


class McpConnection:
    def __init__(self, parameters: McpConnectionParameters):
        self.parameters = parameters
        self.ready: asyncio.Future[tuple[dict[str, Any], ...]] = (
            asyncio.get_running_loop().create_future()
        )
        self.stop = asyncio.Event()
        self.failure: BaseException | None = None
        self.session: ClientSession | None = None

    async def run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                parameters = self.parameters
                if parameters.transport == "stdio":
                    read, write = await stack.enter_async_context(stdio_client(
                        StdioServerParameters(
                            command=parameters.command,
                            args=parameters.args,
                            cwd=parameters.cwd,
                            env=parameters.env,
                        )
                    ))
                else:
                    # The Broker's environment belongs to its first launcher.
                    # Resolve proxy and CA settings from this client's snapshot.
                    env = {key.lower(): value for key, value in parameters.env.items()}
                    proxies = {scheme: env[f"{scheme}_proxy"] for scheme in
                               ("http", "https", "all", "no") if env.get(f"{scheme}_proxy")}
                    url = urlsplit(parameters.url)
                    proxy = None if proxy_bypass_environment(url.netloc, proxies) else (
                        proxies.get(url.scheme) or proxies.get("all")
                    )
                    cafile = parameters.env.get("SSL_CERT_FILE")
                    capath = parameters.env.get("SSL_CERT_DIR")
                    verify = (
                        ssl.create_default_context(cafile=cafile, capath=capath)
                        if cafile or capath else httpx2.create_ssl_context(trust_env=False)
                    )
                    client = await stack.enter_async_context(httpx2.AsyncClient(
                        headers=parameters.headers, trust_env=False, proxy=proxy, verify=verify,
                    ))
                    read, write = await stack.enter_async_context(streamable_http_client(
                        parameters.url, http_client=client,
                    ))
                self.session = await stack.enter_async_context(ClientSession(read, write))
                await self.session.initialize()
                tools = await discover_tools(self.session)
                self.ready.set_result(tools)
                await self.stop.wait()
        except BaseException as error:
            while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
                error = error.exceptions[0]
            if not self.stop.is_set():
                self.failure = error
            if not self.ready.done():
                self.ready.set_exception(error)
        finally:
            self.session = None

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.failure is not None or self.session is None:
            raise ConnectionError("MCP connection is unavailable")
        try:
            result = await self.session.call_tool(tool, arguments)
        except MCPError as error:
            if error.code == CONNECTION_CLOSED:
                self.failure = ConnectionError(str(error))
                self.stop.set()
                raise self.failure from error
            raise
        except (ConnectionError, OSError) as error:
            self.failure = error
            self.stop.set()
            raise
        data = result.model_dump(mode="json", exclude_none=True)
        if result.is_error:
            texts = [
                item["text"] for item in data.get("content", [])
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            raise RuntimeError("\n".join(texts) or json.dumps(data, ensure_ascii=False))
        return data

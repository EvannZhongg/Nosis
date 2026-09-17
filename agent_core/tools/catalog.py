"""Shared Tool Catalog and the per-Agent ToolSet selected from it."""
from typing import Iterable

from .base import (
    Tool,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutput,
    ToolPolicy,
    ToolResult,
)
from .context import ToolExecutionContext


class ToolCatalog:
    """The Runtime's shared, immutable set of Tool instances.

    The catalog holds only stateless Tools and no runtime dependencies, so
    every Agent of a Runtime — and every parallel invocation — draws from
    the same instances. Roles differ by which names they select, not by
    owning their own Tool objects.
    """

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"tool '{tool.name}' is already registered")
            self._tools[tool.name] = tool

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def extend(self, tools: Iterable[Tool]) -> "ToolCatalog":
        """Return a catalog holding this one's tools plus ``tools``."""
        return ToolCatalog((*self._tools.values(), *tools))

    def select(
        self,
        names: Iterable[str],
        context: ToolExecutionContext,
        *,
        policy: ToolPolicy | None = None,
    ) -> "ToolSet":
        """Return the ToolSet a role sees in this Runtime.

        Names the catalog does not know, and tools whose Runtime
        dependencies are absent, are left out: a role asks for a
        capability, and the Runtime decides whether it can offer it.
        """
        selected = []
        for name in names:
            tool = self._tools.get(name)
            if tool is not None and tool.available(context):
                selected.append(tool)
        return ToolSet(selected, context, policy=policy)


class ToolSet:
    """One Agent's view of the catalog, bound to its context and policy.

    A ToolSet is the only object that executes a call. It owns no Tool
    instances and no invocation state, so an Agent may hold one for its
    whole lifetime while the Tools behind it stay shared.
    """

    def __init__(
        self,
        tools: Iterable[Tool],
        context: ToolExecutionContext,
        *,
        policy: ToolPolicy | None = None,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._context = context
        self._policy = policy

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(
            tool.definition(self._context) for tool in self._tools.values()
        )

    def is_concurrent(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool is not None and tool.concurrent

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                error=ToolError(
                    type="tool_not_found",
                    message=f"tool '{call.name}' is not registered",
                ),
            )

        try:
            if self._policy is not None:
                self._policy.authorize(call, self._context)
            output = tool.execute(call.arguments, self._context)
        except Exception as error:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                error=ToolError(
                    type=type(error).__name__,
                    message=str(error),
                ),
            )

        if isinstance(output, ToolOutput):
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                output=output.output,
                attachments=output.attachments,
                artifact_writer=output.artifact_writer,
                artifact_cleanup=output.artifact_cleanup,
            )
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            output=output,
        )

"""Shared Tool Catalog and the per-Agent ToolSet selected from it."""
from dataclasses import replace
from typing import TYPE_CHECKING, Iterable

from ..turn_control import AgentCancelled, TurnControl

from .base import (
    Tool,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolExecutionError,
    ToolOutput,
    ToolPolicy,
    ToolResult,
)
from .context import ToolExecutionContext

if TYPE_CHECKING:
    from ..hooks import HookEvent, HookResult


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

    def run_hooks(
        self, event: "HookEvent", payload: dict[str, object],
        turn_control: TurnControl | None = None,
    ) -> "HookResult":
        from ..hooks import HookResult

        context = self._context
        if not context.hooks:
            return HookResult()
        if turn_control is not None:
            context = replace(context, cancellation=turn_control)
        return context.hooks.run(event, payload, context, self._policy)

    def execute(
        self, call: ToolCall, *, turn_control: TurnControl | None = None,
    ) -> ToolResult:
        cancellation = turn_control or self._context.cancellation
        if cancellation is not None and cancellation.cancelled:
            raise AgentCancelled
        if call.argument_error is not None:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                error=call.argument_error,
            )
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

        from ..hooks import HookExecutionError

        hook = self.run_hooks("PreToolUse", {"tool_call": call.to_dict()}, turn_control)
        if hook.decision == "deny":
            return ToolResult(call.id, call.name,
                              error=ToolError("HookDenied", hook.reason),
                              hook_feedback=hook.feedback)
        executed = False
        try:
            context = self._context
            if turn_control is not None:
                context = replace(context, cancellation=turn_control)
            if context.execution_router is not None:
                context = replace(
                    context,
                    execution=context.execution_router.resolve(call),
                )
            if self._policy is not None:
                self._policy.authorize(call, context)
            if cancellation is not None and cancellation.cancelled:
                raise AgentCancelled
            executed = True
            output = tool.execute(call.arguments, context)
        except HookExecutionError:
            raise
        except Exception as error:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                error=(
                    error.error if isinstance(error, ToolExecutionError)
                    else ToolError(type=type(error).__name__, message=str(error))
                ),
                executed=executed,
                hook_feedback=hook.feedback,
            )

        if isinstance(output, ToolOutput):
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                output=output.output,
                attachments=output.attachments,
                artifact_writer=output.artifact_writer,
                artifact_cleanup=output.artifact_cleanup,
                artifact_preview=output.artifact_preview,
                executed=True,
                hook_feedback=hook.feedback,
            )
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            output=output,
            executed=True,
            hook_feedback=hook.feedback,
        )

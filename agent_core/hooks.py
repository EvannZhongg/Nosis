"""Explicit execution checkpoints and command hooks, independent of transports."""

import json
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from .execution.authority import ExecutionScope
from .tools.base import JSONValue, Tool, ToolCall, ToolDefinition, ToolOutput, ToolPolicy
from .tools.context import ToolExecutionContext
from .turn_control import AgentCancelled

if TYPE_CHECKING:
    from .session import Session


HookEvent = Literal["InputSubmitted", "PreToolUse", "PostToolUse", "Stop"]
HOOK_EVENTS = ("InputSubmitted", "PreToolUse", "PostToolUse", "Stop")
MAX_HOOK_OUTPUT_CHARS = 16_000


class HookExecutionError(RuntimeError):
    """A hook could not produce a valid decision."""


class HookRejectedError(RuntimeError):
    """An input or completion was rejected by a hook."""


@dataclass(frozen=True)
class HookDefinition:
    name: str
    event: HookEvent
    command: str
    args: tuple[str, ...]
    root: Path
    source: Path
    tools: tuple[str, ...] | None = None
    scope: ExecutionScope = ExecutionScope.WORKSPACE
    timeout_seconds: int = 10


@dataclass(frozen=True)
class HookInvocation:
    definition: HookDefinition
    payload: dict[str, object]


@dataclass(frozen=True)
class HookResult:
    decision: str = "pass"
    reason: str = ""
    feedback: tuple[str, ...] = ()


@dataclass(frozen=True)
class HookCompletedEvent:
    session_id: str
    invocation_id: str
    hook_id: str
    event: HookEvent
    status: str
    decision: str | None
    reason: str | None
    duration_ms: int
    tool_call_id: str | None


class HookCommandTool(Tool):
    """Internal capability, selected only by HookRunner and never by a model."""

    name = "_hook_command"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(self.name, "Execute a declared command hook", {"type": "object"})

    def execute(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext,
    ) -> JSONValue | ToolOutput:
        from .tools.builtin.shell import _command_output

        invocation = context.hook_invocation
        route = context.execution
        if invocation is None or route is None or route.executor is None:
            raise RuntimeError("hook command execution is not bound")
        definition = invocation.definition
        command = (arguments["command"], *arguments["args"])
        return _command_output(route.executor.execute(
            command,
            definition.timeout_seconds,
            cancellation=context.cancellation,
            stdin=json.dumps(invocation.payload, ensure_ascii=False).encode("utf-8"),
            read_only_paths=(definition.root,),
        ))


class HookRunner:
    """An immutable assembly of hooks; invocation state belongs to the caller."""

    def __init__(self, definitions: tuple[HookDefinition, ...] = ()) -> None:
        from .tools.catalog import ToolCatalog

        self.definitions = definitions
        self._catalog = ToolCatalog((HookCommandTool(),))

    def __bool__(self) -> bool:
        return bool(self.definitions)

    def run(
        self,
        event: HookEvent,
        payload: dict[str, object],
        context: ToolExecutionContext,
        policy: ToolPolicy | None,
    ) -> HookResult:
        feedback: list[str] = []
        decision = "pass"
        reasons: list[str] = []
        call = payload.get("tool_call")
        for definition in self.definitions:
            if definition.event != event:
                continue
            if definition.tools is not None and (
                not isinstance(call, dict) or call.get("name") not in definition.tools
            ):
                continue
            result = self._invoke(definition, payload, context, policy)
            feedback.extend(result.feedback)
            if result.decision != "pass":
                decision = result.decision
                if result.reason:
                    reasons.append(f"[{definition.name}] {result.reason}")
        return HookResult(decision, "\n".join(reasons), tuple(feedback))

    def _invoke(
        self, definition: HookDefinition, payload: dict[str, object],
        context: ToolExecutionContext, policy: ToolPolicy | None,
    ) -> HookResult:
        from .tool_result import ToolResultNormalizer

        if context.cancellation is not None and context.cancellation.cancelled:
            raise AgentCancelled
        invocation_id = str(uuid4())
        tool_call = payload.get("tool_call")
        tool_call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
        common = {
            "invocation_id": invocation_id,
            "hook_id": definition.name,
            "event": definition.event,
            "source": str(definition.source),
            **({"input": payload["input"]} if "input" in payload else {}),
        }
        context.session.record_hook("started", common, tool_call_id)
        started = monotonic()
        decision = None
        reason = None
        status = "failed"
        diagnostics = None
        feedback: tuple[str, ...] = ()
        try:
            invocation = HookInvocation(definition, {
                "schema_version": 1,
                "event": definition.event,
                "hook_id": definition.name,
                "session_id": context.session.session_id,
                "turn_id": context.session.current_turn_id,
                "workspace": str(context.workspace.path),
                "agent": {"role": context.agent_role, "parent_session_id": context.parent_session_id},
                **payload,
            })
            hook_context = replace(context, hooks=None, hook_invocation=invocation)
            tools = self._catalog.select((HookCommandTool.name,), hook_context, policy=policy)
            expand = lambda value: value.replace("${PLUGIN_ROOT}", str(definition.root))
            result = tools.execute(ToolCall(invocation_id, HookCommandTool.name, {
                "command": expand(definition.command),
                "args": [expand(arg) for arg in definition.args],
                "scope": definition.scope.value,
            }))
            diagnostics = ToolResultNormalizer(
                context.workspace, context.session.session_id,
                sessions_directory=context.sessions_directory,
                artifact_directory=context.artifact_directory,
            ).normalize(result)
            if result.error is not None:
                raise HookExecutionError(result.error.message)
            output = result.output
            if output["timed_out"]:
                raise HookExecutionError("hook command timed out")
            if output["exit_code"] != 0:
                raise HookExecutionError(f"hook command exited with {output['exit_code']}: {output['stderr'][:1000]}")
            if len(output["stdout"]) > MAX_HOOK_OUTPUT_CHARS:
                raise HookExecutionError("hook response exceeds the output budget")
            response = json.loads(output["stdout"])
            parsed = _parse_result(response, definition.event)
            decision, reason, feedback = parsed.decision, parsed.reason, parsed.feedback
            status = "completed"
            return parsed
        except (AgentCancelled, KeyboardInterrupt):
            status = "cancelled"
            raise
        except Exception as error:
            reason = str(error)
            raise HookExecutionError(f"Hook '{definition.name}' ({definition.source}): {error}") from error
        finally:
            duration_ms = int((monotonic() - started) * 1000)
            context.session.record_hook("finished", {
                **common, "status": status, "decision": decision, "reason": reason,
                "feedback": list(feedback), "duration_ms": duration_ms,
                "diagnostics": diagnostics,
            }, tool_call_id)
            if context.on_hook_event is not None:
                context.on_hook_event(HookCompletedEvent(
                    context.session.session_id, invocation_id, definition.name,
                    definition.event, status, decision, reason, duration_ms, tool_call_id,
                ))


def _parse_result(data: object, event: HookEvent) -> HookResult:
    if not isinstance(data, dict) or set(data) - {"decision", "reason", "feedback"}:
        raise ValueError("hook response must be an object with decision, reason and feedback fields")
    decision = data.get("decision", "pass")
    allowed = {"pass", "continue"} if event == "Stop" else {"pass", "deny"}
    if event == "PostToolUse":
        allowed = {"pass"}
    if not isinstance(decision, str) or decision not in allowed:
        raise ValueError(f"invalid decision for {event}: {decision!r}")
    for field in ("reason", "feedback"):
        if field in data and (not isinstance(data[field], str) or not data[field].strip()):
            raise ValueError(f"hook response '{field}' must be a non-empty string")
    reason = data.get("reason", "")
    feedback = data.get("feedback", "")
    if decision == "deny" and not reason:
        raise ValueError("deny requires a reason")
    if decision == "continue" and not feedback:
        raise ValueError("continue requires feedback")
    return HookResult(decision, reason, (feedback,) if feedback else ())


def append_hook_feedback(session: "Session", feedback: tuple[str, ...]) -> None:
    if feedback:
        session.add_item("user", "[Hook feedback]\n" + "\n\n".join(feedback), origin="hook_feedback")


def turn_execution_summary(session: "Session") -> dict[str, object]:
    """Current-turn facts, without treating transcript mentions as executions."""
    results = {item.tool_call_id: item.content for item in session.items if item.role == "tool"}
    return {
        "tools": [
            {"id": execution.tool_call_id, "name": execution.name,
             "arguments": execution.arguments, "status": execution.status,
             "result": results.get(execution.tool_call_id)}
            for execution in session.tool_executions.values()
            if execution.turn_id == session.current_turn_id
        ],
        "jobs": [
            {"id": job.job_id, "kind": job.kind, "status": job.status,
             "details": job.details, "result_summary": job.result_summary}
            for job in session.jobs.values() if job.turn_id == session.current_turn_id
        ],
    }

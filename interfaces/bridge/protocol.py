"""Translation between agent runtime events and protocol messages.

The bridge speaks newline-delimited JSON over stdio. Keeping the
translation pure makes it testable without a running agent.
"""

import json
from datetime import datetime, timezone

from agent_core import (
    AgentEvent,
    AssistantMessageDeltaEvent,
    ReasoningDeltaEvent,
    AssistantMessageEvent,
    ContextWindow,
    ContextWindowEvent,
    ToolBatchStartedEvent,
    ToolCall,
    ToolCallEvent,
    ToolMediaEvent,
    ToolResultEvent,
    UserSteerAppliedEvent,
    JobStatusEvent,
    PlanSnapshot,
    plan_snapshot_to_dict,
)
from agent_core.llm import TokenUsage


def encode(message: dict[str, object]) -> str:
    return json.dumps(message, ensure_ascii=False)


def decode(line: str) -> dict[str, object]:
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("protocol message must be a JSON object")
    if not isinstance(message.get("type"), str):
        raise ValueError("protocol message must have a string 'type'")
    return message


def format_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def tool_call_to_dict(tool_call: ToolCall) -> dict[str, object]:
    return {
        "id": tool_call.id,
        "name": tool_call.name,
        "arguments": tool_call.arguments,
    }


def usage_to_dict(usage: TokenUsage | None) -> dict[str, object] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


def context_window_to_dict(window: ContextWindow) -> dict[str, int]:
    return {
        "input_tokens": window.input_tokens,
        "max_input_tokens": window.max_input_tokens,
        "max_context_tokens": window.max_context_tokens,
        "output_reserve_tokens": window.output_reserve_tokens,
        "compression_threshold": window.compression_threshold,
        "compression_count": window.compression_count,
    }


def _plan_payload(
    plan: PlanSnapshot | dict[str, object] | None,
) -> dict[str, object] | None:
    if plan is None:
        return None
    if isinstance(plan, PlanSnapshot):
        return plan_snapshot_to_dict(plan)
    return plan


def session_ready_message(
    *,
    session_id: str,
    workspace: str,
    provider: str,
    model: str,
    resumed: bool,
    message_count: int,
    permission_preset: str,
) -> dict[str, object]:
    return {
        "type": "session_ready",
        "session_id": session_id,
        "workspace": workspace,
        "provider": provider,
        "model": model,
        "resumed": resumed,
        "message_count": message_count,
        "permission_preset": permission_preset,
    }


def runtime_state_message(
    *,
    phase: str,
    turn_id: str | None = None,
    approval: dict[str, object] | None,
    question: dict[str, object] | None,
    provider: str | None,
    permission_preset: str,
    context_window: dict[str, int] | None,
    jobs: list[dict[str, object]],
    event_sequence: int | None = None,
    runtime_warnings: tuple[str, ...] = (),
    plan: PlanSnapshot | dict[str, object] | None = None,
) -> dict[str, object]:
    """Describe the execution plane independently from Session readiness.

    The first runtime_state after opening is also the frontend synchronization
    barrier: commands must not be dispatched until that snapshot is applied.
    """
    message: dict[str, object] = {
        "type": "runtime_state",
        "phase": phase,
        "turn_id": turn_id,
        "approval": approval,
        "question": question,
        "provider": provider,
        "permission_preset": permission_preset,
        "context_window": context_window,
        "jobs": jobs,
        "runtime_warnings": list(runtime_warnings),
        "plan": _plan_payload(plan),
    }
    if event_sequence is not None:
        message["event_sequence"] = event_sequence
    return message


def plan_updated_message(plan: PlanSnapshot) -> dict[str, object]:
    return {
        "type": "plan_updated",
        "plan": plan_snapshot_to_dict(plan),
    }


def attachment_replaced_message(*, phase: str) -> dict[str, object]:
    """Tell a GUI page that another attachment owns the session."""
    return {
        "type": "attachment_replaced",
        "phase": phase,
    }


def sessions_listed_message(
    sessions: list[dict[str, object]],
) -> dict[str, object]:
    """Answer ``list_sessions`` with the Workspace's stored Sessions."""
    return {
        "type": "sessions_listed",
        "sessions": sessions,
    }


def session_items_message(
    items: list[dict[str, object]],
) -> dict[str, object]:
    """Answer ``load_session`` with the stored conversation to render."""
    return {
        "type": "session_items",
        "items": items,
    }


def event_to_message(
    event: AgentEvent,
    turn_id: str,
) -> dict[str, object]:
    if isinstance(event, AssistantMessageDeltaEvent):
        return {
            "type": "assistant_delta",
            "turn_id": turn_id,
            "text": event.text,
            "model_call_index": event.model_call_index,
        }
    if isinstance(event, ReasoningDeltaEvent):
        return {"type": "reasoning_delta", "turn_id": turn_id, "text": event.text, "model_call_index": event.model_call_index}

    if isinstance(event, ContextWindowEvent):
        return {
            "type": "context_window",
            "turn_id": turn_id,
            **context_window_to_dict(event.window),
        }

    if isinstance(event, AssistantMessageEvent):
        return {
            "type": "assistant_message",
            "turn_id": turn_id,
            "content": event.content,
            "timestamp_utc": format_timestamp(event.timestamp_utc),
            "model_call_index": event.model_call_index,
        }

    if isinstance(event, ToolBatchStartedEvent):
        return {
            "type": "tool_batch_started",
            "turn_id": turn_id,
            "model_call_index": event.model_call_index,
            "tool_calls": [
                tool_call_to_dict(tool_call)
                for tool_call in event.tool_calls
            ],
        }

    if isinstance(event, ToolCallEvent):
        return {
            "type": "tool_call",
            "turn_id": turn_id,
            "tool_call": tool_call_to_dict(event.tool_call),
            "tool_index": event.tool_index,
            "tool_count": event.tool_count,
        }

    if isinstance(event, ToolResultEvent):
        result = event.tool_result
        # Tool output is deliberately omitted: results can be large and
        # are already offloaded to session artifacts by the normalizer.
        return {
            "type": "tool_result",
            "turn_id": turn_id,
            "tool_call_id": result.tool_call_id,
            "name": result.name,
            "ok": result.error is None,
            "error": None
            if result.error is None
            else {
                "type": result.error.type,
                "message": result.error.message,
            },
            "tool_index": event.tool_index,
            "tool_count": event.tool_count,
        }

    if isinstance(event, ToolMediaEvent):
        return {
            "type": "tool_media",
            "turn_id": turn_id,
            "attachments": [
                {
                    "type": "image",
                    "path": part.path,
                    "mime_type": part.mime_type,
                }
                for part in event.attachments
            ],
        }

    if isinstance(event, UserSteerAppliedEvent):
        return {
            "type": "user_steer_applied",
            "turn_id": turn_id,
            "steer_id": event.steer_id,
            "text": event.text,
            "timestamp_utc": format_timestamp(event.timestamp_utc),
        }

    if isinstance(event, JobStatusEvent):
        return {
            "type": "job_status",
            "turn_id": turn_id,
            "job_id": event.job_id,
            "kind": event.kind,
            "status": event.status,
        }

    raise TypeError(f"unsupported agent event: {type(event).__name__}")

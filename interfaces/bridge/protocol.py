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
    ContextArchivedEvent,
    ToolBatchStartedEvent,
    ToolCall,
    ToolCallEvent,
    ToolMediaEvent,
    ToolResultEvent,
    UserSteerAppliedEvent,
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


def ready_message(
    *,
    session_id: str,
    workspace: str,
    model: str,
    resumed: bool,
    message_count: int,
    permission_preset: str,
    skill_warnings: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "type": "ready",
        "session_id": session_id,
        "workspace": workspace,
        "model": model,
        "resumed": resumed,
        "message_count": message_count,
        "permission_preset": permission_preset,
        "skill_warnings": list(skill_warnings),
    }


def runtime_state_message(
    *,
    running: bool,
    turn_id: str | None = None,
    approval: dict[str, object] | None,
    question: dict[str, object] | None,
    provider: str | None,
    permission_preset: str,
    event_sequence: int,
) -> dict[str, object]:
    """Describe a GUI server-owned runtime when a WebSocket attaches."""
    return {
        "type": "runtime_state",
        "running": running,
        "turn_id": turn_id,
        "approval": approval,
        "question": question,
        "provider": provider,
        "permission_preset": permission_preset,
        "event_sequence": event_sequence,
    }


def attachment_replaced_message(*, running: bool) -> dict[str, object]:
    """Tell a GUI page that another attachment owns the session."""
    return {
        "type": "attachment_replaced",
        "running": running,
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

    if isinstance(event, ContextArchivedEvent):
        return {
            "type": "context_archived",
            "turn_id": turn_id,
            "checkpoint_number": event.checkpoint_number,
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

    raise TypeError(f"unsupported agent event: {type(event).__name__}")

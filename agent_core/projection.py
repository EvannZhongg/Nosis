"""Derived views over immutable runtime execution facts."""
from dataclasses import dataclass
from typing import Iterable

from .session import Message


@dataclass(frozen=True)
class ContextUnit:
    """An indivisible provider-context span and its raw Journal bounds."""

    start: int
    end: int
    messages: tuple[Message, ...]


def project_context_units(
    items: Iterable[Message],
    *,
    start_index: int = 0,
) -> tuple[ContextUnit, ...]:
    """Group raw items into units that must be retained or archived whole.

    A tool-calling assistant message, every result in that batch, and its
    trailing media message form one unit, with the results ordered by the
    model's original call order rather than completion order.  Incomplete
    batches remain part of the raw span but expose no provider messages, and
    are therefore absent from the next provider request.
    """
    history = tuple(items)
    result: list[ContextUnit] = []
    index = 0
    while index < len(history):
        item = history[index]
        if item.role == "assistant" and item.tool_calls:
            calls = {call.id for call in item.tool_calls}
            found: dict[str, Message] = {}
            media: list[Message] = []
            end = index + 1
            while end < len(history) and (
                history[end].role == "tool" or history[end].is_tool_media
            ):
                following = history[end]
                if following.tool_call_id in calls:
                    found[following.tool_call_id] = following
                elif following.is_tool_media:
                    media.append(following)
                end += 1
            messages: tuple[Message, ...] = ()
            if calls.issubset(found):
                messages = (
                    item,
                    *(found[call.id] for call in item.tool_calls),
                    *media,
                )
            result.append(
                ContextUnit(
                    start=start_index + index,
                    end=start_index + end,
                    messages=messages,
                )
            )
            index = end
            continue
        messages = () if item.role == "tool" else (item,)
        result.append(
            ContextUnit(
                start=start_index + index,
                end=start_index + index + 1,
                messages=messages,
            )
        )
        index += 1
    return tuple(result)

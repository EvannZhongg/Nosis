"""Derived views over immutable runtime execution facts."""
from typing import Iterable

from .session import Message


def project_provider_messages(items: Iterable[Message]) -> tuple[Message, ...]:
    """Return provider-valid messages without changing execution history.

    An incomplete tool batch remains visible to recovery and UI projections,
    but it is absent from the next provider request.  Completed results are
    ordered by the model's original call order rather than completion order.
    """
    history = tuple(items)
    result: list[Message] = []
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
            if calls.issubset(found):
                result.append(item)
                result.extend(found[call.id] for call in item.tool_calls)
                result.extend(media)
            index = end
            continue
        if item.role != "tool":
            result.append(item)
        index += 1
    return tuple(result)

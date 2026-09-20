"""Strict assembly of streamed Tool Calls from provider deltas."""

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Iterable

from agent_core.errors import ProviderProtocolError
from agent_core.tools import ToolCall


@dataclass
class _ToolCallState:
    order: int
    index: int | None = None
    id: str | None = None
    name: str | None = None
    argument_fragments: list[str] = field(default_factory=list)


class ToolCallStreamAssembler:
    """Assemble Tool Calls while rejecting ambiguous provider streams."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._states: list[_ToolCallState] = []
        self._by_index: dict[int, _ToolCallState] = {}
        self._by_id: dict[str, _ToolCallState] = {}

    def add_batch(self, tool_calls: Iterable[object]) -> None:
        calls = tuple(
            (call, self._tool_call_index(call), self._tool_call_id(call))
            for call in tool_calls
        )
        if len(calls) > 1 and any(
            index is None and tool_call_id is None
            for _, index, tool_call_id in calls
        ):
            self._raise(
                "a tool call in a multi-call chunk has no index or id",
                reason="ambiguous_identity",
            )
        for call, index, tool_call_id in calls:
            self._add(call, index, tool_call_id)

    def finish(self) -> tuple[ToolCall, ...]:
        indexed = [state.index is not None for state in self._states]
        if any(indexed) and not all(indexed):
            self._raise(
                "tool calls mix indexed and unindexed identities",
                reason="ambiguous_order",
            )
        states = (
            sorted(self._states, key=lambda state: state.index)
            if all(indexed)
            else self._states
        )
        result = []
        for state in states:
            if state.id is None:
                self._raise_for_state(
                    state,
                    "tool call id is missing",
                    reason="missing_id",
                )
            if state.name is None:
                self._raise_for_state(
                    state,
                    "tool call name is missing",
                    reason="missing_name",
                )
            result.append(
                ToolCall(
                    id=state.id,
                    name=state.name,
                    arguments=_decode_arguments(state, self._model),
                )
            )
        return tuple(result)

    def _add(
        self,
        call: object,
        index: int | None,
        tool_call_id: str | None,
    ) -> None:
        state = self._resolve(index, tool_call_id)
        self._bind_identity(state, index, tool_call_id)

        function = _get_field(call, "function")
        if function is None:
            return
        name = _non_empty_string(_get_field(function, "name"))
        if name is not None:
            if state.name is not None and state.name != name:
                self._raise_for_state(
                    state,
                    "tool call name changed during the stream",
                    reason="conflicting_name",
                    incoming_name=name,
                )
            state.name = name
        arguments = _get_field(function, "arguments")
        if isinstance(arguments, str):
            if arguments:
                state.argument_fragments.append(arguments)
        elif arguments is not None:
            self._raise_for_state(
                state,
                "tool call arguments must be streamed as text",
                reason="invalid_arguments_fragment",
            )

    def _resolve(
        self,
        index: int | None,
        tool_call_id: str | None,
    ) -> _ToolCallState:
        by_index = self._by_index.get(index) if index is not None else None
        by_id = self._by_id.get(tool_call_id) if tool_call_id is not None else None
        if by_index is not None and by_id is not None and by_index is not by_id:
            self._raise(
                "tool call index and id refer to different calls",
                reason="conflicting_identity",
                tool_index=index,
                tool_call_id=tool_call_id,
            )
        if by_index is not None:
            return by_index
        if by_id is not None:
            return by_id

        if index is None and tool_call_id is None:
            if len(self._states) == 1:
                return self._states[0]
            self._raise(
                "tool call fragment has no index or id",
                reason="ambiguous_identity",
            )

        if len(self._states) == 1:
            only = self._states[0]
            if (
                (index is None and only.id is None)
                or (tool_call_id is None and only.index is None)
            ):
                self._raise(
                    "tool call fragment cannot be linked to the active call",
                    reason="ambiguous_identity",
                    tool_index=index,
                    tool_call_id=tool_call_id,
                )

        state = _ToolCallState(order=len(self._states))
        self._states.append(state)
        return state

    def _bind_identity(
        self,
        state: _ToolCallState,
        index: int | None,
        tool_call_id: str | None,
    ) -> None:
        if index is not None:
            if state.index is not None and state.index != index:
                self._raise_for_state(
                    state,
                    "tool call id was associated with multiple indexes",
                    reason="conflicting_index",
                    incoming_index=index,
                )
            state.index = index
            self._by_index[index] = state
        if tool_call_id is not None:
            if state.id is not None and state.id != tool_call_id:
                self._raise_for_state(
                    state,
                    "tool call index was associated with multiple ids",
                    reason="conflicting_id",
                    incoming_id=tool_call_id,
                )
            state.id = tool_call_id
            self._by_id[tool_call_id] = state

    def _raise_for_state(
        self,
        state: _ToolCallState,
        message: str,
        *,
        reason: str,
        **details: object,
    ) -> None:
        self._raise(
            message,
            reason=reason,
            tool_index=state.index,
            tool_call_id=state.id,
            tool_name=state.name,
            **details,
        )

    def _raise(self, message: str, **details: object) -> None:
        raise ProviderProtocolError(
            message,
            details={
                "phase": "tool_call_assembly",
                "provider": self._model.split("/", 1)[0],
                "model": self._model,
                **details,
            },
        )

    def _tool_call_index(self, call: object) -> int | None:
        value = _get_field(call, "index")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            self._raise(
                "tool call index must be a non-negative integer",
                reason="invalid_index",
                received_type=type(value).__name__,
                received_value=value if isinstance(value, (str, int)) else None,
            )
        return value

    def _tool_call_id(self, call: object) -> str | None:
        value = _get_field(call, "id")
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            self._raise(
                "tool call id must be a non-empty string",
                reason="invalid_id",
            )
        return value


def _decode_arguments(
    state: _ToolCallState,
    model: str,
) -> dict[str, object]:
    fragments = tuple(state.argument_fragments)
    if not fragments:
        return {}
    joined = "".join(fragments)
    joined_value = _json_object(joined)
    if joined_value is not None:
        return joined_value

    candidates = _argument_object_candidates(fragments)
    if len(candidates) == 1:
        return next(iter(candidates.values()))

    try:
        json.loads(joined)
    except json.JSONDecodeError as error:
        json_error = error
    else:
        json_error = None
    if json_error is None and not candidates:
        raise ProviderProtocolError(
            f"arguments for tool '{state.name}' must be a JSON object",
            details={
                **_argument_error_details(state, model, fragments, joined),
                "reason": "arguments_not_object",
            },
        )

    details = _argument_error_details(state, model, fragments, joined)
    details.update(
        {
            "reason": "ambiguous_arguments" if candidates else "invalid_json",
            "candidate_count": len(candidates),
        }
    )
    if json_error is not None:
        details["json_error"] = json_error.msg
        details["json_error_position"] = json_error.pos
    raise ProviderProtocolError(
        f"invalid streamed arguments for tool '{state.name}'",
        details=details,
    ) from json_error


def _argument_object_candidates(
    fragments: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    candidates: dict[str, dict[str, object]] = {}
    joined = "".join(fragments)

    _add_json_object_with_repeated_closing_suffix(
        candidates,
        fragments,
        joined,
    )

    complete_fragments: list[tuple[str, dict[str, object]]] = []
    for fragment in fragments:
        value = _json_object(fragment)
        if value is not None:
            complete_fragments.append((fragment, value))
    for text, value in complete_fragments:
        if all(
            text.startswith(fragment)
            or _json_object(fragment) == value
            for fragment in fragments
        ):
            candidates[_canonical_json(value)] = value

    cumulative = ""
    for fragment in fragments:
        if fragment.startswith(cumulative):
            cumulative = fragment
        elif cumulative.startswith(fragment):
            continue
        else:
            cumulative += fragment
    _add_json_object(candidates, cumulative)

    overlapped = ""
    for fragment in fragments:
        if fragment.startswith(overlapped):
            overlapped = fragment
        elif overlapped.startswith(fragment):
            continue
        else:
            overlap = _suffix_prefix_overlap(overlapped, fragment)
            overlapped += fragment[overlap:]
    _add_json_object(candidates, overlapped)

    for value in _concatenated_json_objects(joined):
        candidates[_canonical_json(value)] = value
    return candidates


def _add_json_object(
    candidates: dict[str, dict[str, object]],
    text: str,
) -> None:
    value = _json_object(text)
    if value is not None:
        candidates[_canonical_json(value)] = value


def _add_json_object_with_repeated_closing_suffix(
    candidates: dict[str, dict[str, object]],
    fragments: tuple[str, ...],
    text: str,
) -> None:
    """Recover arguments from a stream that repeats its closing sequence.

    Only a split stream counts as a repeated suffix: a single fragment with
    trailing delimiters is not evidence of a re-sent terminator, so it stays
    invalid instead of being silently truncated.
    """
    if len(fragments) < 2:
        return
    decoder = json.JSONDecoder()
    try:
        value, offset = decoder.raw_decode(text)
    except json.JSONDecodeError:
        return
    if not isinstance(value, dict):
        return
    suffix = text[offset:]
    closing_sequence = text[:offset].rstrip()
    closing_start = len(closing_sequence.rstrip("]}"))
    closing_sequence = closing_sequence[closing_start:]
    if not suffix or not closing_sequence or not set(suffix) <= {"]", "}"}:
        return
    for width in range(1, len(closing_sequence) + 1):
        unit = closing_sequence[-width:]
        if len(suffix) % width == 0 and suffix == unit * (len(suffix) // width):
            candidates[_canonical_json(value)] = value
            return


def _json_object(value: str) -> dict[str, object] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _concatenated_json_objects(value: str) -> tuple[dict[str, object], ...]:
    decoder = json.JSONDecoder()
    offset = 0
    decoded: list[dict[str, object]] = []
    while offset < len(value):
        while offset < len(value) and value[offset].isspace():
            offset += 1
        if offset == len(value):
            break
        try:
            item, offset = decoder.raw_decode(value, offset)
        except json.JSONDecodeError:
            return ()
        if not isinstance(item, dict):
            return ()
        decoded.append(item)
    return tuple(decoded) if len(decoded) > 1 else ()


def _suffix_prefix_overlap(left: str, right: str) -> int:
    for size in range(min(len(left), len(right)), 0, -1):
        if left.endswith(right[:size]):
            return size
    return 0


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _argument_error_details(
    state: _ToolCallState,
    model: str,
    fragments: tuple[str, ...],
    joined: str,
) -> dict[str, object]:
    return {
        "phase": "tool_call_assembly",
        "provider": model.split("/", 1)[0],
        "model": model,
        "tool_index": state.index,
        "tool_call_id": state.id,
        "tool_name": state.name,
        "arguments_length": len(joined),
        "argument_chunk_count": len(fragments),
        "argument_chunk_lengths": [len(fragment) for fragment in fragments],
        "arguments_sha256": sha256(joined.encode("utf-8")).hexdigest(),
    }


def _get_field(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _non_empty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None

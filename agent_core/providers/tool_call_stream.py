"""Strict assembly of streamed Tool Calls from provider deltas."""

from dataclasses import dataclass, field
from typing import Iterable

from agent_core.errors import ProviderProtocolError
from agent_core.tools import ToolCall

from ._utils import get_field


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

    @property
    def argument_fragment_count(self) -> int:
        return sum(len(state.argument_fragments) for state in self._states)

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
            result.append(ToolCall.from_raw_arguments(
                state.id, state.name, "".join(state.argument_fragments),
            ))
        return tuple(result)

    def _add(
        self,
        call: object,
        index: int | None,
        tool_call_id: str | None,
    ) -> None:
        state = self._resolve(index, tool_call_id)
        self._bind_identity(state, index, tool_call_id)

        function = get_field(call, "function")
        if function is None:
            return
        name = _non_empty_string(get_field(function, "name"))
        if name is not None:
            if state.name is not None and state.name != name:
                self._raise_for_state(
                    state,
                    "tool call name changed during the stream",
                    reason="conflicting_name",
                    incoming_name=name,
                )
            state.name = name
        arguments = get_field(function, "arguments")
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
        value = get_field(call, "index")
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
        value = get_field(call, "id")
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            self._raise(
                "tool call id must be a non-empty string",
                reason="invalid_id",
            )
        return value



def _non_empty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None

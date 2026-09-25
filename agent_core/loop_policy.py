"""Continuation and repetition rules for one Agent turn."""

import json
from typing import Callable

from .tools import ToolCall
from .turn_control import AgentCancelled, TurnControl, UserSteer


class ToolCallLimitExceededError(RuntimeError):
    def __init__(self, tool_name: str, limit: int) -> None:
        self.tool_name = tool_name
        self.limit = limit
        super().__init__(
            f"tool call '{tool_name}' exceeded the maximum of "
            f"{limit} identical consecutive executions"
        )


class ToolCallRepetitionGuard:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.reset()

    def reset(self) -> None:
        self._previous: tuple[str, str] | None = None
        self._count = 0

    def check(self, calls: tuple[ToolCall, ...]) -> None:
        """Validate the whole batch before committing its repetition count."""
        previous, count = self._previous, self._count
        for call in calls:
            key = (call.name, json.dumps(
                call.arguments, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ))
            count = count + 1 if key == previous else 1
            previous = key
            if count > self.limit:
                raise ToolCallLimitExceededError(call.name, self.limit)
        self._previous, self._count = previous, count


class TurnContinuationPolicy:
    """Consume external input at safe points, and close only a quiet turn."""

    def __init__(
        self,
        control: TurnControl | None,
        repetition: ToolCallRepetitionGuard,
        apply_jobs: Callable[[], tuple[bool, bool]],
        append_steers: Callable[[tuple[UserSteer, ...]], None],
    ) -> None:
        self.control = control
        self.repetition = repetition
        self._apply_jobs = apply_jobs
        self._append_steers = append_steers

    def raise_if_cancelled(self) -> None:
        if self.control is not None and self.control.cancelled:
            raise AgentCancelled

    def apply_jobs(self) -> tuple[bool, bool]:
        applied, pending = self._apply_jobs()
        if applied:
            self.repetition.reset()
        return applied, pending

    def apply_steering(self, *, finishing: bool = False) -> bool:
        if self.control is None:
            return False
        steers = self.control.finish() if finishing else self.control.drain_steering()
        self.raise_if_cancelled()
        if not steers:
            return False
        self._append_steers(steers)
        self.repetition.reset()
        return True

    def after_response(self, wait_for_job: Callable[[], bool]) -> bool:
        """Return whether another model call is needed after a final answer."""
        self.raise_if_cancelled()
        applied, pending = self.apply_jobs()
        if applied:
            return True
        while pending:
            self.raise_if_cancelled()
            if self.apply_steering():
                return True
            wait_for_job()
            self.raise_if_cancelled()
            applied, pending = self.apply_jobs()
            if applied:
                return True
        return self.apply_steering(finishing=True)

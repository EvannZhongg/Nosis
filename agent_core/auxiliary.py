"""Measure auxiliary model calls, including response validation."""

from datetime import datetime, timezone
from time import monotonic
from typing import Callable, Literal

from .llm import AuxiliaryModelCall, LLMResponse
from .turn_control import AgentCancelled


class AuxiliaryCallTracker:
    def __init__(
        self,
        on_call: Callable[[AuxiliaryModelCall], None] | None,
        *,
        task: Literal["compression", "memory"],
        scope: str | None = None,
        attempt: int = 1,
    ) -> None:
        self._on_call = on_call
        self._task = task
        self._scope = scope
        self._attempt = attempt
        self.response: LLMResponse | None = None

    def __enter__(self) -> "AuxiliaryCallTracker":
        self._started_at = datetime.now(timezone.utc)
        self._started = monotonic()
        return self

    def __exit__(self, _type, error, _traceback) -> None:
        if self._on_call is None:
            return
        status: Literal["completed", "invalid_response", "failed", "cancelled"] = "completed"
        if isinstance(error, (AgentCancelled, KeyboardInterrupt)):
            status = "cancelled"
        elif error is not None:
            status = "invalid_response" if self.response is not None else "failed"
        self._on_call(AuxiliaryModelCall(
            task=self._task, scope=self._scope, attempt=self._attempt,
            status=status, started_at_utc=self._started_at,
            duration_ms=round((monotonic() - self._started) * 1000),
            usage=self.response.usage if self.response is not None else None,
            error=(str(error) or None) if error is not None else None,
        ))

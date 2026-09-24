"""Turn lifecycle shared by interactive and unattended runtime hosts."""

from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Literal

from agent_core import AgentCancelled, AttachmentPart, TokenUsage, TurnControl, runtime_error_info
from agent_core.errors import RuntimeErrorInfo

from .callbacks import RuntimeCallbacks
from .execution_plane import ExecutionPlane


@dataclass(frozen=True)
class TurnResult:
    status: Literal["completed", "cancelled", "failed"]
    usage: TokenUsage | None = None
    error: RuntimeErrorInfo | None = None
    startup_failed: bool = False


class TurnRunner:
    def __init__(self, callbacks: RuntimeCallbacks) -> None:
        self.callbacks = callbacks
        self.turn_id: str | None = None
        self.control: TurnControl | None = None
        self._lock = Lock()
        self._queued: set[str] = set()
        self._pending_steers: dict[str, deque[tuple[str, str]]] = {}
        self._pending_cancels: set[str] = set()
        self._shutdown = False

    def enqueue(self, turn_id: str) -> None:
        with self._lock:
            self._queued.add(turn_id)

    def steer(self, turn_id: str, steer_id: str, text: str) -> bool:
        with self._lock:
            if self.control is not None and turn_id == self.turn_id:
                return self.control.steer(steer_id, text)
            if turn_id in self._queued and not self._shutdown:
                self._pending_steers.setdefault(turn_id, deque()).append((steer_id, text))
                return True
            return False

    def cancel(self, turn_id: str) -> bool:
        with self._lock:
            if self.control is not None and turn_id == self.turn_id:
                return self.control.cancel()
            if turn_id in self._queued:
                self._pending_cancels.add(turn_id)
            return False

    def shutdown(self) -> bool:
        with self._lock:
            self._shutdown = True
            return self.control.cancel() if self.control is not None else False

    def run_turn(
        self,
        turn_id: str,
        text: str,
        ensure_plane: Callable[[], ExecutionPlane],
        attachments: Callable[[], tuple[AttachmentPart, ...]],
    ) -> TurnResult:
        control = TurnControl()
        with self._lock:
            if self.turn_id is not None:
                raise RuntimeError("a turn is already running")
            self.turn_id = turn_id
            self.control = control
            self._queued.discard(turn_id)
            for steer_id, steer_text in self._pending_steers.pop(turn_id, ()):
                control.steer(steer_id, steer_text)
            if turn_id in self._pending_cancels or self._shutdown:
                control.cancel()
            self._pending_cancels.discard(turn_id)
        memory = None
        started = False
        try:
            plane = ensure_plane()
            started = True
            self.callbacks.on_phase("running", plane.runtime_warnings)
            plane.runtime_warnings = ()
            memory = plane.memory
            if memory is not None:
                memory.begin_turn()
            result = plane.agent.run(
                text,
                turn_id=turn_id,
                turn_control=control,
                attachments=attachments(),
                on_event=lambda event: self.callbacks.on_event(event, turn_id),
            )
            if memory is not None:
                try:
                    memory.reconcile_pending()
                except Exception as error:
                    plane.runtime_warnings = (
                        *plane.runtime_warnings,
                        f"Long-term memory update failed: {error}",
                    )
            return TurnResult("completed", usage=result.response.usage)
        except (KeyboardInterrupt, AgentCancelled):
            if memory is not None:
                memory.discard_pending()
            return TurnResult("cancelled")
        except Exception as error:
            if memory is not None:
                memory.discard_pending()
            return TurnResult("failed", error=runtime_error_info(error), startup_failed=not started)
        finally:
            with self._lock:
                self.control = None
                self.turn_id = None

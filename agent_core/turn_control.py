"""Thread-safe control messages for one active agent turn."""

from collections import deque
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class UserSteer:
    steer_id: str
    text: str


class UserSteeringMailbox:
    """Accept steering immediately and release it only at agent safe points."""

    def __init__(self) -> None:
        self._items: deque[UserSteer] = deque()
        self._accepting = True
        self._lock = Lock()

    def submit(self, steer: UserSteer) -> bool:
        with self._lock:
            if not self._accepting:
                return False
            self._items.append(steer)
            return True

    def drain(self) -> tuple[UserSteer, ...]:
        with self._lock:
            items = tuple(self._items)
            self._items.clear()
            return items

    def drain_or_close(self) -> tuple[UserSteer, ...]:
        """Drain pending steering, or atomically close an empty mailbox."""
        with self._lock:
            if self._items:
                items = tuple(self._items)
                self._items.clear()
                return items
            self._accepting = False
            return ()

    def close(self) -> None:
        with self._lock:
            self._accepting = False
            self._items.clear()

class TurnControl:
    """Runtime-owned controls that may arrive while an Agent is executing."""

    def __init__(self) -> None:
        self._steering = UserSteeringMailbox()
        self._cancelled = False
        self._finished = False
        self._lock = Lock()

    def steer(self, steer_id: str, text: str) -> bool:
        with self._lock:
            if self._cancelled or self._finished:
                return False
            return self._steering.submit(
                UserSteer(steer_id=steer_id, text=text)
            )

    def drain_steering(self) -> tuple[UserSteer, ...]:
        with self._lock:
            if self._cancelled or self._finished:
                return ()
            return self._steering.drain()

    def finish(self) -> tuple[UserSteer, ...]:
        with self._lock:
            if self._cancelled or self._finished:
                return ()
            steers = self._steering.drain_or_close()
            if not steers:
                self._finished = True
            return steers

    def cancel(self) -> bool:
        with self._lock:
            if self._finished or self._cancelled:
                return False
            self._cancelled = True
            self._steering.close()
            return True

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

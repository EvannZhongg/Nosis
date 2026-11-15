"""The ordered work submitted to one Session Runtime."""

from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Lock
from typing import Callable, Generic, TypeVar


@dataclass
class ScheduledTurn:
    session_id: str
    workspace: Path
    turn_id: str
    text: str
    started: Callable[[], None]
    completion: Future[None] = field(default_factory=Future)


Command = TypeVar("Command")


class SessionInbox(Generic[Command]):
    def __init__(self) -> None:
        self._queue: Queue[Command | ScheduledTurn | None] = Queue()
        self._lock = Lock()
        self._stopped = False
        self._scheduled = 0

    @property
    def scheduled(self) -> bool:
        with self._lock:
            return self._scheduled > 0

    def put(self, work: Command | ScheduledTurn | None) -> None:
        with self._lock:
            if work is None:
                self._stopped = True
            self._queue.put(work)

    def submit(self, turn: ScheduledTurn) -> None:
        with self._lock:
            if self._stopped:
                turn.completion.cancel()
                return
            self._scheduled += 1
            self._queue.put(turn)
        turn.completion.add_done_callback(self._scheduled_done)

    def _scheduled_done(self, future: Future[None]) -> None:
        with self._lock:
            self._scheduled -= 1

    def get(self) -> Command | ScheduledTurn | None:
        return self._queue.get()

    def close(self) -> None:
        pending = []
        with self._lock:
            self._stopped = True
            while True:
                try:
                    work = self._queue.get_nowait()
                except Empty:
                    break
                if isinstance(work, ScheduledTurn):
                    pending.append(work)
        for work in pending:
            work.completion.cancel()

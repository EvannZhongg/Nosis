"""Cancellation notifications shared by turns, jobs and process execution."""

from threading import Lock
from typing import Callable, Protocol


class CancellationSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]: ...


class CancellationSource:
    def __init__(self) -> None:
        self._cancel_lock = Lock()
        self._requested = False
        self._callbacks: set[Callable[[], None]] = set()

    @property
    def cancelled(self) -> bool:
        with self._cancel_lock:
            return self._requested

    def cancel(self) -> None:
        with self._cancel_lock:
            if self._requested:
                return
            self._requested = True
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            callback()

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._cancel_lock:
            requested = self._requested
            if not requested:
                self._callbacks.add(callback)
        if requested:
            callback()

        def unsubscribe() -> None:
            with self._cancel_lock:
                self._callbacks.discard(callback)

        return unsubscribe

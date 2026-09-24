"""Active GUI sessions and their bridge attachment lifecycle."""

import asyncio
from collections import deque

from fastapi.encoders import jsonable_encoder

from agent_core import JsonlSessionStore, Workspace, plan_snapshot_to_dict
from interfaces.bridge.protocol import attachment_replaced_message

from .bridge_process import BridgeProcess
from .runtime_projection import RuntimeProjection


EVENT_REPLAY_LIMIT = 512


class SessionRunningError(RuntimeError):
    """Raised when an operation requires an idle active session."""


class ActiveSession:
    """One bridge process whose lifetime is independent of a WebSocket."""

    def __init__(
        self,
        session_id: str,
        provider: str | None,
        workspace: str,
        items: list[object],
        bridge: BridgeProcess,
    ) -> None:
        self.session_id = session_id
        self.items = items
        self.bridge = bridge
        self.projection = RuntimeProjection(provider, workspace)
        self.done = False
        self._closed = False
        self._fatal_pending = False
        self._attachment_id: str | None = None
        self._subscriber: asyncio.Queue[dict[str, object] | None] | None = None
        self._subscriber_detached: asyncio.Event | None = None
        self._events: deque[dict[str, object]] = deque(maxlen=EVENT_REPLAY_LIMIT)
        self._event_sequence = 0
        self._lock = asyncio.Lock()
        self._reader = asyncio.create_task(self._read_bridge())

    async def attach(
        self,
        after_event: int,
        attachment_id: str,
        *,
        takeover: bool,
    ) -> tuple[
        asyncio.Queue[dict[str, object] | None],
        list[dict[str, object]],
        dict[str, object],
    ] | None:
        claimed = False
        while True:
            async with self._lock:
                replacing = (
                    self._attachment_id is not None
                    and self._attachment_id != attachment_id
                )
                if replacing and (
                    claimed or (not takeover and self._subscriber is not None)
                ):
                    return None
                self._attachment_id = attachment_id
                claimed = True
                if self._subscriber is None:
                    queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
                    self._subscriber = queue
                    self._subscriber_detached = asyncio.Event()
                    events = [
                        event
                        for event in self._events
                        if event["event_sequence"] > after_event
                    ]
                    state = self.projection.runtime_state(
                        # The page holds everything the stream emitted, so
                        # its cursor is the newest event; a pending fatal is
                        # left out of it so that a page attaching later
                        # still asks for one.
                        event_sequence=(
                            self.delivered_event_sequence
                            if self._fatal_pending
                            else self._event_sequence
                        )
                    )
                    if self.done:
                        queue.put_nowait(None)
                    return queue, events, state
                previous = self._subscriber
                detached = self._subscriber_detached
                if replacing:
                    previous.put_nowait(
                        attachment_replaced_message(
                            phase=self.projection.phase
                        )
                    )
                previous.put_nowait(None)
            if detached is not None:
                await detached.wait()

    async def detach(
        self,
        queue: asyncio.Queue[dict[str, object] | None],
        attachment_id: str,
    ) -> None:
        async with self._lock:
            if self._subscriber is queue:
                self._subscriber = None
                if self._subscriber_detached is not None:
                    self._subscriber_detached.set()
                    self._subscriber_detached = None

    def send(self, message: dict[str, object]) -> None:
        self.projection.apply_client_message(message)
        if message.get("type") == "user_turn":
            content: object = str(message.get("text", ""))
            attachments = message.get("attachments")
            if isinstance(attachments, list) and attachments:
                content = [
                    {"type": "text", "text": str(message.get("text", ""))},
                    *attachments,
                ]
            self.items.append({"role": "user", "content": content})
        self.bridge.send(message)

    def cancel_turn(self) -> None:
        self.bridge.cancel_turn()

    def owns_attachment(self, attachment_id: str) -> bool:
        return self._attachment_id == attachment_id

    @property
    def attachable(self) -> bool:
        return not self.done or self._fatal_pending

    @property
    def running(self) -> bool:
        return self.projection.running

    @property
    def fatal_pending(self) -> bool:
        return self._fatal_pending

    @property
    def event_sequence(self) -> int:
        return self._event_sequence

    @property
    def delivered_event_sequence(self) -> int:
        """The newest event the items handed out with this Runtime reflect.

        A running Runtime holds only the user turns it accepted, so a page
        that reads it must still be sent every event it emitted. An idle one
        is read from its journal, which already holds normal turn events. A
        fatal is not part of that transcript and remains pending until a page
        receives it.
        """
        if self.running:
            return 0
        if self._fatal_pending:
            return max(0, self._event_sequence - 1)
        return self._event_sequence

    def mark_fatal_delivered(self) -> None:
        self._fatal_pending = False

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.done = True
            await self.bridge.close()
        if not self._reader.done() and self._reader is not asyncio.current_task():
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        async with self._lock:
            if self._subscriber is not None:
                self._subscriber.put_nowait(None)

    async def _read_bridge(self) -> None:
        try:
            while True:
                message = await self.bridge.read()
                if message is None:
                    break
                self.projection.apply_bridge_message(message)
                if message.get("type") == "fatal":
                    self._fatal_pending = True
                async with self._lock:
                    self._event_sequence += 1
                    sequenced = {**message, "event_sequence": self._event_sequence}
                    self._events.append(sequenced)
                    if self._subscriber is not None:
                        self._subscriber.put_nowait(sequenced)
                if message.get("type") == "fatal":
                    break
        finally:
            unexpected_exit = not self._closed
            self.projection.bridge_finished(
                unexpected=unexpected_exit,
                returncode=self.bridge.returncode,
            )
            self.done = True
            if unexpected_exit:
                self._closed = True
                await self.bridge.close()
            async with self._lock:
                if self._subscriber is not None:
                    self._subscriber.put_nowait(None)


class ActiveSessionRegistry:
    """Own active bridge sessions and their cross-request invariants."""

    def __init__(
        self,
        store: JsonlSessionStore,
    ) -> None:
        self._store = store
        self._sessions: dict[str, ActiveSession] = {}
        self._lock = asyncio.Lock()

    async def open_or_attach(
        self,
        opening_message: dict[str, object],
        workspace: Workspace,
        *,
        attach_only: bool,
        attachment_id: str,
    ) -> tuple[ActiveSession | None, bool]:
        session_id = str(opening_message["session_id"])
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is not None and current.done and current.fatal_pending:
                return current, False
            provider = opening_message.get("provider")
            selected_provider = provider if isinstance(provider, str) else None
            selected_workspace = str(workspace.path)
            if current is not None and not current.done:
                same_configuration = (
                    current.projection.provider == selected_provider
                    and current.projection.workspace == selected_workspace
                )
                if (
                    attach_only
                    or current.running
                    or same_configuration
                    or not current.owns_attachment(attachment_id)
                ):
                    return current, False
                self._sessions.pop(session_id, None)
                await current.close()
            if current is not None:
                self._sessions.pop(session_id, None)
            if attach_only:
                return None, False
            bridge = await BridgeProcess.spawn(workspace)
            stored = self._store.load(session_id, recover=False)
            runtime = ActiveSession(
                session_id,
                selected_provider,
                selected_workspace,
                jsonable_encoder(stored.items),
                bridge,
            )
            runtime.projection.plan = (
                plan_snapshot_to_dict(stored.plan)
                if stored.plan is not None
                else None
            )
            self._sessions[session_id] = runtime
            bridge.send(opening_message)
            return runtime, True

    async def get(self, session_id: str) -> ActiveSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def list_attachable(self) -> list[ActiveSession]:
        async with self._lock:
            return [
                runtime
                for runtime in self._sessions.values()
                if runtime.attachable
            ]

    async def release_idle(
        self,
        session_id: str,
        *,
        provider: str | None = None,
        attachment_id: str | None = None,
    ) -> bool:
        async with self._lock:
            runtime = self._sessions.get(session_id)
            if runtime is None:
                return False
            if provider is not None and runtime.projection.provider != provider:
                return False
            if (
                attachment_id is not None
                and not runtime.owns_attachment(attachment_id)
            ):
                return False
            if runtime.running:
                raise SessionRunningError("session is running")
            self._sessions.pop(session_id, None)
            await runtime.close()
            return True

    async def remove_if_finished(self, runtime: ActiveSession) -> None:
        if runtime.attachable:
            return
        async with self._lock:
            if self._sessions.get(runtime.session_id) is runtime:
                self._sessions.pop(runtime.session_id, None)

    async def close_all(self) -> None:
        async with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(
            *(runtime.close() for runtime in sessions),
            return_exceptions=True,
        )

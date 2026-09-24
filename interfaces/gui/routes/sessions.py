"""Stored sessions and live bridge WebSocket endpoints."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from agent_core import JsonlSessionStore, Workspace, plan_snapshot_to_dict
from agent_runtime.settings import SettingsStore
from interfaces.bridge.managed_workspaces import (
    delete_scratch_workspace,
    is_scratch_workspace,
)
from interfaces.bridge.protocol import (
    attachment_replaced_message,
    runtime_state_message,
)

from ..active_session import (
    ActiveSession,
    ActiveSessionRegistry,
    SessionRunningError,
)
from .workspace import WorkspaceResolver


# Messages the browser may forward to the bridge verbatim. ``open_session``
# is excluded because the server owns the Workspace and provider selection.
RELAYED_MESSAGE_TYPES = frozenset(
    {
        "user_turn",
        "user_steer",
        "approval_response",
        "permission_set",
        "provider_set",
        "workspace_set",
        "user_question_response",
    }
)


def create_sessions_router(
    resolver: WorkspaceResolver,
    store: JsonlSessionStore,
    settings: SettingsStore,
    active_sessions: ActiveSessionRegistry,
    *,
    host: str,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/active-sessions")
    async def list_active_sessions() -> list[dict[str, object]]:
        result = []
        for runtime in await active_sessions.list_attachable():
            projection = runtime.projection
            items = runtime.items
            if not runtime.running:
                items = jsonable_encoder(
                    store.load(runtime.session_id, recover=True).items
                )
            result.append(
                {
                    "session_id": runtime.session_id,
                    "provider": projection.provider,
                    "workspace": projection.workspace,
                    "items": items,
                    "phase": projection.phase,
                    "permission_preset": projection.permission_preset,
                    "context_window": projection.context_window,
                    "event_sequence": runtime.delivered_event_sequence,
                    "plan": projection.plan,
                }
            )
        return result

    @router.get("/api/sessions")
    def list_sessions() -> list[dict[str, object]]:
        return [
            {
                **group,
                **(
                    {"scratch": True}
                    if is_scratch_workspace(str(group["workspace"]))
                    else {}
                ),
            }
            for group in store.list_sessions()
        ]

    @router.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> object:
        try:
            runtime = await active_sessions.get(session_id)
            if runtime is not None and runtime.running:
                projection = runtime.projection
                return {
                    "session_id": runtime.session_id,
                    "items": runtime.items,
                    "workspace": projection.workspace,
                    "provider": projection.provider,
                    "permission_preset": projection.permission_preset,
                    "context_window": projection.context_window,
                    "event_sequence": runtime.delivered_event_sequence,
                    "plan": projection.plan,
                }
            session = store.load(session_id, recover=True)
            return jsonable_encoder(
                {
                    "session_id": session.session_id,
                    "items": session.items,
                    "workspace": session.workspace,
                    "provider": store.provider_for(session.session_id),
                    "permission_preset": session.permission_preset.value,
                    "context_window": (
                        runtime.projection.context_window
                        if runtime is not None
                        else None
                    ),
                    "event_sequence": (
                        runtime.delivered_event_sequence
                        if runtime is not None
                        else 0
                    ),
                    "plan": (
                        plan_snapshot_to_dict(session.plan)
                        if session.plan is not None
                        else None
                    ),
                }
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.delete("/api/active-sessions/{session_id}")
    async def release_active_session(
        session_id: str,
        provider: str | None = None,
        attachment_id: str | None = None,
    ) -> dict[str, bool]:
        try:
            released = await active_sessions.release_idle(
                session_id,
                provider=provider,
                attachment_id=attachment_id,
            )
        except SessionRunningError as error:
            raise HTTPException(
                status_code=409,
                detail="会话正在运行。",
            ) from error
        return {"released": released}

    @router.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        try:
            try:
                await active_sessions.release_idle(session_id)
            except SessionRunningError as error:
                raise HTTPException(
                    status_code=409,
                    detail="会话正在运行。",
                ) from error
            workspace_path = store.workspace_for(session_id)
            deleted = store.delete_session(session_id)
            # A scratch workspace can hold several Sessions; it is removed
            # once the last one is gone.
            if (
                deleted
                and workspace_path is not None
                and not store.list_workspace_sessions(workspace_path)
            ):
                delete_scratch_workspace(workspace_path)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if not deleted:
            raise HTTPException(status_code=404, detail="会话不存在。")
        return {"deleted": True}

    @router.websocket("/api/session")
    async def run_session(websocket: WebSocket) -> None:
        # A page served from elsewhere must not be able to drive the agent.
        origin = websocket.headers.get("origin")
        allowed_origins = {
            f"http://{websocket.headers.get('host')}",
            f"http://{host}:5173",
            "http://localhost:5173",
        }
        if origin is not None and origin not in allowed_origins:
            await websocket.close(code=1008)
            return

        await websocket.accept()
        try:
            opening = await websocket.receive_json()
            opening_session_id = (
                opening.get("session_id") if isinstance(opening, dict) else None
            )
            if opening_session_id is not None and not isinstance(
                opening_session_id,
                str,
            ):
                opening_session_id = None
            requested_workspace = (
                opening.get("workspace") if isinstance(opening, dict) else None
            )
            bound_workspace = (
                store.workspace_for(opening_session_id)
                if opening_session_id
                else None
            )
            current_workspace = (
                Workspace(Path(requested_workspace))
                if bound_workspace is None
                and isinstance(requested_workspace, str)
                and requested_workspace
                else resolver.for_session(opening_session_id)
            )
            _, models = settings.model_options()
            opening_message = _open_session_message(
                opening,
                current_workspace,
                models,
            )
        except WebSocketDisconnect:
            return
        except (TypeError, ValueError) as error:
            await _send_protocol_error(websocket, str(error))
            return

        attach_only = opening.get("attach_only") is True
        attachment_id = opening.get("attachment_id")
        if not isinstance(attachment_id, str) or not attachment_id:
            await _send_protocol_error(
                websocket,
                "'attachment_id' must be a non-empty string",
            )
            return
        takeover = opening.get("takeover") is True
        after_event = opening.get("after_event", 0)
        if (
            not isinstance(after_event, int)
            or isinstance(after_event, bool)
            or after_event < 0
        ):
            await _send_protocol_error(
                websocket,
                "'after_event' must be a non-negative integer",
            )
            return
        runtime, created = await active_sessions.open_or_attach(
            opening_message,
            current_workspace,
            attach_only=attach_only,
            attachment_id=attachment_id,
        )
        if runtime is None:
            await websocket.send_json(
                runtime_state_message(
                    phase="inactive",
                    turn_id=None,
                    approval=None,
                    question=None,
                    provider=None,
                    permission_preset="ask_for_approval",
                    context_window=None,
                    event_sequence=0,
                    jobs=[],
                )
            )
            await websocket.close()
            return
        attachment = await runtime.attach(
            after_event,
            attachment_id,
            takeover=takeover,
        )
        if attachment is None:
            await websocket.send_json(
                attachment_replaced_message(phase=runtime.projection.phase)
            )
            await websocket.close()
            return
        queue, events, state = attachment
        fatal_delivered = False
        try:
            replays_fatal = any(
                message.get("type") == "fatal" for message in events
            )
            if not created and replays_fatal:
                await websocket.send_json(state)
            for message in events:
                await websocket.send_json(
                    message if created else {**message, "replayed": True}
                )
                fatal_delivered = message.get("type") == "fatal"
            if not created and not replays_fatal:
                await websocket.send_json(state)
            try:
                fatal_delivered = (
                    await _relay(websocket, runtime, queue, attachment_id)
                    or fatal_delivered
                )
            except asyncio.CancelledError:
                # ASGI servers may cancel the handler when the browser drops
                # the socket. Detaching must not cancel the runtime with it.
                pass
        finally:
            if fatal_delivered:
                runtime.mark_fatal_delivered()
            await asyncio.shield(runtime.detach(queue, attachment_id))
            await active_sessions.remove_if_finished(runtime)

    return router


async def _send_protocol_error(websocket: WebSocket, message: str) -> None:
    await websocket.send_json(
        {
            "type": "fatal",
            "error": {
                "type": "ProtocolError",
                "message": message,
                "details": {},
            },
        }
    )
    await websocket.close()


def _open_session_message(
    opening: object,
    workspace: Workspace,
    models: dict[str, str],
) -> dict[str, object]:
    """Build the bridge's session-open command from the browser's choice."""
    if not isinstance(opening, dict) or opening.get("type") != "open_session":
        raise ValueError("first message must be 'open_session'")

    session_id = opening.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("'session_id' must be a non-empty string")

    provider = opening.get("provider")
    if provider is not None and provider not in models:
        raise ValueError("请选择已配置的模型。")

    return {
        "type": "open_session",
        "workspace": str(workspace.path),
        "session_id": session_id,
        "provider": provider,
    }


async def _relay(
    websocket: WebSocket,
    runtime: ActiveSession,
    queue: asyncio.Queue[dict[str, object] | None],
    attachment_id: str,
) -> bool:
    """Pump messages between one browser attachment and its runtime."""

    async def browser_to_bridge() -> None:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            if not runtime.owns_attachment(attachment_id):
                continue
            message_type = message.get("type")
            if message_type == "cancel":
                if message.get("turn_id") == runtime.projection.turn_id:
                    runtime.cancel_turn()
            elif message_type in RELAYED_MESSAGE_TYPES:
                runtime.send(message)

    fatal_delivered = False

    async def bridge_to_browser() -> None:
        nonlocal fatal_delivered
        while True:
            message = await queue.get()
            if message is None:
                return
            await websocket.send_json(message)
            if message.get("type") == "fatal":
                fatal_delivered = True

    tasks = [
        asyncio.create_task(browser_to_bridge()),
        asyncio.create_task(bridge_to_browser()),
    ]
    try:
        done, _ = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            # A page closing mid-turn is normal; anything else is a bug
            # and belongs in the server log.
            if error is not None and not isinstance(
                error,
                (WebSocketDisconnect, json.JSONDecodeError),
            ):
                raise error
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return fatal_delivered

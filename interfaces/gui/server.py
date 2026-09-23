"""HTTP and WebSocket front end for the agent bridge.

The server owns the lifecycle of bridge processes while WebSockets only
attach browsers to them. Agent semantics remain inside the bridge/core.
"""

import argparse
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import signal
import shutil
import subprocess
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from uuid import uuid4
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agent_core import (
    JsonlSessionStore,
    ListDirectoryTool,
    MemoryDocument,
    Session,
    ToolExecutionContext,
    UnsupportedImageError,
    Workspace,
    image_extension,
    plan_snapshot_to_dict,
    probe_image,
    SchedulerService,
    parse_schedule_update_input,
    trigger_to_dict,
)
from agent_core.path_utils import path_for_comparison

from ..bridge.config import (
    default_config_directory,
    initialize_config_directory,
    load_scratch_workspace_root,
    memory_store,
)
from ..bridge.managed_workspaces import (
    create_scratch_workspace,
    is_scratch_workspace,
)
from ..bridge.process import cancel_process
from ..bridge.protocol import attachment_replaced_message, runtime_state_message
from ..bridge.settings import SettingsStore


STATIC_PATH = Path(__file__).resolve().parent / "static"
HOST = "127.0.0.1"
PORT = 8737
SHUTDOWN_TIMEOUT_SECONDS = 2
EVENT_REPLAY_LIMIT = 512
IMAGE_URL_TTL_SECONDS = 5 * 60
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


class BridgeProcess:
    """A bridge child process addressed as a protocol message stream."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._opened = False
        self._turn_running = False
        self._turn_id: str | None = None

    @classmethod
    async def spawn(cls, workspace: Workspace) -> "BridgeProcess":
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "interfaces.bridge",
            cwd=workspace.path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Its own process group keeps a cancel interrupt aimed at this
            # child instead of at the console every process shares.
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
            start_new_session=os.name != "nt",
        )
        return cls(process)

    def send(self, message: dict[str, object]) -> None:
        stdin = self._process.stdin
        if stdin is None or stdin.is_closing():
            return
        if message.get("type") == "user_turn":
            self._turn_running = True
            self._turn_id = str(message.get("turn_id"))
        stdin.write(
            (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        )

    async def read(self) -> dict[str, object] | None:
        """Return the next message from the bridge, or None once it ends."""
        stdout = self._process.stdout
        if stdout is None:
            return None
        while True:
            line = await stdout.readline()
            if not line:
                await self._process.wait()
                return None
            stripped = line.strip()
            if stripped:
                message = json.loads(stripped)
                message_type = message.get("type")
                if message_type == "session_ready":
                    self._opened = True
                if message_type in {
                    "turn_completed",
                    "turn_cancelled",
                    "turn_failed",
                }:
                    self._turn_running = False
                    self._turn_id = None
                return message

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def cancel_turn(self) -> None:
        """Route cancellation to the active turn."""
        if self._turn_running and self._turn_id is not None:
            self.send({"type": "cancel", "turn_id": self._turn_id})

    async def close(self) -> None:
        # Explicit Runtime shutdown routes cancellation first so a running
        # turn can journal what it already produced before the process exits.
        if not self._opened and self._process.returncode is None:
            cancel_process(self._process)
        else:
            self.cancel_turn()
        self.send({"type": "shutdown"})
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            await asyncio.wait_for(
                self._process.wait(),
                timeout=SHUTDOWN_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, ConnectionResetError):
            self._kill()
            await self._process.wait()

    def _kill(self) -> None:
        """Kill the bridge together with the processes it started.

        A venv ``python.exe`` is a launcher, so terminating only the process
        that was spawned would leave the real bridge running on the session
        and holding its MCP servers open.
        """
        if self._process.returncode is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self._process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


TURN_END_MESSAGE_TYPES = frozenset(
    {"turn_completed", "turn_cancelled", "turn_failed"}
)


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
        self.provider = provider
        self.workspace = workspace
        self.items = items
        self.bridge = bridge
        self.phase = "inactive"
        self.turn_id: str | None = None
        self.approval: dict[str, object] | None = None
        self.question: dict[str, object] | None = None
        self.permission_preset = "ask_for_approval"
        self.context_window: dict[str, object] | None = None
        self.plan: dict[str, object] | None = None
        self.runtime_warnings: tuple[str, ...] = ()
        self.jobs: dict[str, dict[str, object]] = {}
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
                    state = runtime_state_message(
                        phase=self.phase,
                        turn_id=self.turn_id,
                        approval=self.approval,
                        question=self.question,
                        provider=self.provider,
                        permission_preset=self.permission_preset,
                        context_window=self.context_window,
                        jobs=list(self.jobs.values()),
                        runtime_warnings=self.runtime_warnings,
                        plan=self.plan,
                        # The page holds everything the stream emitted, so
                        # its cursor is the newest event; a pending fatal is
                        # left out of it so that a page attaching later
                        # still asks for one.
                        event_sequence=(
                            self.delivered_event_sequence
                            if self._fatal_pending
                            else self._event_sequence
                        ),
                    )
                    if self.done:
                        queue.put_nowait(None)
                    return queue, events, state
                previous = self._subscriber
                detached = self._subscriber_detached
                if replacing:
                    previous.put_nowait(
                        attachment_replaced_message(phase=self.phase)
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
        if message.get("type") == "user_turn":
            self.phase = "starting"
            self.turn_id = str(message.get("turn_id"))
            self.approval = None
            self.question = None
            content: object = str(message.get("text", ""))
            attachments = message.get("attachments")
            if isinstance(attachments, list) and attachments:
                content = [
                    {"type": "text", "text": str(message.get("text", ""))},
                    *attachments,
                ]
            self.items.append({"role": "user", "content": content})
        elif message.get("type") == "approval_response":
            self.approval = None
        elif message.get("type") == "user_question_response":
            self.question = None
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
        return self.phase in {
            "starting",
            "running",
            "waiting_approval",
            "waiting_user",
        }

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
                message_type = message.get("type")
                if message_type == "approval_request":
                    self.approval = message
                    self.question = None
                elif message_type == "user_question":
                    self.approval = None
                    self.question = message
                elif message_type in {"session_ready", "permission_changed"}:
                    preset = message.get("permission_preset", message.get("preset"))
                    if isinstance(preset, str):
                        self.permission_preset = preset
                    if message_type == "session_ready":
                        provider = message.get("provider")
                        if isinstance(provider, str):
                            self.provider = provider
                elif message_type == "provider_changed":
                    provider = message.get("provider")
                    if isinstance(provider, str):
                        self.provider = provider
                elif message_type == "workspace_changed":
                    workspace = message.get("workspace")
                    if isinstance(workspace, str):
                        self.workspace = workspace
                elif message_type == "runtime_state":
                    phase = message.get("phase")
                    if isinstance(phase, str):
                        self.phase = phase
                    provider = message.get("provider")
                    if isinstance(provider, str):
                        self.provider = provider
                    permission_preset = message.get("permission_preset")
                    if isinstance(permission_preset, str):
                        self.permission_preset = permission_preset
                    turn_id = message.get("turn_id")
                    self.turn_id = turn_id if isinstance(turn_id, str) else None
                    approval = message.get("approval")
                    self.approval = approval if isinstance(approval, dict) else None
                    question = message.get("question")
                    self.question = question if isinstance(question, dict) else None
                    context_window = message.get("context_window")
                    self.context_window = (
                        context_window if isinstance(context_window, dict) else None
                    )
                    jobs = message.get("jobs")
                    self.jobs = {
                        str(job["job_id"]): job
                        for job in jobs
                        if isinstance(job, dict) and isinstance(job.get("job_id"), str)
                    } if isinstance(jobs, list) else {}
                    warnings = message.get("runtime_warnings")
                    if isinstance(warnings, list) and warnings:
                        self.runtime_warnings = tuple(
                            warning for warning in warnings
                            if isinstance(warning, str)
                        )
                    plan = message.get("plan")
                    self.plan = plan if isinstance(plan, dict) else None
                elif message_type == "plan_updated":
                    plan = message.get("plan")
                    self.plan = plan if isinstance(plan, dict) else None
                elif message_type == "context_window":
                    self.context_window = {
                        key: message[key]
                        for key in (
                            "input_tokens",
                            "max_input_tokens",
                            "max_context_tokens",
                            "output_reserve_tokens",
                            "compression_threshold",
                            "compression_count",
                        )
                    }
                elif message_type == "job_status":
                    job_id = message.get("job_id")
                    if isinstance(job_id, str):
                        if message.get("status") in {
                            "completed", "failed", "cancelled"
                        }:
                            self.jobs.pop(job_id, None)
                        else:
                            self.jobs[job_id] = message
                elif message_type in TURN_END_MESSAGE_TYPES:
                    self.phase = "idle"
                    self.turn_id = None
                    self.approval = None
                    self.question = None
                    self.jobs.clear()
                    self.runtime_warnings = ()
                elif message_type == "fatal":
                    self.phase = "failed"
                    self.turn_id = None
                    self.approval = None
                    self.question = None
                    self.jobs.clear()
                    self._fatal_pending = True
                async with self._lock:
                    self._event_sequence += 1
                    sequenced = {**message, "event_sequence": self._event_sequence}
                    self._events.append(sequenced)
                    if self._subscriber is not None:
                        self._subscriber.put_nowait(sequenced)
                if message_type == "fatal":
                    break
        finally:
            unexpected_exit = not self._closed
            if unexpected_exit and (
                self.running or self.bridge.returncode != 0
            ):
                self.phase = "failed"
            self.approval = None
            self.question = None
            self.jobs.clear()
            self.done = True
            if unexpected_exit:
                self._closed = True
                await self.bridge.close()
            async with self._lock:
                if self._subscriber is not None:
                    self._subscriber.put_nowait(None)


def create_app(
    workspace: Workspace,
    store: JsonlSessionStore,
    settings: SettingsStore,
) -> FastAPI:
    active_sessions: dict[str, ActiveSession] = {}
    active_session_lock = asyncio.Lock()
    image_url_secret = secrets.token_bytes(32)
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await asyncio.gather(
            *(runtime.close() for runtime in tuple(active_sessions.values())),
            return_exceptions=True,
        )

    app = FastAPI(
        title="Nosis",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[HOST, "localhost"])

    async def active_session_for(
        opening_message: dict[str, object],
        current_workspace: Workspace,
        *,
        attach_only: bool,
        attachment_id: str,
    ) -> tuple[ActiveSession | None, bool]:
        session_id = str(opening_message["session_id"])
        async with active_session_lock:
            current = active_sessions.get(session_id)
            if current is not None and current.done and current.fatal_pending:
                return current, False
            provider = opening_message.get("provider")
            selected_provider = provider if isinstance(provider, str) else None
            selected_workspace = str(current_workspace.path)
            if current is not None and not current.done:
                same_configuration = (
                    current.provider == selected_provider
                    and current.workspace == selected_workspace
                )
                if (
                    attach_only
                    or current.running
                    or same_configuration
                    or not current.owns_attachment(attachment_id)
                ):
                    return current, False
                active_sessions.pop(session_id, None)
                await current.close()
            if current is not None:
                active_sessions.pop(session_id, None)
            if attach_only:
                return None, False
            bridge = await BridgeProcess.spawn(current_workspace)
            stored = store.load(session_id, recover=False)
            runtime = ActiveSession(
                session_id,
                selected_provider,
                selected_workspace,
                jsonable_encoder(stored.items),
                bridge,
            )
            runtime.plan = (
                plan_snapshot_to_dict(stored.plan)
                if stored.plan is not None
                else None
            )
            active_sessions[session_id] = runtime
            bridge.send(opening_message)
            return runtime, True

    @app.get("/api/models")
    def list_models() -> dict[str, object]:
        default_provider, models = settings.model_options()
        return {
            "default": default_provider,
            "models": [
                {"id": name, "model": model}
                for name, model in models.items()
            ],
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, object]:
        try:
            return settings.snapshot()
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/memory")
    def get_memory() -> dict[str, object]:
        try:
            global_memory, workspaces = memory_store(settings.directory).load_all()
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "global": _memory_document_to_dict(global_memory),
            "workspaces": [
                {
                    "workspace": workspace_path,
                    "memory": _memory_document_to_dict(document),
                }
                for workspace_path, document in sorted(workspaces.items())
            ],
        }

    @app.get("/api/schedules")
    def list_schedules() -> list[dict[str, object]]:
        current = SchedulerService(settings.directory / "schedule.jsonl")
        latest_runs = {}
        for run in current.runs:
            previous = latest_runs.get(run.schedule_id)
            if previous is None or run.scheduled_for > previous.scheduled_for:
                latest_runs[run.schedule_id] = run
        return [
            {
                "schedule_id": item.schedule_id,
                "prompt": item.action.prompt,
                "trigger": trigger_to_dict(item.trigger),
                "workspace": item.workspace,
                "execution_scope": item.execution_scope.value,
                "origin_session_id": item.origin_session_id,
                "schedule_session_id": item.schedule_session_id,
                "session_available": store.has_journal(item.schedule_session_id),
                "enabled": item.enabled,
                "end_at": item.end_at.isoformat() if item.end_at else None,
                "next_run_at": (
                    item.next_run_at.isoformat() if item.next_run_at else None
                ),
                "latest_run": (
                    {
                        "run_id": latest_runs[item.schedule_id].run_id,
                        "status": latest_runs[item.schedule_id].status,
                        "scheduled_for": latest_runs[
                            item.schedule_id
                        ].scheduled_for.isoformat(),
                        "started_at": (
                            latest_runs[item.schedule_id].started_at.isoformat()
                            if latest_runs[item.schedule_id].started_at
                            else None
                        ),
                        "finished_at": (
                            latest_runs[item.schedule_id].finished_at.isoformat()
                            if latest_runs[item.schedule_id].finished_at
                            else None
                        ),
                        "error": latest_runs[item.schedule_id].error,
                    }
                    if item.schedule_id in latest_runs
                    else None
                ),
            }
            for item in current.schedules
        ]

    @app.put("/api/schedules/{schedule_id}")
    def update_schedule(
        schedule_id: str,
        payload: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            current = SchedulerService(settings.directory / "schedule.jsonl")
            changes = parse_schedule_update_input(payload)
            item = current.update_schedule(
                schedule_id,
                **changes,
            )
            return {
                "schedule_id": item.schedule_id,
                "schedule_session_id": item.schedule_session_id,
                "trigger": trigger_to_dict(item.trigger),
                "execution_scope": item.execution_scope.value,
                "enabled": item.enabled,
                "end_at": item.end_at.isoformat() if item.end_at else None,
                "next_run_at": (
                    item.next_run_at.isoformat() if item.next_run_at else None
                ),
            }
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.put("/api/settings/providers/{provider_id}")
    def save_provider_settings(
        provider_id: str,
        payload: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            revision = payload.pop("expected_revision", None)
            return settings.save_provider(
                provider_id,
                payload,
                revision if isinstance(revision, str) else None,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.put("/api/settings/agent")
    def save_agent_settings(
        payload: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            revision = payload.pop("expected_revision", None)
            return settings.save_agent(
                payload,
                revision if isinstance(revision, str) else None,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.put("/api/settings/routing")
    def save_routing_settings(
        payload: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            revision = payload.pop("expected_revision", None)
            return settings.save_routing(
                payload,
                revision if isinstance(revision, str) else None,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/active-sessions")
    async def list_active_sessions() -> list[dict[str, object]]:
        async with active_session_lock:
            result = []
            for runtime in active_sessions.values():
                if not runtime.attachable:
                    continue
                items = runtime.items
                if not runtime.running:
                    items = jsonable_encoder(
                        store.load(runtime.session_id, recover=True).items
                    )
                result.append({
                    "session_id": runtime.session_id,
                    "provider": runtime.provider,
                    "workspace": runtime.workspace,
                    "items": items,
                    "phase": runtime.phase,
                    "permission_preset": runtime.permission_preset,
                    "context_window": runtime.context_window,
                    "event_sequence": runtime.delivered_event_sequence,
                    "plan": runtime.plan,
                })
            return result

    @app.get("/api/sessions")
    def list_sessions() -> list[dict[str, object]]:
        return [
            {
                **group,
                **(
                    {"scratch": True}
                    if is_scratch_workspace(
                        str(group["workspace"]),
                    )
                    else {}
                ),
            }
            for group in store.list_sessions()
        ]

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> object:
        try:
            async with active_session_lock:
                runtime = active_sessions.get(session_id)
            if runtime is not None and runtime.running:
                return {
                    "session_id": runtime.session_id,
                    "items": runtime.items,
                    "workspace": runtime.workspace,
                    "provider": runtime.provider,
                    "permission_preset": runtime.permission_preset,
                    "context_window": runtime.context_window,
                    "event_sequence": runtime.delivered_event_sequence,
                    "plan": runtime.plan,
                }
            session = store.load(
                session_id,
                recover=True,
            )
            return jsonable_encoder(
                {
                    "session_id": session.session_id,
                    "items": session.items,
                    "workspace": session.workspace,
                    "provider": store.provider_for(session.session_id),
                    "permission_preset": session.permission_preset.value,
                    "context_window": (
                        runtime.context_window
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

    async def release_idle_session(
        session_id: str,
        *,
        provider: str | None = None,
        attachment_id: str | None = None,
    ) -> bool:
        async with active_session_lock:
            runtime = active_sessions.get(session_id)
            if runtime is None:
                return False
            if provider is not None and runtime.provider != provider:
                return False
            if (
                attachment_id is not None
                and not runtime.owns_attachment(attachment_id)
            ):
                return False
            if runtime.running:
                raise HTTPException(status_code=409, detail="会话正在运行。")
            active_sessions.pop(session_id, None)
            await runtime.close()
            return True

    @app.delete("/api/active-sessions/{session_id}")
    async def release_active_session(
        session_id: str,
        provider: str | None = None,
        attachment_id: str | None = None,
    ) -> dict[str, bool]:
        return {
            "released": await release_idle_session(
                session_id,
                provider=provider,
                attachment_id=attachment_id,
            )
        }

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        try:
            await release_idle_session(session_id)
            deleted = store.delete_session(session_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if not deleted:
            raise HTTPException(status_code=404, detail="会话不存在。")
        return {"deleted": True}

    def session_workspace(session_id: str | None) -> Workspace:
        """Resolve a stored Session workspace, falling back for new Sessions."""
        if session_id:
            bound = store.workspace_for(session_id)
            if bound:
                try:
                    return Workspace(Path(bound))
                except (OSError, ValueError) as error:
                    raise HTTPException(status_code=400, detail=str(error)) from error
        return workspace

    def signed_image_workspace(session_id: str | None) -> Workspace:
        if session_id is None:
            return workspace
        active = active_sessions.get(session_id)
        if active is not None:
            try:
                return Workspace(Path(active.workspace))
            except (OSError, ValueError) as error:
                raise HTTPException(
                    status_code=404, detail="图片不存在。"
                ) from error
        bound = store.workspace_for(session_id)
        if bound is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        try:
            return Workspace(Path(bound))
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=404, detail="图片不存在。") from error

    def resolve_image_path(
        current_workspace: Workspace, path: str
    ) -> tuple[Path, str]:
        try:
            resolved = current_workspace.resolve_path(path)
            info = probe_image(resolved, max_bytes=None)
        except (UnsupportedImageError, OSError, ValueError) as error:
            raise HTTPException(status_code=404, detail="图片不存在。") from error
        return resolved, info.mime_type

    def sign_image_path(
        current_workspace: Workspace,
        path: str,
        session_id: str | None,
    ) -> tuple[str, int]:
        expires_at = int(time.time()) + IMAGE_URL_TTL_SECONDS
        payload = json.dumps(
            {
                "session_id": session_id,
                "path": path,
                "workspace": hashlib.sha256(
                    str(current_workspace.path).encode("utf-8")
                ).hexdigest(),
                "expires_at": expires_at,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(
            image_url_secret, encoded, hashlib.sha256
        ).digest()
        token = (
            encoded.decode("ascii")
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )
        return f"/api/images/{token}", expires_at

    def decode_image_token(token: str) -> tuple[Workspace, str, int]:
        try:
            encoded, encoded_signature = token.split(".", 1)
            payload_bytes = encoded.encode("ascii")
            signature = base64.urlsafe_b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4)
            )
        except (binascii.Error, UnicodeEncodeError, ValueError) as error:
            raise HTTPException(status_code=404, detail="图片链接无效。") from error
        expected = hmac.new(
            image_url_secret, payload_bytes, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=404, detail="图片链接无效。")
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(
                    encoded + "=" * (-len(encoded) % 4)
                )
            )
        except (binascii.Error, UnicodeDecodeError, ValueError) as error:
            raise HTTPException(status_code=404, detail="图片链接无效。") from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=404, detail="图片链接无效。")
        path = payload.get("path")
        session_id = payload.get("session_id")
        workspace_digest = payload.get("workspace")
        expires_at = payload.get("expires_at")
        if (
            not isinstance(path, str)
            or not path
            or (session_id is not None and not isinstance(session_id, str))
            or not isinstance(workspace_digest, str)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
        ):
            raise HTTPException(status_code=404, detail="图片链接无效。")
        if expires_at <= int(time.time()):
            raise HTTPException(status_code=410, detail="图片链接已过期。")
        current_workspace = signed_image_workspace(session_id)
        current_digest = hashlib.sha256(
            str(current_workspace.path).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(workspace_digest, current_digest):
            raise HTTPException(status_code=404, detail="图片链接无效。")
        return current_workspace, path, expires_at

    @app.get("/api/select-workspace")
    def select_workspace() -> dict[str, str | None]:
        """Open a native folder picker for the local GUI server."""
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(title="选择工作区")
            root.destroy()
        except Exception as error:
            raise HTTPException(status_code=500, detail=f"无法打开文件夹选择器：{error}") from error
        return {"workspace": selected or None}

    @app.post("/api/workspaces/scratch")
    def create_scratch(payload: object = Body(...)) -> dict[str, str]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求必须是对象。")
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise HTTPException(status_code=400, detail="session_id 不能为空。")
        try:
            scratch = create_scratch_workspace(
                load_scratch_workspace_root(settings.agent_path),
                session_id,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"workspace": str(scratch.path)}

    @app.get("/api/workspace")
    def get_workspace(
        path: str = ".",
        cursor: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        current_workspace = session_workspace(session_id)
        try:
            arguments = {"path": path}
            if cursor is not None:
                arguments["cursor"] = cursor
            listing = ListDirectoryTool().execute(
                arguments,
                ToolExecutionContext(
                    workspace=current_workspace,
                    session=Session(),
                ),
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"root": str(current_workspace.path), **listing}

    @app.post("/api/attachments")
    async def upload_attachments(
        files: list[UploadFile] = File(...),
        session_id: str | None = None,
    ) -> dict[str, object]:
        """Persist browser files as workspace-relative attachment paths."""
        current_workspace = session_workspace(session_id)
        attachment_root = (current_workspace.path / ".nosis" / "attachments").resolve()
        attachment_root.mkdir(parents=True, exist_ok=True)
        attachments = []
        for upload in files:
            path = attachment_root / uuid4().hex
            try:
                with path.open("wb") as target:
                    shutil.copyfileobj(upload.file, target)
            except OSError as error:
                path.unlink(missing_ok=True)
                raise HTTPException(status_code=500, detail=str(error)) from error
            filename = Path(upload.filename or "attachment").name
            try:
                info = probe_image(path, max_bytes=None)
            except UnsupportedImageError:
                info = None
            suffix = (
                image_extension(info.mime_type)
                if info is not None
                else Path(filename).suffix
            )
            named = path.with_name(path.name + suffix)
            path.replace(named)
            mime_type = (
                info.mime_type
                if info is not None
                else mimetypes.guess_type(filename)[0]
                or upload.content_type
                or "application/octet-stream"
            )
            attachments.append({
                "type": "image" if info is not None else "file",
                "path": f".nosis/attachments/{named.name}",
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": named.stat().st_size,
            })
        return {"attachments": attachments}

    @app.post("/api/image-url")
    def create_image_url(payload: object = Body(...)) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求必须是对象。")
        path = payload.get("path")
        session_id = payload.get("session_id")
        if not isinstance(path, str) or not path:
            raise HTTPException(status_code=400, detail="path 不能为空。")
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id
        ):
            raise HTTPException(status_code=400, detail="session_id 无效。")
        current_workspace = signed_image_workspace(session_id)
        resolve_image_path(current_workspace, path)
        url, expires_at = sign_image_path(
            current_workspace, path, session_id
        )
        return {"url": url, "expires_at": expires_at}

    @app.get("/api/images/{token}")
    def get_signed_image(token: str) -> FileResponse:
        current_workspace, path, expires_at = decode_image_token(token)
        resolved, mime_type = resolve_image_path(current_workspace, path)
        return FileResponse(
            resolved,
            media_type=mime_type,
            headers={
                "Cache-Control": (
                    "private, max-age="
                    f"{max(0, expires_at - int(time.time()))}"
                )
            },
        )

    @app.get("/api/attachments/{filename}")
    def get_attachment(
        filename: str,
        session_id: str | None = None,
        download_name: str | None = None,
    ) -> FileResponse:
        attachment_root = (session_workspace(session_id).path / ".nosis" / "attachments").resolve()
        path = (attachment_root / filename).resolve()
        try:
            path_for_comparison(path).relative_to(
                path_for_comparison(attachment_root)
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail="附件不存在。") from error
        if not path.is_file():
            raise HTTPException(status_code=404, detail="附件不存在。")
        try:
            info = probe_image(path, max_bytes=None)
        except UnsupportedImageError:
            return FileResponse(
                path,
                media_type="application/octet-stream",
                filename=Path(download_name or filename).name,
            )
        raise HTTPException(status_code=404, detail="请使用签名图片链接。")

    @app.websocket("/api/session")
    async def run_session(websocket: WebSocket) -> None:
        # A page served from elsewhere must not be able to drive the agent.
        origin = websocket.headers.get("origin")
        allowed_origins = {
            f"http://{websocket.headers.get('host')}",
            f"http://{HOST}:5173",
            "http://localhost:5173",
        }
        if origin is not None and origin not in allowed_origins:
            await websocket.close(code=1008)
            return

        await websocket.accept()
        try:
            opening = await websocket.receive_json()
            opening_session_id = opening.get("session_id") if isinstance(opening, dict) else None
            if opening_session_id is not None and not isinstance(opening_session_id, str):
                opening_session_id = None
            requested_workspace = opening.get("workspace") if isinstance(opening, dict) else None
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
                else session_workspace(opening_session_id)
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
            await websocket.send_json(
                {
                    "type": "fatal",
                    "error": {
                        "type": "ProtocolError",
                        "message": str(error),
                        "details": {},
                    },
                }
            )
            await websocket.close()
            return

        attach_only = opening.get("attach_only") is True
        attachment_id = opening.get("attachment_id")
        if not isinstance(attachment_id, str) or not attachment_id:
            await websocket.send_json(
                {
                    "type": "fatal",
                    "error": {
                        "type": "ProtocolError",
                        "message": "'attachment_id' must be a non-empty string",
                        "details": {},
                    },
                }
            )
            await websocket.close()
            return
        takeover = opening.get("takeover") is True
        after_event = opening.get("after_event", 0)
        if not isinstance(after_event, int) or isinstance(after_event, bool) or after_event < 0:
            await websocket.send_json(
                {
                    "type": "fatal",
                    "error": {
                        "type": "ProtocolError",
                        "message": "'after_event' must be a non-negative integer",
                        "details": {},
                    },
                }
            )
            await websocket.close()
            return
        runtime, created = await active_session_for(
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
                attachment_replaced_message(phase=runtime.phase)
            )
            await websocket.close()
            return
        queue, events, state = attachment
        fatal_delivered = False
        try:
            replays_fatal = any(message.get("type") == "fatal" for message in events)
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
                    await _relay(
                        websocket,
                        runtime,
                        queue,
                        attachment_id,
                    )
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
            if not runtime.attachable:
                async with active_session_lock:
                    if active_sessions.get(runtime.session_id) is runtime:
                        active_sessions.pop(runtime.session_id, None)

    if STATIC_PATH.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_PATH, html=True), name="gui")
    return app


def _memory_document_to_dict(
    document: MemoryDocument,
) -> dict[str, list[str]]:
    return {
        "preferences": list(document.preferences),
        "facts": list(document.facts),
        "decisions": list(document.decisions),
    }


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
                if message.get("turn_id") == runtime.turn_id:
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


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    config_directory = default_config_directory()
    parser = argparse.ArgumentParser(prog="nosis-gui")
    parser.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        initialize_config_directory(config_directory)
        workspace = Workspace(args.workspace or Path.cwd())
        app = create_app(
            workspace,
            JsonlSessionStore(config_directory / "sessions"),
            SettingsStore(config_directory),
        )
    except (OSError, ValueError) as error:
        raise SystemExit(f"Failed to start Nosis: {error}") from error

    print(f"Workspace: {workspace.path}")
    print(f"Nosis GUI: http://{HOST}:{PORT}")
    if not STATIC_PATH.is_dir():
        print(
            "The interface is not built. Run 'npm install && npm run build' "
            "in interfaces/gui."
        )
    uvicorn.run(app, host=HOST, port=PORT)

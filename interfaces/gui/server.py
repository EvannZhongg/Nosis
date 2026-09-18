"""HTTP and WebSocket front end for the agent bridge.

The server owns the lifecycle of bridge processes while WebSockets only
attach browsers to them. Agent semantics remain inside the bridge/core.
"""

import argparse
import asyncio
import json
import mimetypes
import os
import signal
import shutil
import subprocess
import sys
from collections import deque
from contextlib import asynccontextmanager
from uuid import uuid4
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agent_core import (
    JsonlSessionStore,
    ListDirectoryTool,
    Session,
    ToolExecutionContext,
    UnsupportedImageError,
    Workspace,
    image_extension,
    plan_snapshot_to_dict,
    probe_image,
)

from ..bridge.config import (
    default_config_directory,
    initialize_config_directory,
    load_model_options,
)
from ..bridge.process import cancel_process
from ..bridge.protocol import attachment_replaced_message, runtime_state_message


STATIC_PATH = Path(__file__).resolve().parent / "static"
HOST = "127.0.0.1"
PORT = 8737
SHUTDOWN_TIMEOUT_SECONDS = 2
EVENT_REPLAY_LIMIT = 512
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
        self.skill_warnings: tuple[str, ...] = ()
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
                if replacing and (not takeover or claimed):
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
                        skill_warnings=self.skill_warnings,
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
                    warnings = message.get("skill_warnings")
                    if isinstance(warnings, list) and warnings:
                        self.skill_warnings = tuple(
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
                    self.skill_warnings = ()
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
                self.running or getattr(self.bridge, "returncode", None) != 0
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
    *,
    models: dict[str, str],
    default_model: str,
) -> FastAPI:
    active_sessions: dict[str, ActiveSession] = {}
    active_session_lock = asyncio.Lock()

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
                if attach_only or current.running or same_configuration:
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
        return {
            "default": default_model,
            "models": [
                {"id": name, "model": model}
                for name, model in models.items()
            ],
        }

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
        return store.list_sessions()

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

    @app.get("/api/workspace")
    def get_workspace(path: str = ".", session_id: str | None = None) -> dict[str, object]:
        current_workspace = session_workspace(session_id)
        try:
            listing = ListDirectoryTool().execute(
                {"path": path},
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
        """Persist browser images as workspace-relative attachment paths."""
        current_workspace = session_workspace(session_id)
        attachment_root = (current_workspace.path / ".nosis" / "attachments").resolve()
        attachment_root.mkdir(parents=True, exist_ok=True)
        attachments = []
        for upload in files:
            # The bytes decide the type, not the media type the browser
            # declared: the same probe used on the way into the model's
            # context is what accepts the file here, and the extension it
            # reports is what the stored copy is named after.
            path = attachment_root / uuid4().hex
            try:
                with path.open("wb") as target:
                    shutil.copyfileobj(upload.file, target)
                # No size limit: a large image is still worth showing, and
                # the 5 MiB ceiling belongs to the model-bound route, which
                # rejects it with a message naming the actual size.
                info = probe_image(path, max_bytes=None)
            except UnsupportedImageError as error:
                path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=415, detail="只能上传图片附件。"
                ) from error
            except OSError as error:
                path.unlink(missing_ok=True)
                raise HTTPException(status_code=500, detail=str(error)) from error
            named = path.with_name(path.name + image_extension(info.mime_type))
            path.replace(named)
            attachments.append({
                "type": "image",
                "path": f".nosis/attachments/{named.name}",
                "mime_type": info.mime_type,
            })
        return {"attachments": attachments}

    @app.get("/api/attachments/{filename}")
    def get_attachment(filename: str, session_id: str | None = None) -> FileResponse:
        attachment_root = (session_workspace(session_id).path / ".nosis" / "attachments").resolve()
        path = (attachment_root / filename).resolve()
        try:
            path.relative_to(attachment_root)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="附件不存在。") from error
        if not path.is_file():
            raise HTTPException(status_code=404, detail="附件不存在。")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])

    @app.get("/api/workspace-image")
    def get_workspace_image(
        path: str,
        session_id: str | None = None,
    ) -> FileResponse:
        """Serve an image the agent read from anywhere in the workspace.

        ``read_image`` may load any image the workspace holds, not only
        an upload, so the transcript needs to render a path that never
        passed through the attachment folder.  Only real images are
        served: the media type is read from the file rather than guessed
        from its name, which also keeps this route from becoming a way
        to fetch arbitrary workspace files.
        """
        workspace = session_workspace(session_id)
        try:
            resolved = workspace.resolve_path(path)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="图片不存在。") from error
        try:
            # No size limit here: the 5 MiB ceiling exists to bound what
            # is sent to a model, and a large image is still viewable.
            info = probe_image(resolved, max_bytes=None)
        except (UnsupportedImageError, OSError) as error:
            # An unreadable file is a missing image to the page, not a
            # server fault: letting OSError escape would answer a locked
            # or unreadable file with a traceback and the host path.
            raise HTTPException(status_code=404, detail="图片不存在。") from error
        return FileResponse(resolved, media_type=info.mime_type)

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
            opening_message = _open_session_message(
                opening,
                current_workspace,
                models,
            )
        except WebSocketDisconnect:
            return
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            await websocket.send_json(
                {
                    "type": "fatal",
                    "error": {
                        "type": "ProtocolError",
                        "message": str(error),
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
                    },
                }
            )
            await websocket.close()
            return
        runtime, created = await active_session_for(
            opening_message,
            current_workspace,
            attach_only=attach_only,
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
                await websocket.send_json(message)
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
        provider_config_path = config_directory / "provider_config.json"
        default_model, models = load_model_options(provider_config_path)
        app = create_app(
            workspace,
            JsonlSessionStore(config_directory / "sessions"),
            models=models,
            default_model=default_model,
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

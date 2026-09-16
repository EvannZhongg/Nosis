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
    probe_image,
)

from ..bridge.config import (
    default_config_directory,
    initialize_default_configs,
    load_model_options,
)
from ..bridge.process import cancel_process
from ..bridge.protocol import attachment_replaced_message, runtime_state_message


STATIC_PATH = Path(__file__).resolve().parent / "static"
HOST = "127.0.0.1"
PORT = 8737
SHUTDOWN_TIMEOUT_SECONDS = 2
# Messages the browser may forward to the bridge verbatim. 'start' is
# excluded: the server builds it so a page cannot point the agent at an
# arbitrary configuration file.
RELAYED_MESSAGE_TYPES = frozenset({"user_turn", "approval_response"})


class BridgeProcess:
    """A bridge child process addressed as a protocol message stream."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._ready = False
        self._turn_running = False

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
                return None
            stripped = line.strip()
            if stripped:
                message = json.loads(stripped)
                message_type = message.get("type")
                if message_type == "ready":
                    self._ready = True
                if message_type in {
                    "turn_completed",
                    "turn_cancelled",
                    "turn_failed",
                }:
                    self._turn_running = False
                return message

    def cancel_turn(self) -> None:
        """Interrupt the running turn, as Esc does in the TUI."""
        if self._turn_running and self._process.returncode is None:
            cancel_process(self._process)

    async def close(self) -> None:
        # A page that goes away mid-turn would otherwise take the running
        # turn down with the process; the interrupt lets the bridge store
        # what the turn already produced before it shuts down.
        if not self._ready and self._process.returncode is None:
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


TERMINAL_MESSAGE_TYPES = frozenset(
    {"turn_completed", "turn_cancelled", "turn_failed", "fatal"}
)


class ActiveRuntime:
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
        self.running = False
        self.approval: dict[str, object] | None = None
        self.done = False
        self._closed = False
        self._terminal_pending = False
        self._attachment_id: str | None = None
        self._subscriber: asyncio.Queue[dict[str, object] | None] | None = None
        self._subscriber_detached: asyncio.Event | None = None
        self._events: list[dict[str, object]] = []
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
                    events = list(self._events[after_event:])
                    state = runtime_state_message(
                        running=self.running,
                        approval=self.approval,
                        provider=self.provider,
                    )
                    if self.done:
                        queue.put_nowait(None)
                    return queue, events, state
                previous = self._subscriber
                detached = self._subscriber_detached
                if replacing:
                    previous.put_nowait(
                        attachment_replaced_message(running=self.running)
                    )
                previous.put_nowait(None)
            if detached is not None:
                await detached.wait()

    async def detach(
        self,
        queue: asyncio.Queue[dict[str, object] | None],
        attachment_id: str,
    ) -> None:
        close_idle = False
        async with self._lock:
            if self._subscriber is queue:
                self._subscriber = None
                close_idle = (
                    self._attachment_id == attachment_id
                    and not self.running
                    and not self.done
                )
                if self._subscriber_detached is not None:
                    self._subscriber_detached.set()
                    self._subscriber_detached = None
        if close_idle:
            await self.close()

    def send(self, message: dict[str, object]) -> None:
        if message.get("type") == "user_turn":
            self.running = True
            self.approval = None
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
        self.bridge.send(message)

    def cancel_turn(self) -> None:
        self.bridge.cancel_turn()

    def owns_attachment(self, attachment_id: str) -> bool:
        return self._attachment_id == attachment_id

    @property
    def attachable(self) -> bool:
        return not self.done or self._terminal_pending

    def mark_terminal_delivered(self) -> None:
        self._terminal_pending = False

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
                elif message_type in TERMINAL_MESSAGE_TYPES:
                    self.running = False
                    self.approval = None
                    self._terminal_pending = True
                async with self._lock:
                    self._events.append(message)
                    if self._subscriber is not None:
                        self._subscriber.put_nowait(message)
                if message_type in TERMINAL_MESSAGE_TYPES:
                    break
        finally:
            self.running = False
            self.approval = None
            self.done = True
            if not self._closed:
                self._closed = True
                await self.bridge.close()
            async with self._lock:
                if self._subscriber is not None:
                    self._subscriber.put_nowait(None)


def create_app(
    workspace: Workspace,
    store: JsonlSessionStore,
    provider_config_path: Path,
    agent_config_path: Path,
    *,
    models: dict[str, str],
    default_model: str,
) -> FastAPI:
    runtimes: dict[str, ActiveRuntime] = {}
    runtime_lock = asyncio.Lock()
    pending_workspaces: dict[str, Path] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await asyncio.gather(
            *(runtime.close() for runtime in tuple(runtimes.values())),
            return_exceptions=True,
        )

    app = FastAPI(
        title="Nosis",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[HOST, "localhost"])

    async def runtime_for(
        start: dict[str, object],
        current_workspace: Workspace,
        *,
        attach_only: bool,
    ) -> tuple[ActiveRuntime | None, bool]:
        session_id = str(start["session_id"])
        async with runtime_lock:
            current = runtimes.get(session_id)
            if current is not None and not current.done:
                return current, False
            if current is not None and attach_only and current.attachable:
                return current, False
            if current is not None:
                runtimes.pop(session_id, None)
            if attach_only:
                return None, False
            bridge = await BridgeProcess.spawn(current_workspace)
            provider = start.get("provider")
            stored = store.load(session_id, recover=False)
            runtime = ActiveRuntime(
                session_id,
                provider if isinstance(provider, str) else None,
                str(current_workspace.path),
                jsonable_encoder(stored.items),
                bridge,
            )
            runtimes[session_id] = runtime
            bridge.send(start)
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

    @app.get("/api/runtimes")
    async def list_runtimes() -> list[dict[str, object]]:
        async with runtime_lock:
            return [
                {
                    "session_id": runtime.session_id,
                    "provider": runtime.provider,
                    "workspace": runtime.workspace,
                    "items": runtime.items,
                    "running": runtime.running,
                }
                for runtime in runtimes.values()
                if runtime.attachable
            ]

    @app.get("/api/sessions")
    def list_sessions() -> list[dict[str, object]]:
        return store.list_sessions()

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> object:
        try:
            runtime = runtimes.get(session_id)
            if runtime is not None and not runtime.done:
                return {
                    "session_id": runtime.session_id,
                    "items": runtime.items,
                    "workspace": runtime.workspace,
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
                }
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, bool]:
        try:
            deleted = store.delete_session(session_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if not deleted:
            raise HTTPException(status_code=404, detail="会话不存在。")
        pending_workspaces.pop(session_id, None)
        return {"deleted": True}

    def session_workspace(session_id: str | None) -> Workspace:
        """Resolve the workspace bound to a session, falling back for new sessions."""
        if session_id:
            bound = store.workspace_for(session_id)
            if bound:
                pending_workspaces.pop(session_id, None)
                try:
                    return Workspace(Path(bound))
                except (OSError, ValueError) as error:
                    raise HTTPException(status_code=400, detail=str(error)) from error
            pending = pending_workspaces.get(session_id)
            if pending is not None:
                return Workspace(pending)
        return workspace

    @app.put("/api/sessions/{session_id}/workspace")
    def update_session_workspace(session_id: str, payload: dict[str, object]) -> dict[str, str]:
        value = payload.get("workspace")
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(status_code=400, detail="workspace 必须是非空路径。")
        try:
            selected = Workspace(Path(value.strip()))
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            if store.has_journal(session_id):
                store.bind_workspace(session_id, selected.path)
            else:
                pending_workspaces[session_id] = selected.path
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"workspace": str(selected.path)}

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
            current_workspace = session_workspace(opening_session_id)
            start = _start_message(
                opening,
                current_workspace,
                provider_config_path,
                agent_config_path,
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
        runtime, created = await runtime_for(
            start,
            current_workspace,
            attach_only=attach_only,
        )
        if runtime is None:
            await websocket.send_json(
                runtime_state_message(
                    running=False,
                    approval=None,
                    provider=None,
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
                attachment_replaced_message(running=runtime.running)
            )
            await websocket.close()
            return
        queue, events, state = attachment
        terminal_delivered = False
        try:
            for message in events:
                await websocket.send_json(message)
                terminal_delivered = message.get("type") in TERMINAL_MESSAGE_TYPES
            if not created:
                await websocket.send_json(state)
            try:
                terminal_delivered = (
                    await _relay(
                        websocket,
                        runtime,
                        queue,
                        attachment_id,
                    )
                    or terminal_delivered
                )
            except asyncio.CancelledError:
                # ASGI servers may cancel the handler when the browser drops
                # the socket. Detaching must not cancel the runtime with it.
                pass
        finally:
            if terminal_delivered:
                runtime.mark_terminal_delivered()
            await asyncio.shield(runtime.detach(queue, attachment_id))
            if not runtime.attachable:
                async with runtime_lock:
                    if runtimes.get(runtime.session_id) is runtime:
                        runtimes.pop(runtime.session_id, None)

    if STATIC_PATH.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_PATH, html=True), name="gui")
    return app


def _start_message(
    opening: object,
    workspace: Workspace,
    provider_config_path: Path,
    agent_config_path: Path,
    models: dict[str, str],
) -> dict[str, object]:
    """Build the bridge's 'start' from the browser's session choice."""
    if not isinstance(opening, dict) or opening.get("type") != "start":
        raise ValueError("first message must be 'start'")

    session_id = opening.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("'session_id' must be a non-empty string")

    provider = opening.get("provider")
    if provider is not None and provider not in models:
        raise ValueError("请选择已配置的模型。")

    return {
        "type": "start",
        "workspace": str(workspace.path),
        "session_id": session_id,
        "provider_config_path": str(provider_config_path),
        "agent_config_path": str(agent_config_path),
        "provider": provider,
    }


async def _relay(
    websocket: WebSocket,
    runtime: ActiveRuntime,
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
                runtime.cancel_turn()
            elif message_type in RELAYED_MESSAGE_TYPES:
                runtime.send(message)

    terminal_delivered = False

    async def bridge_to_browser() -> None:
        nonlocal terminal_delivered
        while True:
            message = await queue.get()
            if message is None:
                return
            await websocket.send_json(message)
            if message.get("type") in TERMINAL_MESSAGE_TYPES:
                terminal_delivered = True

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
    return terminal_delivered


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    config_directory = default_config_directory()
    parser = argparse.ArgumentParser(prog="nosis-gui")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument(
        "--config",
        type=Path,
        default=config_directory / "provider_config.json",
    )
    parser.add_argument(
        "--agent-config",
        type=Path,
        default=config_directory / "agent_config.json",
    )
    args = parser.parse_args(argv)

    try:
        initialize_default_configs(config_directory)
        workspace = Workspace(args.workspace or Path.cwd())
        default_model, models = load_model_options(args.config)
        app = create_app(
            workspace,
            JsonlSessionStore(args.config.expanduser().resolve().parent / "sessions"),
            args.config,
            args.agent_config,
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

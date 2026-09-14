"""HTTP and WebSocket front end for the agent bridge.

The server owns no agent logic: each WebSocket connection spawns a
``python -m interfaces.bridge`` child and relays protocol messages
between it and the browser. Session persistence, shell approval and
cancellation therefore behave exactly as they do in the TUI.
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
    probe_image,
)

from ..bridge.config import (
    default_config_directory,
    initialize_default_configs,
    load_model_options,
)


STATIC_PATH = Path(__file__).resolve().parent / "static"
HOST = "127.0.0.1"
PORT = 8737
SHUTDOWN_TIMEOUT_SECONDS = 2
# A page switching model reconnects while the previous bridge is still
# shutting down; only a genuinely occupied agent should be refused.
HANDOVER_TIMEOUT_SECONDS = 5

# Messages the browser may forward to the bridge verbatim. 'start' is
# excluded: the server builds it so a page cannot point the agent at an
# arbitrary configuration file.
RELAYED_MESSAGE_TYPES = frozenset({"user_turn", "approval_response"})


class BridgeProcess:
    """A bridge child process addressed as a protocol message stream."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

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
        )
        return cls(process)

    def send(self, message: dict[str, object]) -> None:
        stdin = self._process.stdin
        if stdin is None or stdin.is_closing():
            return
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
                return json.loads(stripped)

    def cancel_turn(self) -> None:
        """Interrupt the running turn, as Esc does in the TUI."""
        if self._process.returncode is None:
            # Windows has no SIGINT for a child: CTRL_BREAK is the signal
            # that reaches the bridge, which turns it into KeyboardInterrupt.
            self._process.send_signal(
                signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
            )

    async def close(self) -> None:
        # A page that goes away mid-turn would otherwise take the running
        # turn down with the process; the interrupt lets the bridge store
        # what the turn already produced before it shuts down.
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
        self._process.kill()


def create_app(
    workspace: Workspace,
    store: JsonlSessionStore,
    provider_config_path: Path,
    agent_config_path: Path,
    *,
    models: dict[str, str],
    default_model: str,
) -> FastAPI:
    app = FastAPI(title="Nosis", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[HOST, "localhost"])
    # Serialize connections for the same session. Different sessions have
    # independent bridge processes and may run concurrently.
    session_locks: dict[str, asyncio.Lock] = {}
    pending_workspaces: dict[str, Path] = {}

    @app.get("/api/models")
    def list_models() -> dict[str, object]:
        return {
            "default": default_model,
            "models": [
                {"id": name, "model": model}
                for name, model in models.items()
            ],
        }

    @app.get("/api/sessions")
    def list_sessions() -> list[dict[str, object]]:
        return store.list_sessions()

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> object:
        try:
            return jsonable_encoder(store.load(session_id))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

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
            if store.has_transcript(session_id):
                store.bind_workspace(session_id, selected.path)
            else:
                pending_workspaces[session_id] = selected.path
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"workspace": str(selected.path)}

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
            mime_type = upload.content_type or ""
            suffix_by_type = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }
            if mime_type not in suffix_by_type:
                raise HTTPException(status_code=415, detail="只能上传图片附件。")
            filename = f"{uuid4().hex}{suffix_by_type[mime_type]}"
            path = attachment_root / filename
            try:
                with path.open("wb") as target:
                    shutil.copyfileobj(upload.file, target)
            except OSError as error:
                raise HTTPException(status_code=500, detail=str(error)) from error
            attachments.append({
                "type": "image",
                "path": f".nosis/attachments/{filename}",
                "mime_type": mime_type,
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
        # Read the opening frame before selecting a lock: the session id is
        # the unit of concurrency. A new session has no id yet, so give this
        # connection a private key and let the bridge allocate its id.
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

        session_id = start["session_id"]
        lock_key = session_id if isinstance(session_id, str) and session_id else f"new:{uuid4()}"
        session_lock = session_locks.setdefault(lock_key, asyncio.Lock())
        lock_acquired = False
        try:
            try:
                await asyncio.wait_for(
                    session_lock.acquire(),
                    timeout=HANDOVER_TIMEOUT_SECONDS,
                )
                lock_acquired = True
            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "fatal",
                        "error": {
                            "type": "SessionBusy",
                            "message": "该会话正在另一个页面中执行任务。",
                        },
                    }
                )
                await websocket.close()
                return

            bridge = await BridgeProcess.spawn(current_workspace)
            bridge.send(start)
            try:
                await _relay(websocket, bridge)
            finally:
                await bridge.close()
        finally:
            if lock_acquired:
                session_lock.release()
                if not session_lock.locked():
                    session_locks.pop(lock_key, None)

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
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("'session_id' must be a string or null")

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


async def _relay(websocket: WebSocket, bridge: BridgeProcess) -> None:
    """Pump messages both ways until either side closes."""

    async def browser_to_bridge() -> None:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            message_type = message.get("type")
            if message_type == "cancel":
                bridge.cancel_turn()
            elif message_type in RELAYED_MESSAGE_TYPES:
                bridge.send(message)

    async def bridge_to_browser() -> None:
        while True:
            message = await bridge.read()
            if message is None:
                return
            await websocket.send_json(message)

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

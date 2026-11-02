"""FastAPI application assembly for the GUI.

The application host owns shared sessions independently of this HTTP adapter.
WebSockets attach browsers to its Bridge; execution stays in the bridge/core.
"""

import asyncio
import importlib
import os
import socket
from contextlib import nullcontext
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from agent_core import JsonlSessionStore, Workspace
from agent_runtime.settings import SettingsStore
from interfaces.session_host import ActiveSessionRegistry
from .routes.media import create_media_router
from .routes.schedules import create_schedules_router
from .routes.sessions import create_sessions_router
from .routes.settings import create_settings_router
from .routes.workspace import WorkspaceResolver, create_workspace_router

STATIC_PATH = Path(__file__).resolve().parent / "static"
HOST = "127.0.0.1"
PORT = 8737


def create_app(
    workspace: Workspace,
    store: JsonlSessionStore,
    settings: SettingsStore,
    active_sessions: ActiveSessionRegistry,
) -> FastAPI:
    workspace_resolver = WorkspaceResolver(workspace, store)

    app = FastAPI(
        title="Nosis",
        docs_url=None,
        redoc_url=None,
    )

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[HOST, "localhost"],
    )
    app.include_router(create_settings_router(settings))
    app.include_router(create_schedules_router(settings, store))
    app.include_router(create_workspace_router(workspace_resolver, settings))
    app.include_router(create_media_router(workspace_resolver, active_sessions))
    app.include_router(
        create_sessions_router(
            workspace_resolver,
            store,
            settings,
            active_sessions,
            host=HOST,
        )
    )

    if STATIC_PATH.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_PATH, html=True), name="gui")
    return app


def require_websocket_backend() -> None:
    for module_name in ("websockets", "wsproto"):
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        return
    raise RuntimeError(
        "Nosis HTTP adapter requires a WebSocket backend. "
        'Install with `uv sync --extra server` or `python -m pip install -e ".[server]"`.'
    )


class HostedServer(uvicorn.Server):
    def capture_signals(self):
        # The application host owns process signals for both HTTP and IM.
        return nullcontext()


async def serve_http(
    workspace: Workspace,
    store: JsonlSessionStore,
    settings: SettingsStore,
    active_sessions: ActiveSessionRegistry,
) -> None:
    require_websocket_backend()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST, PORT))
        listener.listen(128)
        app = create_app(workspace, store, settings, active_sessions)
        server = HostedServer(uvicorn.Config(app, host=HOST, port=PORT, lifespan="off"))
        print(f"Nosis GUI: http://{HOST}:{PORT}")
        if not STATIC_PATH.is_dir():
            print("The interface is not built. Run 'npm install && npm run build' in interfaces/gui.")
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.shield(task)
        finally:
            server.should_exit = True
            await task
        if not server.started:
            raise RuntimeError("Nosis HTTP adapter failed to start; see the startup error above")

"""FastAPI application assembly for the GUI.

The server owns the lifecycle of bridge processes while WebSockets only
attach browsers to them. Agent semantics remain inside the bridge/core.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from agent_core import JsonlSessionStore, Workspace
from agent_runtime.settings import SettingsStore

from .active_session import ActiveSessionRegistry
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
) -> FastAPI:
    active_sessions = ActiveSessionRegistry(store)
    workspace_resolver = WorkspaceResolver(workspace, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await active_sessions.close_all()

    app = FastAPI(
        title="Nosis",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
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

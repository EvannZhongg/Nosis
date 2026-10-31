"""FastAPI application assembly for the GUI.

The application host supplies the shared Session registry and lifecycle.
WebSockets attach browsers to its Bridge; execution stays in the bridge/core.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Lifespan

from agent_core import JsonlSessionStore, Workspace
from agent_runtime.settings import SettingsStore
from interfaces.session_host import ActiveSessionRegistry
from .routes.media import create_media_router
from .routes.schedules import create_schedules_router
from .routes.sessions import create_sessions_router
from .routes.settings import create_settings_router
from .routes.workspace import WorkspaceResolver, create_workspace_router

STATIC_PATH = Path(__file__).resolve().parent / "static"


def create_app(
    workspace: Workspace,
    store: JsonlSessionStore,
    settings: SettingsStore,
    active_sessions: ActiveSessionRegistry,
    *,
    host: str,
    lifespan: Lifespan[FastAPI],
) -> FastAPI:
    workspace_resolver = WorkspaceResolver(workspace, store)

    app = FastAPI(
        title="Nosis",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[host, "localhost"],
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
            host=host,
        )
    )

    return app

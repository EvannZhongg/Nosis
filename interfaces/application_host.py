"""Launch or reuse the local host shared by GUI and messaging frontends."""

import argparse
import errno
import importlib
import json
import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from agent_core import JsonlSessionStore, Workspace
from agent_runtime.config import default_config_directory, initialize_config_directory
from agent_runtime.settings import SettingsStore
from interfaces.session_host import ActiveSessionRegistry

if TYPE_CHECKING:
    from fastapi import FastAPI


HOST = "127.0.0.1"
PORT = 8737
log = logging.getLogger(__name__)


def create_app(
    workspace: Workspace,
    store: JsonlSessionStore,
    settings: SettingsStore,
) -> "FastAPI":
    # Keep optional frontend dependencies out of command-line module imports.
    from fastapi.staticfiles import StaticFiles

    from interfaces.gui.server import STATIC_PATH, create_app as create_gui_app
    from interfaces.mobile.service import MobileService

    active_sessions = ActiveSessionRegistry(store, workspace, settings.directory)

    @asynccontextmanager
    async def lifespan(_app: "FastAPI"):
        mobile = None
        try:
            await active_sessions.start()
            try:
                mobile = MobileService(settings.directory, active_sessions, store, workspace)
                await mobile.start()
            except (OSError, ValueError, RuntimeError) as error:
                log.error("Nosis mobile failed to start: %s", error)
                if mobile is not None:
                    await mobile.close()
                    mobile = None
            yield
        finally:
            try:
                if mobile is not None:
                    await mobile.close()
            finally:
                await active_sessions.close_all()

    app = create_gui_app(
        workspace,
        store,
        settings,
        active_sessions,
        host=HOST,
        lifespan=lifespan,
    )

    @app.get("/api/host")
    def host_identity() -> dict[str, str]:
        return {
            "application": "nosis",
            "config_directory": str(settings.directory.resolve()),
            "workspace": str(workspace.path),
        }

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
        "Nosis application host requires a WebSocket backend. "
        'Install with `uv sync --extra gui` or `python -m pip install -e ".[gui]"`.'
    )


def run_application_host(workspace: Path, config_directory: Path) -> None:
    import httpx
    import uvicorn

    from interfaces.gui.server import STATIC_PATH

    require_websocket_backend()
    directory = config_directory.expanduser().resolve()
    initialize_config_directory(directory)
    selected_workspace = Workspace(workspace)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            listener.bind((HOST, PORT))
        except OSError as error:
            if error.errno not in {errno.EADDRINUSE, errno.EACCES}:
                raise
            # The owning process reserves the listening socket before starting
            # Bridge / IM. This request also waits for an in-progress startup.
            try:
                response = httpx.get(f"http://{HOST}:{PORT}/api/host", timeout=30, trust_env=False)
                response.raise_for_status()
            except httpx.HTTPError as request_error:
                raise RuntimeError(
                    f"Cannot attach to the application listening on {HOST}:{PORT}: {request_error}"
                ) from request_error
            try:
                identity = response.json()
            except json.JSONDecodeError as decode_error:
                raise RuntimeError(
                    f"Port {PORT} belongs to another application or Nosis config directory"
                ) from decode_error
            if (
                not isinstance(identity, dict)
                or identity.get("application") != "nosis"
                or not isinstance(identity.get("config_directory"), str)
                or not isinstance(identity.get("workspace"), str)
                or os.path.normcase(identity["config_directory"])
                != os.path.normcase(str(directory))
            ):
                raise RuntimeError(
                    f"Port {PORT} belongs to another application or Nosis config directory"
                ) from error
            print(f"Workspace: {identity['workspace']}")
            print(f"Nosis GUI: http://{HOST}:{PORT}")
            print("Using the running Nosis host (GUI and IM share its sessions).")
            return
        listener.listen(128)
        app = create_app(
            selected_workspace,
            JsonlSessionStore(directory / "sessions"),
            SettingsStore(directory),
        )
        print(f"Workspace: {selected_workspace.path}")
        print(f"Nosis GUI: http://{HOST}:{PORT}")
        if not STATIC_PATH.is_dir():
            print(
                "The interface is not built. Run 'npm install && npm run build' in interfaces/gui."
            )
        server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT))
        server.run(sockets=[listener])
        if not server.started:
            raise RuntimeError(
                "Nosis application host failed to start; see the startup error above"
            )
    finally:
        listener.close()


def main(argv: list[str] | None = None, *, prog: str) -> None:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--config-directory", type=Path, default=default_config_directory())
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        run_application_host(args.workspace, args.config_directory)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        raise SystemExit(f"Failed to start Nosis: {error}") from error

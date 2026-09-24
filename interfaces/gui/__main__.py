"""Command-line entry point for the GUI server."""

import argparse
from pathlib import Path

from agent_core import JsonlSessionStore, Workspace
from agent_runtime.config import (
    default_config_directory,
    initialize_config_directory,
)
from agent_runtime.settings import SettingsStore

from .server import HOST, PORT, STATIC_PATH, create_app


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


if __name__ == "__main__":
    main()

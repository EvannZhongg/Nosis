"""Console script that hands the terminal to the Ink UI.

The UI runs as a child process that this script waits for.  ``execvp``
cannot be used: on Windows it starts a new process and terminates this
one, so the shell regains its prompt while the UI is still running and
both then compete for console input.
"""

import argparse
import os
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from agent_runtime.config import (
    default_config_directory,
    initialize_config_directory,
    load_scratch_workspace_root,
)
from .bridge.managed_workspaces import create_scratch_workspace


def ui_bundle_path() -> Path:
    return Path(str(files("interfaces").joinpath("tui/dist/app.js")))


def main() -> None:
    parser = argparse.ArgumentParser(prog="nosis", allow_abbrev=False)
    parser.add_argument("--workspace", type=Path, help="Workspace directory (default: current directory)")
    parser.add_argument("--config-directory", type=Path, default=default_config_directory(),
                        help="Use this Nosis configuration directory")
    parser.add_argument("--temporary", action="store_true",
                        help="Use a persistent Nosis-managed scratch workspace")
    options = parser.parse_args()
    if options.temporary and options.workspace is not None:
        parser.error("--temporary cannot be used with --workspace")

    node = shutil.which("node")
    if node is None:
        raise SystemExit(
            "Nosis requires Node.js 22 or newer on PATH for its terminal "
            "interface. Install it from https://nodejs.org and try again."
        )

    bundle = ui_bundle_path()
    if not bundle.is_file():
        raise SystemExit(
            f"Nosis terminal interface is not built: {bundle} is missing. "
            "Run 'npm install && npm run build' in interfaces/tui."
        )

    config_directory = options.config_directory.expanduser().resolve()
    workspace = (options.workspace or Path.cwd()).resolve()
    if options.temporary:
        try:
            if not (config_directory / "agent_config.json").is_file():
                initialize_config_directory(config_directory)
            workspace = create_scratch_workspace(
                load_scratch_workspace_root(
                    config_directory / "agent_config.json"
                )
            ).path
        except (OSError, ValueError) as error:
            raise SystemExit(
                f"Failed to create scratch workspace: {error}"
            ) from error

    # The bridge must run in the interpreter that owns agent_core.
    environment = {
        **os.environ, "NOSIS_PYTHON": sys.executable,
        "NOSIS_CONFIG_DIRECTORY": str(config_directory),
    }
    raise SystemExit(subprocess.call([node, str(bundle), str(workspace)], env=environment))

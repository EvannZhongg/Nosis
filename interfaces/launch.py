"""Console script that hands the terminal to the Ink UI.

The UI runs as a child process that this script waits for.  ``execvp``
cannot be used: on Windows it starts a new process and terminates this
one, so the shell regains its prompt while the UI is still running and
both then compete for console input.
"""

import os
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from .bridge.config import default_config_directory, initialize_config_directory


def ui_bundle_path() -> Path:
    return Path(str(files("interfaces").joinpath("tui/dist/app.js")))


def main() -> None:
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

    initialize_config_directory(default_config_directory())

    # The bridge must run in the interpreter that owns agent_core.
    os.environ["NOSIS_PYTHON"] = sys.executable
    raise SystemExit(subprocess.call([node, str(bundle), *sys.argv[1:]]))

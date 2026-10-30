"""Process entry point for the agent bridge.

Claims a private protocol channel before any provider import, because
LiteLLM writes diagnostics straight to stdout on failure and would
otherwise corrupt the newline-delimited JSON stream.
"""

import json
import argparse
import os
import signal
import sys
from typing import TextIO
from pathlib import Path


def _interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def _claim_stdout() -> TextIO:
    protocol_fd = os.dup(1)
    os.dup2(2, 1)  # print()/library banners now land on stderr
    return os.fdopen(protocol_fd, "w", encoding="utf-8", buffering=1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nosis-bridge")
    parser.add_argument("--config-directory", type=Path, default=None)
    args = parser.parse_args(argv)
    # Node writes the protocol as UTF-8.  On Windows Python may inherit a
    # locale-specific (for example GBK) stdin encoding, which corrupts
    # non-ASCII paths before JSON decoding.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(signal, "SIGBREAK"):
        # A bridge that has not reached the protocol loop cannot receive a
        # shutdown message. Its owner uses CTRL_BREAK to terminate startup.
        signal.signal(signal.SIGBREAK, _interrupt)
    protocol_out = _claim_stdout()
    bridge = None
    try:
        from agent_runtime.config import default_config_directory, initialize_config_directory

        directory = (args.config_directory or default_config_directory()).resolve()
        initialize_config_directory(directory)
        from .server import BridgeServer

        bridge = BridgeServer(sys.stdin, protocol_out, config_directory=directory)
        bridge.serve()
    except KeyboardInterrupt:
        return
    except SystemExit:
        raise
    except BaseException as error:
        message = {
            "type": "fatal",
            "error": {
                "type": type(error).__name__,
                "message": str(error),
                "details": {},
            },
        }
        if bridge is not None:
            bridge.emit(**message)
        else:
            protocol_out.write(json.dumps(message, ensure_ascii=False) + "\n")
        raise SystemExit(1)
    finally:
        if bridge is not None:
            bridge.close()


if __name__ == "__main__":
    main()

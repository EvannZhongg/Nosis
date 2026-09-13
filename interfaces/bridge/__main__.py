"""Process entry point for the agent bridge.

Claims a private protocol channel before any provider import, because
LiteLLM writes diagnostics straight to stdout on failure and would
otherwise corrupt the newline-delimited JSON stream.
"""

import os
import signal
import sys
from typing import TextIO


def _interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def _claim_stdout() -> TextIO:
    protocol_fd = os.dup(1)
    os.dup2(2, 1)  # print()/library banners now land on stderr
    return os.fdopen(protocol_fd, "w", encoding="utf-8", buffering=1)


def main() -> None:
    # Node writes the protocol as UTF-8.  On Windows Python may inherit a
    # locale-specific (for example GBK) stdin encoding, which corrupts
    # non-ASCII paths before JSON decoding.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(signal, "SIGBREAK"):
        # A Windows child has no usable SIGINT, so the GUI cancels a turn
        # with CTRL_BREAK: unwind the agent loop exactly as Ctrl+C would.
        signal.signal(signal.SIGBREAK, _interrupt)
    protocol_out = _claim_stdout()

    from .bridge import Bridge

    bridge = Bridge(sys.stdin, protocol_out)
    try:
        first = bridge.read_message()
        if first is None:
            return
        if first["type"] != "start":
            bridge.emit(
                "fatal",
                error={
                    "type": "ProtocolError",
                    "message": "first message must be 'start'",
                },
            )
            raise SystemExit(1)
        bridge.start(first)
    except SystemExit:
        raise
    except BaseException as error:
        bridge.emit(
            "fatal",
            error={"type": type(error).__name__, "message": str(error)},
        )
        raise SystemExit(1)

    bridge.serve()


if __name__ == "__main__":
    main()

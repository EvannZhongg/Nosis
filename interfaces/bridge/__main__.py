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
        # A bridge that has not reached the protocol loop cannot receive a
        # shutdown message. Its owner uses CTRL_BREAK to terminate startup.
        signal.signal(signal.SIGBREAK, _interrupt)
    protocol_out = _claim_stdout()
    bridge = None
    try:
        from .bridge import Bridge

        bridge = Bridge(sys.stdin, protocol_out)
        bridge.serve()
    except KeyboardInterrupt:
        return
    except SystemExit:
        raise
    except BaseException as error:
        if bridge is not None:
            bridge.emit(
                "fatal",
                error={
                    "type": type(error).__name__,
                    "message": str(error),
                    "details": {},
                },
            )
        raise SystemExit(1)
    finally:
        if bridge is not None:
            bridge.close()


if __name__ == "__main__":
    main()

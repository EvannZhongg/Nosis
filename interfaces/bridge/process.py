"""Process-group helpers shared by bridge front ends."""

import os
import signal
from typing import Protocol


class _Process(Protocol):
    returncode: int | None
    pid: int

    def send_signal(self, signal_number: int) -> None: ...


def cancel_process(process: _Process) -> None:
    """Deliver a soft interrupt to a bridge process running in its own group."""
    if process.returncode is not None:
        return
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        # The bridge is the leader of a private session/process group; signal
        # the group so provider/tool descendants unwind with it.
        pid = getattr(process, "pid", None)
        if pid is None:
            process.send_signal(signal.SIGINT)
        else:
            try:
                os.kill(-pid, signal.SIGINT)
            except ProcessLookupError:
                pass

"""Protocol checkpoints shared by stream publishers and attachment hosts."""

def runtime_is_active(phase: str) -> bool:
    return phase in {"starting", "running", "waiting_approval", "waiting_user"}


class EventStream:
    """Assign cursors and bind transcript checkpoints to emitted events.

    A checkpoint replaces the transcript through its cursor. Later events
    must be replayed, regardless of the execution phase. Fatal errors never
    advance that checkpoint because they are not conversation items.
    """

    def __init__(self) -> None:
        self.sequence = 0

    def publish(
        self,
        message: dict[str, object],
        *,
        state: dict[str, object] | None = None,
        items: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        self.sequence += 1
        result = {
            **message,
            "event_sequence": self.sequence,
            "resume_after": self.sequence - 1 if message["type"] == "fatal" else self.sequence,
        }
        if state is not None and message["type"] != "runtime_state":
            result["runtime"] = state
        if items is not None:
            result["transcript"] = {"items": items, "event_sequence": self.sequence}
        return result

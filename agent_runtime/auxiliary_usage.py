"""Session-local usage records for background model calls."""

import json
import os
from pathlib import Path
from threading import Lock

from agent_core.llm import AuxiliaryModelCall
from agent_core.usage import usage_to_dict


class AuxiliaryUsageLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()

    def append(
        self,
        model: str,
        source: str,
        role: str | None,
        subagent_session_id: str | None,
        call: AuxiliaryModelCall,
    ) -> None:
        record = {
            "timestamp_utc": call.started_at_utc.isoformat().replace("+00:00", "Z"),
            "source": source,
            "role": role,
            "subagent_session_id": subagent_session_id,
            "task": call.task,
            "scope": call.scope,
            "model": model,
            "attempt": call.attempt,
            "status": call.status,
            "duration_ms": call.duration_ms,
            "usage": usage_to_dict(call.usage),
            "error": call.error,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
                file.flush()
                os.fsync(file.fileno())

"""Persist frontend selections separately from the Session journal."""

import json
from pathlib import Path

from agent_core.atomic import atomic_write_text


class Bindings:
    def __init__(self, path: Path) -> None:
        self.path = path
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"bindings": {}, "received": []}
        if not isinstance(document, dict) or not isinstance(document.get("bindings"), dict) or not isinstance(document.get("received"), list):
            raise ValueError("im_bindings.json must contain an object")
        self.entries = document["bindings"]
        self.received = document["received"]

    def get(self, key: str) -> dict[str, str]:
        return dict(self.entries.get(key, {}))

    def save(self, key: str, workspace: str, session_id: str | None) -> None:
        self.entries[key] = {"workspace": workspace, "session_id": session_id}
        self._write()

    def accept(self, channel_id: str, message_id: str) -> bool:
        identity = [channel_id, message_id]
        if identity in self.received:
            return False
        self.received = [*self.received[-999:], identity]
        self._write()
        return True

    def _write(self) -> None:
        atomic_write_text(self.path, json.dumps({"bindings": self.entries, "received": self.received}, ensure_ascii=False, indent=2) + "\n")

"""Persist mobile bindings and inbound processing receipts."""

import json
from pathlib import Path
from uuid import uuid4

from agent_core.atomic import atomic_write_text


class Bindings:
    def __init__(self, path: Path) -> None:
        self.path = path
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"bindings": {}, "messages": {}}
        if not isinstance(document, dict) or not isinstance(document.get("bindings"), dict) or not isinstance(document.get("messages"), dict):
            raise ValueError("im_bindings.json must contain an object")
        self.entries = document["bindings"]
        self.messages = document["messages"]

    def get(self, key: str) -> dict[str, str]:
        return dict(self.entries.get(key, {}))

    def save(self, key: str, workspace: str, session_id: str | None) -> None:
        self.entries[key] = {"workspace": workspace, "session_id": session_id}
        self._write()

    def receive(self, channel_id: str, chat_id: str, user_id: str, text: str,
                message_id: str, attachments) -> dict:
        identity = json.dumps([channel_id, message_id])
        if identity in self.messages:
            return self.messages[identity]
        record = {"channel_id": channel_id, "chat_id": chat_id, "user_id": user_id,
                  "text": text, "message_id": message_id, "attachments": list(attachments),
                  "status": "received", "turn": None, "control": None}
        self.messages[identity] = record
        try:
            self._write()
        except BaseException:
            del self.messages[identity]
            raise
        return record

    def prepare_turn(self, record: dict, workspace: str, session_id: str) -> dict:
        if record["turn"] is None:
            record["turn"] = {"workspace": workspace, "session_id": session_id,
                              "turn_id": str(uuid4())}
            try:
                self._write()
            except BaseException:
                record["turn"] = None
                raise
        return record["turn"]

    def mark(self, record: dict, status: str) -> None:
        record["status"] = status
        self._write()

    def prepare_control(self, record: dict, session_id: str, message: dict) -> None:
        record["control"] = {"session_id": session_id, "message": message}
        self._write()

    def finish_turn(self, session_id: str, turn_id: str) -> None:
        for record in self.messages.values():
            turn = record["turn"]
            if turn and turn["session_id"] == session_id and turn["turn_id"] == turn_id:
                self.mark(record, "completed")
                return

    def _write(self) -> None:
        completed = [key for key, record in self.messages.items() if record["status"] == "completed"]
        for key in completed[:-1000]:
            del self.messages[key]
        atomic_write_text(self.path, json.dumps({"bindings": self.entries, "messages": self.messages}, ensure_ascii=False, indent=2) + "\n")

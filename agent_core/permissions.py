"""Runtime permission state and Tool authorization policy."""

import json
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .session import Session
    from .tools.base import ToolCall, ToolPolicy


class PermissionPreset(StrEnum):
    ASK_FOR_APPROVAL = "ask_for_approval"
    FULL_ACCESS = "full_access"


class PermissionController:
    """Own the mutable permission preset for one Runtime and Session."""

    def __init__(
        self,
        session: "Session",
        approval_policy: "ToolPolicy",
        persist_preset: Callable[[PermissionPreset], None] | None = None,
    ) -> None:
        self._session = session
        self._approval_policy = approval_policy
        self._persist_preset = persist_preset
        self._lock = Lock()

    @property
    def preset(self) -> PermissionPreset:
        with self._lock:
            return self._session.permission_preset

    def set_preset(self, preset: PermissionPreset) -> None:
        with self._lock:
            if preset is self._session.permission_preset:
                return
            self._session.set_permission_preset(preset)
            if self._persist_preset is not None:
                self._persist_preset(preset)

    def authorize(self, call: "ToolCall") -> None:
        with self._lock:
            preset = self._session.permission_preset
        if preset is PermissionPreset.ASK_FOR_APPROVAL:
            try:
                consulted = self._approval_policy.authorize(call)
            except PermissionError:
                self._record_approval(call, False)
                raise
            if consulted:
                self._record_approval(call, True)

    def _record_approval(self, call: "ToolCall", approved: bool) -> None:
        self._session.record_user_interaction(
            f"Tool approval: {call.name}"
            f"({json.dumps(call.arguments, ensure_ascii=False)})\n"
            f"User response: {'approved' if approved else 'denied'}",
            "approval_response",
        )

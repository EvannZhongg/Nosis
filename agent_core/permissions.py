"""Runtime permission state and Tool authorization policy."""

import json
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING, Callable

from .execution import ExecutionScope

if TYPE_CHECKING:
    from .session import Session
    from .tools.base import ToolCall, ToolPolicy
    from .tools.context import ToolExecutionContext


class PermissionPreset(StrEnum):
    ASK_FOR_APPROVAL = "ask_for_approval"
    WORKSPACE_ACCESS = "workspace_access"
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

    def authorize(
        self,
        call: "ToolCall",
        context: "ToolExecutionContext",
    ) -> None:
        with self._lock:
            preset = self._session.permission_preset
        scope = self._approval_policy.execution_scope(call, context)
        if scope is None:
            return
        if preset is PermissionPreset.FULL_ACCESS:
            return
        if (
            preset is PermissionPreset.WORKSPACE_ACCESS
            and scope is ExecutionScope.WORKSPACE
        ):
            return
        try:
            consulted = self._approval_policy.authorize(call, context)
        except PermissionError:
            self._record_approval(call, context, False)
            raise
        if consulted:
            self._record_approval(call, context, True)

    def _record_approval(
        self,
        call: "ToolCall",
        context: "ToolExecutionContext",
        approved: bool,
    ) -> None:
        context.session.record_user_interaction(
            f"Tool approval: {call.name}"
            f"({json.dumps(call.arguments, ensure_ascii=False)})\n"
            f"User response: {'approved' if approved else 'denied'}",
            "approval_response",
        )

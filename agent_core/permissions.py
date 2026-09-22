"""Runtime permission state and Tool authorization policy."""

import json
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING, Callable

from .execution import (
    APPROVAL_REQUIRED_AUTHORITY,
    FULL_ACCESS_AUTHORITY,
    WORKSPACE_ACCESS_AUTHORITY,
    ExecutionAuthority,
    ExecutionScope,
)

if TYPE_CHECKING:
    from .session import Session
    from .tools.base import ToolCall, ToolPolicy
    from .tools.context import ToolExecutionContext


class PermissionPreset(StrEnum):
    ASK_FOR_APPROVAL = "ask_for_approval"
    WORKSPACE_ACCESS = "workspace_access"
    FULL_ACCESS = "full_access"

    @property
    def authority(self) -> ExecutionAuthority:
        return {
            PermissionPreset.ASK_FOR_APPROVAL: APPROVAL_REQUIRED_AUTHORITY,
            PermissionPreset.WORKSPACE_ACCESS: WORKSPACE_ACCESS_AUTHORITY,
            PermissionPreset.FULL_ACCESS: FULL_ACCESS_AUTHORITY,
        }[self]

    @property
    def default_scope(self) -> ExecutionScope:
        return self.authority.default_scope

    @classmethod
    def from_authority(cls, authority: ExecutionAuthority) -> "PermissionPreset":
        for preset in cls:
            if preset.authority == authority:
                return preset
        raise ValueError("execution authority has no permission preset")


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

    @property
    def authority(self) -> ExecutionAuthority:
        return self.preset.authority

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
        route = context.execution
        if route is None or route.scope is None:
            return
        if route.authority.allows_unattended(route.scope):
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

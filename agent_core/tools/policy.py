from typing import Callable

from .base import ToolCall, ToolPolicy
from .context import ToolExecutionContext


class ShellApprovalPolicy:
    def __init__(
        self,
        request_permission: Callable[[str], bool],
    ) -> None:
        self._request_permission = request_permission

    def authorize(
        self, call: ToolCall, context: ToolExecutionContext
    ) -> bool:
        if call.name != "shell":
            return False

        command = call.arguments.get("command")
        if not isinstance(command, str) or not command:
            return False
        if not self._request_permission(command):
            raise PermissionError("shell command was not approved")
        return True


class CompositeToolPolicy:
    def __init__(self, *policies: ToolPolicy) -> None:
        self._policies = tuple(policies)

    def authorize(
        self, call: ToolCall, context: ToolExecutionContext
    ) -> bool:
        consulted = False
        for policy in self._policies:
            consulted = bool(policy.authorize(call, context)) or consulted
        return consulted


class McpApprovalPolicy:
    def __init__(
        self,
        request_permission: Callable[[ToolCall], bool],
        requires_approval: Callable[[str], bool],
    ) -> None:
        self._request_permission = request_permission
        self._requires_approval = requires_approval

    def authorize(
        self, call: ToolCall, context: ToolExecutionContext
    ) -> bool:
        if not self._requires_approval(call.name):
            return False
        if not self._request_permission(call):
            raise PermissionError("MCP tool call was not approved")
        return True

from typing import Callable

from .base import ToolCall, ToolPolicy


class ShellApprovalPolicy:
    def __init__(
        self,
        request_permission: Callable[[str], bool],
    ) -> None:
        self._request_permission = request_permission

    def authorize(self, call: ToolCall) -> None:
        if call.name != "shell":
            return

        command = call.arguments.get("command")
        if not isinstance(command, str) or not command:
            return
        if not self._request_permission(command):
            raise PermissionError("shell command was not approved")


class CompositeToolPolicy:
    def __init__(self, *policies: ToolPolicy) -> None:
        self._policies = tuple(policies)

    def authorize(self, call: ToolCall) -> None:
        for policy in self._policies:
            policy.authorize(call)


class McpApprovalPolicy:
    def __init__(
        self,
        request_permission: Callable[[ToolCall], bool],
        requires_approval: Callable[[str], bool],
    ) -> None:
        self._request_permission = request_permission
        self._requires_approval = requires_approval

    def authorize(self, call: ToolCall) -> None:
        if not self._requires_approval(call.name):
            return
        if not self._request_permission(call):
            raise PermissionError("MCP tool call was not approved")

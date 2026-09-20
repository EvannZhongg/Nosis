from typing import Callable

from ..execution import ExecutionScope, execution_scope_from_arguments
from .base import ToolCall, ToolPolicy
from .context import ToolExecutionContext


class ShellApprovalPolicy:
    def __init__(
        self,
        request_permission: Callable[[str], bool],
    ) -> None:
        self._request_permission = request_permission

    def execution_scope(
        self, call: ToolCall, context: ToolExecutionContext
    ) -> ExecutionScope | None:
        if call.name != "shell":
            return None
        return execution_scope_from_arguments(call.arguments)

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

    def execution_scope(
        self, call: ToolCall, context: ToolExecutionContext
    ) -> ExecutionScope | None:
        scopes = tuple(
            scope
            for policy in self._policies
            if (scope := policy.execution_scope(call, context)) is not None
        )
        if ExecutionScope.HOST in scopes:
            return ExecutionScope.HOST
        if ExecutionScope.WORKSPACE in scopes:
            return ExecutionScope.WORKSPACE
        return None

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

    def execution_scope(
        self, call: ToolCall, context: ToolExecutionContext
    ) -> ExecutionScope | None:
        if not self._requires_approval(call.name):
            return None
        return ExecutionScope.HOST

    def authorize(
        self, call: ToolCall, context: ToolExecutionContext
    ) -> bool:
        if not self._requires_approval(call.name):
            return False
        if not self._request_permission(call):
            raise PermissionError("MCP tool call was not approved")
        return True

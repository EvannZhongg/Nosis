from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .authority import (
    WORKSPACE_ACCESS_AUTHORITY,
    ExecutionAuthority,
    ExecutionScope,
)
from .executor import CommandExecutor

if TYPE_CHECKING:
    from ..tools.base import ToolCall


@dataclass(frozen=True)
class ResolvedExecution:
    scope: ExecutionScope | None
    authority: ExecutionAuthority
    executor: CommandExecutor | None = None


AuthoritySource = ExecutionAuthority | Callable[[], ExecutionAuthority]


class ExecutionRouter:
    """Resolve an invocation's authority, scope, and executor once."""

    def __init__(
        self,
        workspace_executor: CommandExecutor,
        host_executor: CommandExecutor,
        *,
        authority: AuthoritySource = WORKSPACE_ACCESS_AUTHORITY,
        host_tool: Callable[[str], bool] | None = None,
    ) -> None:
        self._workspace_executor = workspace_executor
        self._host_executor = host_executor
        self._authority = authority
        self._host_tool = host_tool or (lambda name: False)

    @property
    def authority(self) -> ExecutionAuthority:
        source = self._authority
        return source() if callable(source) else source

    @property
    def workspace_policy(self) -> object | None:
        return getattr(self._workspace_executor, "policy", None)

    def resolve(self, call: "ToolCall") -> ResolvedExecution:
        authority = self.authority
        if call.name == "shell":
            scope = self._shell_scope(call.arguments, authority)
            return ResolvedExecution(
                scope=scope,
                authority=authority,
                executor=(
                    self._workspace_executor
                    if scope is ExecutionScope.WORKSPACE
                    else self._host_executor
                ),
            )
        scope = ExecutionScope.HOST if self._host_tool(call.name) else None
        if scope is not None and not authority.allows(scope):
            raise PermissionError(
                f"execution scope '{scope.value}' exceeds authority"
            )
        return ResolvedExecution(scope=scope, authority=authority)

    def close(self) -> None:
        try:
            self._workspace_executor.close()
        finally:
            self._host_executor.close()

    @staticmethod
    def _shell_scope(
        arguments: dict[str, object],
        authority: ExecutionAuthority,
    ) -> ExecutionScope:
        value = arguments.get("scope", authority.default_scope.value)
        if not isinstance(value, str):
            raise ValueError("execution scope must be 'workspace' or 'host'")
        try:
            scope = ExecutionScope(value)
        except ValueError as error:
            raise ValueError(
                "execution scope must be 'workspace' or 'host'"
            ) from error
        if not authority.allows(scope):
            raise PermissionError(
                f"execution scope '{scope.value}' exceeds authority"
            )
        return scope

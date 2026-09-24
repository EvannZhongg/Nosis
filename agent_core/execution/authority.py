from dataclasses import dataclass
from enum import StrEnum


class ExecutionScope(StrEnum):
    WORKSPACE = "workspace"
    HOST = "host"


_SCOPE_RANK = {
    ExecutionScope.WORKSPACE: 0,
    ExecutionScope.HOST: 1,
}


@dataclass(frozen=True)
class ExecutionAuthority:
    """The execution boundary an Agent may request and use unattended."""

    default_scope: ExecutionScope
    maximum_scope: ExecutionScope
    unattended_scope: ExecutionScope | None

    def __post_init__(self) -> None:
        if not self.allows(self.default_scope):
            raise ValueError("default execution scope exceeds authority")
        if (
            self.unattended_scope is not None
            and not self.allows(self.unattended_scope)
        ):
            raise ValueError("unattended execution scope exceeds authority")

    def allows(self, scope: ExecutionScope) -> bool:
        return _SCOPE_RANK[scope] <= _SCOPE_RANK[self.maximum_scope]

    def allows_unattended(self, scope: ExecutionScope) -> bool:
        return (
            self.unattended_scope is not None
            and _SCOPE_RANK[scope] <= _SCOPE_RANK[self.unattended_scope]
        )

    @property
    def scopes(self) -> tuple[ExecutionScope, ...]:
        return tuple(scope for scope in ExecutionScope if self.allows(scope))

    def intersect(self, other: "ExecutionAuthority") -> "ExecutionAuthority":
        maximum_scope = min(
            self.maximum_scope,
            other.maximum_scope,
            key=_SCOPE_RANK.__getitem__,
        )
        default_scope = min(
            self.default_scope,
            other.default_scope,
            maximum_scope,
            key=_SCOPE_RANK.__getitem__,
        )
        unattended = tuple(
            scope
            for scope in (self.unattended_scope, other.unattended_scope)
            if scope is not None
        )
        unattended_scope = (
            min((*unattended, maximum_scope), key=_SCOPE_RANK.__getitem__)
            if len(unattended) == 2
            else None
        )
        return ExecutionAuthority(
            default_scope=default_scope,
            maximum_scope=maximum_scope,
            unattended_scope=unattended_scope,
        )


APPROVAL_REQUIRED_AUTHORITY = ExecutionAuthority(
    default_scope=ExecutionScope.WORKSPACE,
    maximum_scope=ExecutionScope.HOST,
    unattended_scope=None,
)
WORKSPACE_ACCESS_AUTHORITY = ExecutionAuthority(
    default_scope=ExecutionScope.WORKSPACE,
    maximum_scope=ExecutionScope.HOST,
    unattended_scope=ExecutionScope.WORKSPACE,
)
WORKSPACE_ONLY_AUTHORITY = ExecutionAuthority(
    default_scope=ExecutionScope.WORKSPACE,
    maximum_scope=ExecutionScope.WORKSPACE,
    unattended_scope=ExecutionScope.WORKSPACE,
)
FULL_ACCESS_AUTHORITY = ExecutionAuthority(
    default_scope=ExecutionScope.HOST,
    maximum_scope=ExecutionScope.HOST,
    unattended_scope=ExecutionScope.HOST,
)

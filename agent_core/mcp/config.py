import os
import re
from dataclasses import dataclass, field
from typing import Literal


McpTransport = Literal["stdio", "streamable_http"]

_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_COMMON_FIELDS = {
    "enabled",
    "transport",
    "tools",
    "startup_timeout_seconds",
    "call_timeout_seconds",
}
_STDIO_FIELDS = _COMMON_FIELDS | {"command", "args", "cwd", "env"}
_HTTP_FIELDS = _COMMON_FIELDS | {"url", "headers"}


@dataclass(frozen=True)
class McpToolConfig:
    enabled: frozenset[str] | None = None
    require_approval: frozenset[str] | None = None


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: McpTransport
    enabled: bool = True
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    tools: McpToolConfig = field(default_factory=McpToolConfig)
    startup_timeout_seconds: int = 15
    call_timeout_seconds: int = 60


@dataclass(frozen=True)
class McpConfig:
    enabled: bool = False
    servers: tuple[McpServerConfig, ...] = ()


def load_mcp_config(value: object) -> McpConfig:
    if value is None:
        return McpConfig()
    if not isinstance(value, dict):
        raise ValueError("config field 'mcp' must be an object")
    _reject_unknown(value, {"enabled", "servers"}, "mcp")

    enabled = _boolean(value, "enabled", "mcp", False)
    servers_value = value.get("servers", {})
    if not isinstance(servers_value, dict):
        raise ValueError("config field 'mcp.servers' must be an object")

    servers = tuple(
        _load_server(name, server)
        for name, server in servers_value.items()
    )
    return McpConfig(enabled=enabled, servers=servers)


def _load_server(name: object, value: object) -> McpServerConfig:
    if not isinstance(name, str) or not _SERVER_NAME.fullmatch(name):
        raise ValueError(
            "MCP server names may contain only letters, numbers, '_' and '-'"
        )
    path = f"mcp.servers.{name}"
    if not isinstance(value, dict):
        raise ValueError(f"config field '{path}' must be an object")

    transport = _transport(value, path)
    allowed_fields = (
        _STDIO_FIELDS if transport == "stdio" else _HTTP_FIELDS
    )
    _reject_unknown(value, allowed_fields, path)

    enabled = _boolean(value, "enabled", path, True)
    tools = _tool_config(value.get("tools"), path)
    startup_timeout = _positive_integer(
        value, "startup_timeout_seconds", path, 15
    )
    call_timeout = _positive_integer(
        value, "call_timeout_seconds", path, 60
    )

    if transport == "stdio":
        command = _required_string(value, "command", path)
        args = _string_tuple(value, "args", path)
        cwd = _optional_string(value, "cwd", path)
        env = _string_map(value, "env", path, expand_environment=True)
        return McpServerConfig(
            name=name,
            transport=transport,
            enabled=enabled,
            command=command,
            args=args,
            cwd=cwd,
            env=env,
            tools=tools,
            startup_timeout_seconds=startup_timeout,
            call_timeout_seconds=call_timeout,
        )

    url = _required_string(value, "url", path)
    headers = _string_map(
        value, "headers", path, expand_environment=True
    )
    return McpServerConfig(
        name=name,
        transport=transport,
        enabled=enabled,
        url=url,
        headers=headers,
        tools=tools,
        startup_timeout_seconds=startup_timeout,
        call_timeout_seconds=call_timeout,
    )


def _tool_config(value: object, server_path: str) -> McpToolConfig:
    path = f"{server_path}.tools"
    if value is None:
        return McpToolConfig()
    if not isinstance(value, dict):
        raise ValueError(f"config field '{path}' must be an object")
    _reject_unknown(value, {"enabled", "approval"}, path)

    enabled = _optional_string_set(value, "enabled", path)
    approval = value.get("approval", "always")
    if approval == "always":
        require_approval = None
    elif approval == "never":
        require_approval = frozenset()
    elif isinstance(approval, dict):
        _reject_unknown(approval, {"always"}, f"{path}.approval")
        if "always" not in approval:
            raise ValueError(
                f"config field '{path}.approval.always' is required"
            )
        require_approval = _string_set(
            approval["always"],
            f"{path}.approval.always",
        )
    else:
        raise ValueError(
            f"config field '{path}.approval' must be 'always', 'never', "
            "or an object with an 'always' array"
        )

    if (
        enabled is not None
        and require_approval is not None
        and not require_approval <= enabled
    ):
        unknown = ", ".join(sorted(require_approval - enabled))
        raise ValueError(
            f"config field '{path}.approval.always' contains tools not "
            f"listed in '{path}.enabled': {unknown}"
        )
    return McpToolConfig(
        enabled=enabled,
        require_approval=require_approval,
    )


def _transport(
    data: dict[object, object],
    path: str,
) -> McpTransport:
    transport = data.get("transport")
    if "transport" in data:
        if transport == "stdio":
            return "stdio"
        if transport == "streamable_http":
            return "streamable_http"
        raise ValueError(
            f"config field '{path}.transport' must be 'stdio' or "
            "'streamable_http'"
        )

    has_command = "command" in data
    has_url = "url" in data
    if has_command and has_url:
        raise ValueError(
            f"config field '{path}' cannot infer transport because both "
            "'command' and 'url' are present"
        )
    if has_command:
        return "stdio"
    if has_url:
        return "streamable_http"
    raise ValueError(
        f"config field '{path}' must specify 'transport', 'command', or 'url'"
    )


def _reject_unknown(
    data: dict[object, object], allowed: set[str], path: str
) -> None:
    unknown = set(data) - allowed
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ValueError(f"unknown config field(s) in '{path}': {names}")


def _boolean(
    data: dict[object, object], field: str, path: str, default: bool
) -> bool:
    value = data.get(field, default)
    if not isinstance(value, bool):
        raise ValueError(f"config field '{path}.{field}' must be a boolean")
    return value


def _positive_integer(
    data: dict[object, object], field: str, path: str, default: int
) -> int:
    value = data.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"config field '{path}.{field}' must be a positive integer"
        )
    return value


def _required_string(
    data: dict[object, object], field: str, path: str
) -> str:
    value = _optional_string(data, field, path)
    if value is None:
        raise ValueError(
            f"config field '{path}.{field}' must be a non-empty string"
        )
    return value


def _optional_string(
    data: dict[object, object], field: str, path: str
) -> str | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"config field '{path}.{field}' must be a non-empty string"
        )
    return value.strip()


def _string_tuple(
    data: dict[object, object], field: str, path: str
) -> tuple[str, ...]:
    value = data.get(field, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(
            f"config field '{path}.{field}' must be an array of strings"
        )
    return tuple(value)


def _optional_string_set(
    data: dict[object, object], field: str, path: str
) -> frozenset[str] | None:
    if field not in data:
        return None
    return _string_set(data[field], f"{path}.{field}")


def _string_set(value: object, path: str) -> frozenset[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(
            f"config field '{path}' must be an array of strings"
        )
    values = tuple(value)
    if any(not value for value in values):
        raise ValueError(
            f"config field '{path}' cannot contain empty strings"
        )
    if len(set(values)) != len(values):
        raise ValueError(
            f"config field '{path}' cannot contain duplicates"
        )
    return frozenset(values)


def _string_map(
    data: dict[object, object],
    field: str,
    path: str,
    *,
    expand_environment: bool,
) -> dict[str, str]:
    value = data.get(field, {})
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError(
            f"config field '{path}.{field}' must be an object of strings"
        )
    if not expand_environment:
        return dict(value)
    return {
        key: _expand_environment(item, f"{path}.{field}.{key}")
        for key, item in value.items()
    }


def _expand_environment(value: str, path: str) -> str:
    if not (value.startswith("${") and value.endswith("}")):
        return value
    variable = value[2:-1]
    if not variable:
        raise ValueError(
            f"config field '{path}' has an empty environment reference"
        )
    resolved = os.environ.get(variable)
    if resolved is None:
        raise ValueError(
            f"environment variable '{variable}' is required by '{path}'"
        )
    return resolved

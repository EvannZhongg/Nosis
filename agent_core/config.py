import json
from dataclasses import dataclass, field
from pathlib import Path

from .tools.config import ROLE_TOOL_NAMES, ToolConfig, load_tool_config
from .mcp.config import McpConfig, load_mcp_config

@dataclass(frozen=True)
class ContextCompressionConfig:
    enabled: bool = True
    trigger_ratio: float | None = None
    keep_recent_units: int = 6


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    global_max_tokens: int = 2000
    workspace_max_tokens: int = 3000


@dataclass(frozen=True)
class ProviderRequestConfig:
    request_timeout_seconds: int = 300
    max_retries: int = 2


@dataclass(frozen=True)
class SubagentRoleConfig:
    """A sub-agent role: what it is for, and which tools it may use.

    A disabled role stays in the file but is not offered to the model.
    """

    description: str
    tools: ToolConfig
    enabled: bool = True


@dataclass(frozen=True)
class AgentConfig:
    max_same_tool_calls: int
    output_reserve_tokens: int
    tools: ToolConfig
    workspace_instruction_files: tuple[str, ...]
    max_generation_tokens: int | None = None
    provider: ProviderRequestConfig = field(default_factory=ProviderRequestConfig)
    context: ContextCompressionConfig = field(
        default_factory=ContextCompressionConfig
    )
    mcp: McpConfig = field(default_factory=McpConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    subagent_roles: dict[str, SubagentRoleConfig] = field(
        default_factory=dict
    )


def load_agent_config(path: Path) -> AgentConfig:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)

    max_same_tool_calls = _positive_integer(
        data,
        "max_same_tool_calls",
    )
    output_reserve_tokens = _positive_integer(
        data,
        "output_reserve_tokens",
    )
    max_generation_tokens = _optional_positive_integer(
        data,
        "max_generation_tokens",
    )
    provider = _provider_config(data.get("provider"))
    workspace_instruction_files = _workspace_instruction_files(
        data.get("workspace_instruction_files")
    )
    tools = _main_agent_tools(data.get("main_agent"))
    subagent_roles = _subagent_roles(data.get("subagent_roles"))
    context = _context_config(data.get("context"))
    mcp = load_mcp_config(data.get("mcp"))
    memory = _memory_config(data.get("memory"))

    return AgentConfig(
        max_same_tool_calls=max_same_tool_calls,
        output_reserve_tokens=output_reserve_tokens,
        tools=tools,
        workspace_instruction_files=workspace_instruction_files,
        max_generation_tokens=max_generation_tokens,
        provider=provider,
        subagent_roles=subagent_roles,
        context=context,
        mcp=mcp,
        memory=memory,
    )


def _provider_config(value: object) -> ProviderRequestConfig:
    if value is None:
        return ProviderRequestConfig()
    if not isinstance(value, dict):
        raise ValueError("config field 'provider' must be an object")
    unknown = set(value) - {"request_timeout_seconds", "max_retries"}
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ValueError(f"unknown field(s) in 'provider': {fields}")
    return ProviderRequestConfig(
        request_timeout_seconds=_positive_integer_with_default(
            value,
            "request_timeout_seconds",
            300,
            prefix="provider",
        ),
        max_retries=_non_negative_integer_with_default(
            value,
            "max_retries",
            2,
            prefix="provider",
        ),
    )


def _workspace_instruction_files(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(
            "config field 'workspace_instruction_files' must be an array"
        )
    files = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                "config field 'workspace_instruction_files' must contain "
                "non-empty strings"
            )
        filename = item.strip()
        if "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise ValueError(
                "config field 'workspace_instruction_files' must contain "
                "workspace root filenames"
            )
        if filename in files:
            raise ValueError(
                "config field 'workspace_instruction_files' must not "
                "contain duplicate filenames"
            )
        files.append(filename)
    return tuple(files)


def _main_agent_tools(value: object) -> ToolConfig:
    if not isinstance(value, dict):
        raise ValueError("config field 'main_agent' must be an object")
    unknown = set(value) - {"tools"}
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ValueError(f"unknown field(s) in 'main_agent': {fields}")
    return load_tool_config(value.get("tools"))


def _subagent_roles(value: object) -> dict[str, SubagentRoleConfig]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("config field 'subagent_roles' must be an object")

    roles: dict[str, SubagentRoleConfig] = {}
    for name, settings in value.items():
        if not name.strip():
            raise ValueError("a subagent role name must not be empty")
        if not isinstance(settings, dict):
            raise ValueError(
                f"config field 'subagent_roles.{name}' must be an object"
            )
        description = settings.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(
                f"config field 'subagent_roles.{name}.description' must be "
                "a non-empty string"
            )
        enabled = settings.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(
                f"config field 'subagent_roles.{name}.enabled' must be "
                "a boolean"
            )
        unknown = set(settings) - {"description", "enabled", "tools"}
        if unknown:
            fields = ", ".join(sorted(unknown))
            raise ValueError(
                f"unknown field(s) in 'subagent_roles.{name}': {fields}"
            )
        roles[name] = SubagentRoleConfig(
            description=description.strip(),
            enabled=enabled,
            tools=load_tool_config(
                settings.get("tools", {}),
                allowed=ROLE_TOOL_NAMES,
            ),
        )
    return roles


def _context_config(value: object) -> ContextCompressionConfig:
    if value is None:
        return ContextCompressionConfig()
    if not isinstance(value, dict):
        raise ValueError("config field 'context' must be an object")
    compression = value.get("compression")
    if compression is None:
        return ContextCompressionConfig()
    if not isinstance(compression, dict):
        raise ValueError("config field 'context.compression' must be an object")
    unknown = set(compression) - {
        "enabled",
        "trigger_ratio",
        "keep_recent_units",
    }
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ValueError(
            "unknown field(s) in 'context.compression': "
            f"{fields}"
        )
    enabled = compression.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("config field 'context.compression.enabled' must be a boolean")
    trigger = _optional_ratio(compression, "trigger_ratio")
    keep_recent_units = _positive_integer_with_default(
        compression, "keep_recent_units", 6, prefix="context.compression"
    )
    return ContextCompressionConfig(enabled, trigger, keep_recent_units)


def _memory_config(value: object) -> MemoryConfig:
    if value is None:
        return MemoryConfig()
    if not isinstance(value, dict):
        raise ValueError("config field 'memory' must be an object")
    unknown = set(value) - {
        "enabled",
        "global_max_tokens",
        "workspace_max_tokens",
    }
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ValueError(f"unknown field(s) in 'memory': {fields}")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("config field 'memory.enabled' must be a boolean")
    return MemoryConfig(
        enabled=enabled,
        global_max_tokens=_positive_integer_with_default(
            value, "global_max_tokens", 2000, prefix="memory"
        ),
        workspace_max_tokens=_positive_integer_with_default(
            value, "workspace_max_tokens", 3000, prefix="memory"
        ),
    )


def _optional_ratio(data: dict[str, object], field: str) -> float | None:
    value = data.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
        raise ValueError(f"config field 'context.compression.{field}' must be between 0 and 1")
    return float(value)


def _positive_integer(data: dict[str, object], field: str) -> int:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"config field '{field}' must be a positive integer"
        )
    return value


def _optional_positive_integer(
    data: dict[str, object],
    field: str,
) -> int | None:
    value = data.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"config field '{field}' must be a positive integer or null"
        )
    return value


def _positive_integer_with_default(
    data: dict[str, object],
    field: str,
    default: int,
    *,
    prefix: str,
) -> int:
    value = data.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"config field '{prefix}.{field}' must be a positive integer"
        )
    return value


def _non_negative_integer_with_default(
    data: dict[str, object],
    field: str,
    default: int,
    *,
    prefix: str,
) -> int:
    value = data.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"config field '{prefix}.{field}' must be a non-negative integer"
        )
    return value

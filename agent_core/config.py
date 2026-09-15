import json
from dataclasses import dataclass, field
from pathlib import Path

from .execution import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    MAX_COMMAND_TIMEOUT_SECONDS,
)
from .tools.config import ROLE_TOOL_NAMES, ToolConfig, load_tool_config
from .mcp.config import McpConfig, load_mcp_config


DEFAULT_SHELL_TIMEOUT_SECONDS = DEFAULT_COMMAND_TIMEOUT_SECONDS
MAX_SHELL_TIMEOUT_SECONDS = MAX_COMMAND_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ContextCompressionConfig:
    enabled: bool = True
    trigger_ratio: float | None = None


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
    max_generation_tokens: int | None = None
    shell_timeout_seconds: int = DEFAULT_SHELL_TIMEOUT_SECONDS
    context: ContextCompressionConfig = field(
        default_factory=ContextCompressionConfig
    )
    mcp: McpConfig = field(default_factory=McpConfig)
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
    shell_timeout_seconds = data.get(
        "shell_timeout_seconds",
        DEFAULT_SHELL_TIMEOUT_SECONDS,
    )
    if (
        isinstance(shell_timeout_seconds, bool)
        or not isinstance(shell_timeout_seconds, int)
        or shell_timeout_seconds < 1
        or shell_timeout_seconds > MAX_SHELL_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "config field 'shell_timeout_seconds' must be an integer "
            f"between 1 and {MAX_SHELL_TIMEOUT_SECONDS}"
        )
    tools = _main_agent_tools(data.get("main_agent"))
    subagent_roles = _subagent_roles(data.get("subagent_roles"))
    context = _context_config(data.get("context"))
    mcp = load_mcp_config(data.get("mcp"))

    return AgentConfig(
        max_same_tool_calls=max_same_tool_calls,
        output_reserve_tokens=output_reserve_tokens,
        tools=tools,
        max_generation_tokens=max_generation_tokens,
        subagent_roles=subagent_roles,
        shell_timeout_seconds=shell_timeout_seconds,
        context=context,
        mcp=mcp,
    )


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
    unknown = set(compression) - {"enabled", "trigger_ratio"}
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
    return ContextCompressionConfig(enabled, trigger)


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

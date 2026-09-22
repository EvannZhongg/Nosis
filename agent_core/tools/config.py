from dataclasses import dataclass


TOOL_NAMES = (
    "read_file",
    "apply_patch",
    "edit_file",
    "write_file",
    "search_files",
    "list_directory",
    "shell",
    "web_search",
    "subagent",
    "create_scheduled_task",
    "update_scheduled_task",
    "list_scheduled_tasks",
    "delete_scheduled_task",
)

# ``read_image``, ``analyze_image`` and ``read_skill`` are absent by design:
# they are derived from Runtime capabilities rather than configured. See
# ``agent_core.subagent``.

# A role runs inside a sub-agent, so it can never delegate again.
ROLE_TOOL_NAMES = tuple(name for name in TOOL_NAMES if name != "subagent")


@dataclass(frozen=True)
class ToolConfig:
    """Which tools an agent may call, in a fixed order.

    Tool schemas sit at the front of the prompt, so their order is part of
    the cache prefix. ``enabled`` is a tuple ordered by ``TOOL_NAMES``
    rather than a set, so the same config yields the same schema order in
    every process and the provider's prompt cache keeps hitting.
    """

    enabled: tuple[str, ...]

    def is_enabled(self, name: str) -> bool:
        return name in self.enabled


def load_tool_config(data: object, *, allowed: tuple[str, ...] = TOOL_NAMES) -> ToolConfig:
    if not isinstance(data, dict):
        raise ValueError("config field 'tools' must be an object")

    unknown_names = set(data) - set(allowed)
    if unknown_names:
        names = ", ".join(sorted(unknown_names))
        raise ValueError(f"unknown tool config field(s): {names}")

    enabled = []
    for name in allowed:
        value = data.get(name, False)
        if not isinstance(value, bool):
            raise ValueError(
                f"config field 'tools.{name}' must be a boolean"
            )
        if value:
            enabled.append(name)

    return ToolConfig(enabled=tuple(enabled))

"""Plugin package discovery and component routing for Bridge assembly."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from agent_core import (
    McpServerConfig,
    SkillLocation,
    load_mcp_server_map,
    namespace_mcp_servers,
)


_PLUGIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COMPONENT_KINDS = ("skills", "mcp", "tools", "agents", "hooks")


@dataclass(frozen=True)
class PluginComponents:
    """References declared by a plugin package.

    Skill directories and MCP configurations are registered today. The
    remaining references are retained as subsystem-specific extension points
    and have no runtime behavior until their owning subsystem has an adapter.
    """

    skills: tuple[Path, ...] = ()
    mcp: tuple[Path, ...] = ()
    tools: tuple[Path, ...] = ()
    agents: tuple[Path, ...] = ()
    hooks: tuple[Path, ...] = ()


@dataclass(frozen=True)
class PluginDescriptor:
    """Declarative metadata for one Nosis capability package."""

    name: str
    root: Path
    enabled: bool = True
    version: str | None = None
    description: str | None = None
    dependencies: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    components: PluginComponents = field(default_factory=PluginComponents)


class PluginLoader:
    """Parse one ``plugin.json`` without initializing runtime components."""

    def load(self, manifest_path: Path) -> PluginDescriptor:
        manifest = manifest_path.resolve()
        try:
            with manifest.open(encoding="utf-8") as file:
                data = json.load(file)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON in {manifest}: {error}") from error
        if not isinstance(data, dict):
            raise ValueError(f"plugin manifest must be an object: {manifest}")

        _reject_unknown(
            data,
            {
                "name",
                "version",
                "description",
                "enabled",
                "dependencies",
                "capabilities",
                "components",
            },
            "plugin",
        )
        name = _required_string(data, "name", "plugin")
        if not _PLUGIN_NAME.fullmatch(name):
            raise ValueError(
                "plugin name may contain only letters, numbers, '.', '_', "
                "and '-', and must start with a letter or number"
            )
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("plugin field 'enabled' must be a boolean")

        root = manifest.parent
        component_data = data.get("components", {})
        if not isinstance(component_data, dict):
            raise ValueError("plugin field 'components' must be an object")
        _reject_unknown(component_data, set(_COMPONENT_KINDS), "components")
        components = PluginComponents(
            skills=_component_paths(component_data, "skills", root),
            mcp=_component_paths(component_data, "mcp", root),
            tools=_component_paths(component_data, "tools", root),
            agents=_component_paths(component_data, "agents", root),
            hooks=_component_paths(component_data, "hooks", root),
        )
        return PluginDescriptor(
            name=name,
            root=root,
            enabled=enabled,
            version=_optional_string(data, "version", "plugin"),
            description=_optional_string(data, "description", "plugin"),
            dependencies=_string_tuple(data, "dependencies", "plugin"),
            capabilities=_string_tuple(data, "capabilities", "plugin"),
            components=components,
        )


@dataclass(frozen=True)
class PluginSkillSource:
    """Expose an enabled plugin's declared Skill directories to Core."""

    plugin: PluginDescriptor

    def skill_locations(self) -> Iterable[SkillLocation]:
        locations = []
        for root in self.plugin.components.skills:
            if not root.is_dir():
                continue
            locations.extend(
                SkillLocation(child / "SKILL.md", self.plugin.name)
                for child in sorted(root.iterdir(), key=lambda path: path.name)
                if child.is_dir() and (child / "SKILL.md").is_file()
            )
        return tuple(locations)


class PluginManager:
    """Discover plugin packages and route enabled components to subsystems."""

    def __init__(
        self,
        plugins: Iterable[PluginDescriptor] = (),
        warnings: Iterable[str] = (),
    ) -> None:
        self._plugins = tuple(plugins)
        self._warnings = tuple(warnings)

    @classmethod
    def discover(cls, directory: Path) -> "PluginManager":
        root = directory.expanduser().resolve()
        if not root.is_dir():
            return cls()

        loader = PluginLoader()
        plugins: dict[str, PluginDescriptor] = {}
        warnings = []
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            manifest = child / "plugin.json"
            if not child.is_dir() or not manifest.is_file():
                continue
            try:
                plugin = loader.load(manifest)
            except (OSError, ValueError) as error:
                warnings.append(f"Skipping plugin at '{manifest}': {error}")
                continue
            existing = plugins.get(plugin.name)
            if existing is not None:
                warnings.append(
                    f"Skipping duplicate plugin '{plugin.name}' at "
                    f"'{plugin.root}'; already discovered at '{existing.root}'"
                )
                continue
            plugins[plugin.name] = plugin
        return cls(plugins.values(), warnings)

    @property
    def plugins(self) -> tuple[PluginDescriptor, ...]:
        return self._plugins

    @property
    def enabled_plugins(self) -> tuple[PluginDescriptor, ...]:
        return tuple(plugin for plugin in self._plugins if plugin.enabled)

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    def skill_sources(self) -> tuple[PluginSkillSource, ...]:
        return tuple(PluginSkillSource(plugin) for plugin in self.enabled_plugins)

    def mcp_servers(self) -> tuple[McpServerConfig, ...]:
        """Load enabled MCP component references through the MCP subsystem."""
        servers = []
        for plugin in self.enabled_plugins:
            for path in plugin.components.mcp:
                try:
                    with path.open(encoding="utf-8") as file:
                        data = json.load(file)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"invalid MCP JSON for plugin '{plugin.name}' at "
                        f"'{path}': {error}"
                    ) from error
                try:
                    loaded = load_mcp_server_map(data)
                except ValueError as error:
                    raise ValueError(
                        f"invalid MCP config for plugin '{plugin.name}' at "
                        f"'{path}': {error}"
                    ) from error
                servers.extend(
                    namespace_mcp_servers(
                        loaded,
                        plugin.name,
                        base_directory=plugin.root,
                    )
                )
        return tuple(servers)


def _component_paths(
    data: dict[str, object],
    field: str,
    root: Path,
) -> tuple[Path, ...]:
    references = _string_tuple(data, field, "components")
    paths = []
    for reference in references:
        relative = Path(reference)
        if relative.is_absolute():
            raise ValueError(
                f"plugin field 'components.{field}' must contain relative paths"
            )
        resolved = (root / relative).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError(
                f"plugin field 'components.{field}' must stay inside the plugin"
            ) from error
        paths.append(resolved)
    return tuple(paths)


def _reject_unknown(
    data: dict[object, object], allowed: set[str], path: str
) -> None:
    unknown = set(data) - allowed
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ValueError(f"unknown plugin field(s) in '{path}': {names}")


def _required_string(
    data: dict[str, object], field: str, path: str
) -> str:
    value = _optional_string(data, field, path)
    if value is None:
        raise ValueError(
            f"plugin field '{path}.{field}' must be a non-empty string"
        )
    return value


def _optional_string(
    data: dict[str, object], field: str, path: str
) -> str | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"plugin field '{path}.{field}' must be a non-empty string"
        )
    return value.strip()


def _string_tuple(
    data: dict[str, object], field: str, path: str
) -> tuple[str, ...]:
    value = data.get(field, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(
            f"plugin field '{path}.{field}' must be an array of non-empty strings"
        )
    values = tuple(item.strip() for item in value)
    if len(set(values)) != len(values):
        raise ValueError(
            f"plugin field '{path}.{field}' cannot contain duplicates"
        )
    return values

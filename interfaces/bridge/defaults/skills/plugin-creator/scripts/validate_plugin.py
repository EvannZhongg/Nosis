#!/usr/bin/env python3
"""Validate a Nosis Plugin package against the contract the Runtime loader accepts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from plugin_names import validate_plugin_name

PLUGIN_FIELDS = {
    "name",
    "version",
    "description",
    "enabled",
    "capabilities",
    "components",
}
COMPONENT_KINDS = ("skills", "agents", "mcp")
ROLE_TOOL_NAMES = (
    "read_file",
    "edit_file",
    "write_file",
    "search_files",
    "list_directory",
    "shell",
    "web_search",
    "create_scheduled_task",
    "update_scheduled_task",
    "list_scheduled_tasks",
    "delete_scheduled_task",
)
FRONT_MATTER = re.compile(
    r"\A---[ \t]*\r?\n(?P<metadata>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
MCP_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
MCP_SHARED_FIELDS = {
    "enabled",
    "transport",
    "tools",
    "startup_timeout_seconds",
    "call_timeout_seconds",
}
MCP_STDIO_FIELDS = MCP_SHARED_FIELDS | {"command", "args", "cwd", "env"}
MCP_HTTP_FIELDS = MCP_SHARED_FIELDS | {"url", "headers"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a Nosis Plugin package.")
    parser.add_argument("plugin_path", help="Path to the plugin root directory")
    return parser.parse_args()


def main() -> None:
    plugin_root = Path(parse_args().plugin_path).expanduser().resolve()
    errors, registered = validate_plugin(plugin_root)
    if errors:
        print(f"Plugin validation failed: {plugin_root}")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)
    print(f"Plugin validation passed: {plugin_root}")
    for entry in registered:
        print(f"  {entry}")


def validate_plugin(plugin_root: Path) -> tuple[list[str], list[str]]:
    plugin_root = plugin_root.resolve()
    errors: list[str] = []
    registered: list[str] = []
    if not plugin_root.is_dir():
        return [f"plugin directory does not exist: {plugin_root}"], registered

    manifest = load_json_object(plugin_root / "plugin.json", "plugin.json", errors)
    if manifest is None:
        return errors, registered

    reject_unknown(manifest, PLUGIN_FIELDS, "plugin.json", errors)
    plugin_name = require_string(manifest, "name", "plugin.json", errors)
    if plugin_name is not None:
        try:
            validate_plugin_name(plugin_name)
        except ValueError as error:
            errors.append(f"plugin.json field `name` is invalid: {error}")
    for field in ("version", "description"):
        optional_string(manifest, field, "plugin.json", errors)
    if "enabled" in manifest and not isinstance(manifest["enabled"], bool):
        errors.append("plugin.json field `enabled` must be a boolean")
    string_array(manifest, "capabilities", "plugin.json", errors)

    components = manifest.get("components", {})
    if not isinstance(components, dict):
        errors.append("plugin.json field `components` must be an object")
        return errors, registered
    reject_unknown(
        components, set(COMPONENT_KINDS), "plugin.json field `components`", errors
    )

    namespace = plugin_name or ""
    for path in component_paths(components, "skills", plugin_root, errors):
        validate_skill_directory(path, namespace, errors, registered)
    for path in component_paths(components, "agents", plugin_root, errors):
        validate_agent(path, namespace, errors, registered)
    for path in component_paths(components, "mcp", plugin_root, errors):
        validate_mcp_servers(path, namespace, errors, registered)
    return errors, registered


def component_paths(
    components: dict[str, Any],
    kind: str,
    plugin_root: Path,
    errors: list[str],
) -> list[Path]:
    value = components.get(kind)
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        errors.append(
            f"plugin.json field `components.{kind}` must be an array of "
            "non-empty strings"
        )
        return []
    references = [item.strip() for item in value]
    if len(set(references)) != len(references):
        errors.append(
            f"plugin.json field `components.{kind}` cannot contain duplicates"
        )
    paths = []
    for reference in references:
        if Path(reference).is_absolute():
            errors.append(
                f"plugin.json field `components.{kind}` must contain relative "
                f"paths, found `{reference}`"
            )
            continue
        resolved = (plugin_root / reference).resolve()
        if not resolved.is_relative_to(plugin_root):
            errors.append(
                f"plugin.json field `components.{kind}` must stay inside the "
                f"plugin, found `{reference}`"
            )
            continue
        paths.append(resolved)
    return paths


def validate_skill_directory(
    root: Path,
    namespace: str,
    errors: list[str],
    registered: list[str],
) -> None:
    if not root.is_dir():
        errors.append(f"declared skills path is not a directory: {root}")
        return
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if not child.is_dir() or not (child / "SKILL.md").is_file():
            continue
        label = f"skill `{child.name}`"
        front_matter = read_front_matter(child / "SKILL.md", label, errors)
        if front_matter is None:
            continue
        name = require_string(front_matter, "name", label, errors)
        require_string(front_matter, "description", label, errors)
        if name is not None:
            registered.append(f"{namespace}:{name}")


def validate_agent(
    path: Path,
    namespace: str,
    errors: list[str],
    registered: list[str],
) -> None:
    label = f"agent `{path.name}`"
    if not path.is_file():
        errors.append(f"declared agent path is not a file: {path}")
        return
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        errors.append(f"{label} is not readable as UTF-8: {error}")
        return
    match = FRONT_MATTER.match(content)
    if match is None:
        errors.append(f"{label} must start with YAML front matter")
        return
    try:
        metadata = yaml.safe_load(match.group("metadata"))
    except yaml.YAMLError as error:
        errors.append(f"{label} front matter is not valid YAML: {error}")
        return
    if not isinstance(metadata, dict):
        errors.append(f"{label} front matter must be an object")
        return
    name = require_string(metadata, "name", label, errors)
    if name is not None:
        try:
            validate_plugin_name(name)
        except ValueError as error:
            errors.append(f"{label} field `name` is invalid: {error}")
    require_string(metadata, "description", label, errors)
    if not content[match.end() :].strip():
        errors.append(f"{label} body must contain the role instructions")
    validate_agent_tools(metadata, label, errors)
    if "model" in metadata:
        optional_string(metadata, "model", label, errors)
    if name is not None:
        registered.append(f"{namespace}:{name}")


def validate_agent_tools(
    metadata: dict[Any, Any], label: str, errors: list[str]
) -> None:
    if "tools" not in metadata:
        return
    value = metadata["tools"]
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        errors.append(f"{label} field `tools` must be an array of tool names")
        return
    tools = [item.strip() for item in value]
    if len(set(tools)) != len(tools):
        errors.append(f"{label} field `tools` cannot contain duplicates")
        return
    unknown = sorted(set(tools) - set(ROLE_TOOL_NAMES))
    if unknown:
        errors.append(
            f"{label} field `tools` uses unknown tool(s): {', '.join(unknown)}"
        )


def validate_mcp_servers(
    path: Path,
    namespace: str,
    errors: list[str],
    registered: list[str],
) -> None:
    label = f"MCP component `{path.name}`"
    payload = load_json_object(path, label, errors)
    if payload is None:
        return
    for server_name, server in payload.items():
        server_label = f"{label} server `{server_name}`"
        if not isinstance(server_name, str) or not MCP_SERVER_NAME.fullmatch(
            server_name
        ):
            errors.append(
                f"{label} server names may contain only letters, numbers, "
                "'_' and '-'"
            )
            continue
        validate_mcp_server(server, server_label, errors)
        registered.append(f"{namespace}:{server_name}")


def validate_mcp_server(server: Any, label: str, errors: list[str]) -> None:
    if not isinstance(server, dict):
        errors.append(f"{label} must be an object")
        return
    if "type" in server and "transport" in server:
        errors.append(f"{label} cannot set both `type` and `transport`")
        return
    transport = server.get("type", server.get("transport"))
    if transport is None:
        if "command" in server and "url" in server:
            errors.append(
                f"{label} cannot infer transport from both `command` and `url`"
            )
            return
        transport = "stdio" if "command" in server else "http"
    if transport in ("http", "streamable_http"):
        allowed = MCP_HTTP_FIELDS
        required = "url"
    elif transport == "stdio":
        allowed = MCP_STDIO_FIELDS
        required = "command"
    else:
        errors.append(
            f"{label} transport must be 'stdio', 'http' or 'streamable_http'"
        )
        return
    reject_unknown(
        {key: value for key, value in server.items() if key != "type"},
        allowed | ({"type"} if "type" in server else set()),
        label,
        errors,
    )
    if not isinstance(server.get(required), str) or not server[required].strip():
        errors.append(f"{label} field `{required}` must be a non-empty string")


def load_json_object(
    path: Path, label: str, errors: list[str]
) -> dict[str, Any] | None:
    if not path.is_file():
        errors.append(f"{label} is missing")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        errors.append(f"{label} is not readable as UTF-8: {error}")
        return None
    except json.JSONDecodeError as error:
        errors.append(f"{label} must contain valid JSON: {error}")
        return None
    if not isinstance(payload, dict):
        errors.append(f"{label} must contain a JSON object")
        return None
    return payload


def read_front_matter(
    path: Path, label: str, errors: list[str]
) -> dict[str, Any] | None:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        errors.append(f"{label} is not readable as UTF-8: {error}")
        return None
    match = FRONT_MATTER.match(content)
    if match is None:
        errors.append(f"{label} must start with YAML front matter")
        return None
    try:
        metadata = yaml.safe_load(match.group("metadata"))
    except yaml.YAMLError as error:
        errors.append(f"{label} front matter is not valid YAML: {error}")
        return None
    if not isinstance(metadata, dict):
        errors.append(f"{label} front matter must be an object")
        return None
    return metadata


def reject_unknown(
    payload: dict[Any, Any], allowed: set[str], label: str, errors: list[str]
) -> None:
    for key in sorted(set(payload) - allowed):
        errors.append(f"{label} field `{key}` is not accepted")


def require_string(
    payload: dict[Any, Any], field: str, label: str, errors: list[str]
) -> str | None:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} field `{field}` must be a non-empty string")
        return None
    return value.strip()


def optional_string(
    payload: dict[Any, Any], field: str, label: str, errors: list[str]
) -> None:
    if field in payload and (
        not isinstance(payload[field], str) or not payload[field].strip()
    ):
        errors.append(f"{label} field `{field}` must be a non-empty string")


def string_array(
    payload: dict[Any, Any], field: str, label: str, errors: list[str]
) -> None:
    if field not in payload:
        return
    value = payload[field]
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        errors.append(
            f"{label} field `{field}` must be an array of non-empty strings"
        )
        return
    items = [item.strip() for item in value]
    if len(set(items)) != len(items):
        errors.append(f"{label} field `{field}` cannot contain duplicates")


if __name__ == "__main__":
    main()

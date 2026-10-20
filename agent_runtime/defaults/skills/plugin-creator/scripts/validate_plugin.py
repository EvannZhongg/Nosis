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
    "apply_patch",
    "edit_file",
    "write_file",
    "search_files",
    "list_directory",
    "shell",
    "web_search",
    "web_fetch",
    "scheduled_task",
)
FRONT_MATTER = re.compile(
    r"\A---[ \t]*\r?\n(?P<metadata>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
MCP_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
MCP_TOOL_REFERENCE = re.compile(r"^mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+$")
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
    print("Static validation only. Verify MCP tools in Nosis.")


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
    for kind, validate in (
        ("skills", validate_skill_directory),
        ("agents", validate_agent),
        ("mcp", validate_mcp_servers),
    ):
        entries: list[str] = []
        for path in component_paths(components, kind, plugin_root, errors):
            validate(path, namespace, errors, entries)
        duplicates = sorted({entry for entry in entries if entries.count(entry) > 1})
        for entry in duplicates:
            errors.append(f"duplicate {kind} registration: {entry}")
        registered.extend(entries)
    return errors, registered


def component_paths(
    components: dict[str, Any],
    kind: str,
    plugin_root: Path,
    errors: list[str],
) -> list[Path]:
    if kind not in components:
        return []
    value = components[kind]
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
        metadata = front_matter.get("metadata")
        enabled = True
        if metadata is not None:
            if not isinstance(metadata, dict):
                errors.append(f"{label} field `metadata` must be an object")
                continue
            nosis = metadata.get("nosis")
            if nosis is not None:
                if not isinstance(nosis, dict):
                    errors.append(f"{label} field `metadata.nosis` must be an object")
                    continue
                enabled = nosis.get("register", True)
                if not isinstance(enabled, bool):
                    errors.append(f"{label} field `metadata.nosis.register` must be a boolean")
                    continue
        if name is not None and enabled:
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
    unknown = sorted(
        name for name in tools
        if name not in ROLE_TOOL_NAMES and not MCP_TOOL_REFERENCE.fullmatch(name)
    )
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
    server = dict(server)
    server_type = server.pop("type", None)
    if server_type is not None and "transport" in server:
        errors.append(f"{label} cannot set both `type` and `transport`")
        return
    if server_type is not None:
        if server_type not in ("stdio", "http"):
            errors.append(f"{label} type must be 'stdio' or 'http'")
            return
        server["transport"] = "stdio" if server_type == "stdio" else "streamable_http"
    if "transport" not in server:
        if "command" in server and "url" in server:
            errors.append(
                f"{label} cannot infer transport from both `command` and `url`"
            )
            return
        if "command" not in server and "url" not in server:
            errors.append(f"{label} must specify transport, command, or url")
            return
        server["transport"] = "stdio" if "command" in server else "streamable_http"
    transport = server["transport"]
    if transport == "streamable_http":
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
    reject_unknown(server, allowed, label, errors)
    if not isinstance(server.get(required), str) or not server[required].strip():
        errors.append(f"{label} field `{required}` must be a non-empty string")
    if "enabled" in server and not isinstance(server["enabled"], bool):
        errors.append(f"{label} field `enabled` must be a boolean")
    for field in ("startup_timeout_seconds", "call_timeout_seconds"):
        value = server.get(field, 1)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            errors.append(f"{label} field `{field}` must be a positive integer")
    if transport == "stdio":
        optional_string(server, "cwd", label, errors)
        args = server.get("args", [])
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            errors.append(f"{label} field `args` must be an array of strings")
    map_field = "env" if transport == "stdio" else "headers"
    mapping = server.get(map_field, {})
    if not isinstance(mapping, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in mapping.items()
    ):
        errors.append(f"{label} field `{map_field}` must be an object of strings")
    elif any(value == "${}" for value in mapping.values()):
        errors.append(f"{label} field `{map_field}` has an empty environment reference")
    tools = server.get("tools")
    if tools is None:
        return
    if not isinstance(tools, dict):
        errors.append(f"{label} field `tools` must be an object")
        return
    tool_label = f"{label} field `tools`"
    reject_unknown(tools, {"enabled", "approval"}, tool_label, errors)
    enabled = None
    if "enabled" in tools:
        enabled = mcp_tool_array(tools["enabled"], f"{tool_label}.enabled", errors)
    approval = tools.get("approval", "always")
    if isinstance(approval, dict):
        reject_unknown(approval, {"always"}, f"{tool_label}.approval", errors)
        required = mcp_tool_array(approval.get("always"), f"{tool_label}.approval.always", errors)
        if enabled is not None and required is not None and not required <= enabled:
            errors.append(f"{tool_label}.approval.always contains tools not listed in tools.enabled")
    elif approval not in ("always", "never"):
        errors.append(f"{tool_label}.approval must be 'always', 'never', or an object with an 'always' array")


def mcp_tool_array(value: Any, label: str, errors: list[str]) -> set[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        errors.append(f"{label} must be an array of non-empty strings")
        return None
    if len(set(value)) != len(value):
        errors.append(f"{label} cannot contain duplicates")
    return set(value)


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
    lines = content.splitlines()
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if not lines or lines[0].strip() != "---" or end is None:
        errors.append(f"{label} must start with YAML front matter")
        return None
    try:
        metadata = yaml.safe_load("\n".join(lines[1:end]))
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
    for key in sorted(set(payload) - allowed, key=str):
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
    if payload.get(field) is not None and (
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

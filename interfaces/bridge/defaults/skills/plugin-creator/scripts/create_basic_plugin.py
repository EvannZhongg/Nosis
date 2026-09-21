#!/usr/bin/env python3
"""Scaffold a Nosis Plugin package with a manifest the Runtime loader accepts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from plugin_names import (
    MAX_PLUGIN_NAME_LENGTH,
    display_name,
    normalize_plugin_name,
    validate_plugin_name,
)

DEFAULT_PARENT = Path.home() / ".nosis" / "plugins"
COMPONENT_ORDER = ("skills", "agents", "mcp")

EXAMPLE_AGENT = """---
name: example
description: Summarize what this role does and when it should be used.
tools:
  - read_file
  - search_files
---

Describe the working method this role follows.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a Nosis Plugin package with a valid plugin.json."
    )
    parser.add_argument("plugin_name")
    parser.add_argument(
        "--path",
        default=str(DEFAULT_PARENT),
        help=(
            "Directory that receives the plugin folder "
            f"(defaults to {DEFAULT_PARENT})"
        ),
    )
    parser.add_argument(
        "--with-skills", action="store_true", help="Create and declare skills/"
    )
    parser.add_argument(
        "--with-agents",
        action="store_true",
        help="Create and declare an example sub-agent role",
    )
    parser.add_argument(
        "--with-mcp",
        action="store_true",
        help="Create and declare .mcp.json",
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite existing files"
    )
    return parser.parse_args()


def build_manifest(
    plugin_name: str, components: dict[str, list[str]]
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "name": plugin_name,
        "version": "0.1.0",
        "description": f"{display_name(plugin_name)} capability package",
        "enabled": True,
    }
    if components:
        manifest["capabilities"] = [
            kind for kind in COMPONENT_ORDER if kind in components
        ]
    manifest["components"] = components
    return manifest


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def write_text(path: Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def create_components(plugin_root: Path, args: argparse.Namespace) -> dict[str, list[str]]:
    components: dict[str, list[str]] = {}
    if args.with_skills:
        (plugin_root / "skills").mkdir(parents=True, exist_ok=True)
        components["skills"] = ["skills"]
    if args.with_agents:
        agents_directory = plugin_root / "agents"
        agents_directory.mkdir(parents=True, exist_ok=True)
        write_text(
            agents_directory / "example.md",
            EXAMPLE_AGENT,
            args.force,
        )
        components["agents"] = ["agents/example.md"]
    if args.with_mcp:
        write_text(
            plugin_root / ".mcp.json",
            json.dumps({}, indent=2) + "\n",
            args.force,
        )
        components["mcp"] = [".mcp.json"]
    return components


def main() -> None:
    args = parse_args()
    plugin_name = normalize_plugin_name(args.plugin_name)
    if not plugin_name:
        raise ValueError("plugin name must contain at least one letter or digit")
    if len(plugin_name) > MAX_PLUGIN_NAME_LENGTH:
        raise ValueError(
            f"plugin name '{plugin_name}' is longer than "
            f"{MAX_PLUGIN_NAME_LENGTH} characters"
        )
    validate_plugin_name(plugin_name)
    if plugin_name != args.plugin_name:
        print(f"Note: using normalized plugin name '{plugin_name}'.")

    plugin_root = Path(args.path).expanduser().resolve() / plugin_name
    manifest_path = plugin_root / "plugin.json"
    if manifest_path.exists() and not args.force:
        raise FileExistsError(
            f"{manifest_path} already exists; pass --force to overwrite it"
        )

    plugin_root.mkdir(parents=True, exist_ok=True)
    components = create_components(plugin_root, args)
    write_json(manifest_path, build_manifest(plugin_name, components))

    print(f"Created plugin package: {plugin_root}")
    print(f"manifest: {manifest_path}")
    if not components:
        print("This plugin declares no components and contributes nothing yet.")
    for kind in COMPONENT_ORDER:
        for reference in components.get(kind, []):
            print(f"{kind}: {plugin_root / reference}")
    print(
        "Replace the placeholder description, then validate with "
        f"'python3 scripts/validate_plugin.py {plugin_root}'."
    )


if __name__ == "__main__":
    main()

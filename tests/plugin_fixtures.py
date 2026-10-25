"""Declarative plugin inputs shared by loader and Runtime integration tests."""

import importlib.util
import json
from pathlib import Path


SCRIPTS = (Path(__file__).resolve().parents[1] / "agent_runtime" / "defaults"
           / "skills" / "plugin-creator" / "scripts")
spec = importlib.util.spec_from_file_location("plugin_validator", SCRIPTS / "validate_plugin.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def write_manifest(root: Path, name: str, **fields: object) -> Path:
    plugin = root / name
    plugin.mkdir(parents=True)
    manifest = plugin / "plugin.json"
    manifest.write_text(
        json.dumps({"name": name, **fields}),
        encoding="utf-8",
    )
    return manifest


def write_skill(root: Path, name: str = "shared") -> None:
    skill = root / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Skill from {root.name}.\n"
        "---\n",
        encoding="utf-8",
    )


def write_agent(
    root: Path,
    name: str = "reviewer",
    *,
    fields: str = "",
    body: str = "Review the requested code.",
) -> Path:
    agent = root / "agents" / f"{name}.md"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Review code as {name}.\n"
        f"{fields}"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return agent

"""Skill documents shared by loading and registration-order tests."""

from pathlib import Path


def write_skill(
    root: Path,
    directory: str,
    *,
    name: str = "demo-skill",
    description: str = "Does the demo task.",
    metadata: str = "",
    body: str = "# Demo\n\nFollow these instructions.",
) -> Path:
    skill_directory = root / directory
    skill_directory.mkdir(parents=True)
    skill_path = skill_directory / "SKILL.md"
    skill_path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{metadata}"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill_path

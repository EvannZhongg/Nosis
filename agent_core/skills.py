"""Skill discovery and metadata exposed to Agent runtimes."""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml


@dataclass(frozen=True)
class Skill:
    """One registered skill and the directory that owns its resources."""

    name: str
    description: str
    directory: Path


class SkillRegistry:
    """Registered skills keyed by their front-matter name."""

    def __init__(
        self,
        skills: Iterable[Skill] = (),
        warnings: Iterable[str] = (),
    ) -> None:
        self._skills: dict[str, Skill] = {}
        self._warnings = tuple(warnings)
        for skill in skills:
            if skill.name in self._skills:
                raise ValueError(
                    f"skill '{skill.name}' is already registered"
                )
            self._skills[skill.name] = skill

    @classmethod
    def discover(cls, directory: Path) -> "SkillRegistry":
        """Load valid, uniquely named ``SKILL.md`` files from children."""
        root = directory.expanduser().resolve()
        if not root.is_dir():
            return cls()

        skills: dict[str, Skill] = {}
        discovery_warnings = []
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            skill_path = child / "SKILL.md"
            if not child.is_dir() or not skill_path.is_file():
                continue
            try:
                skill, registered = _load_skill(skill_path)
            except (OSError, ValueError) as error:
                discovery_warnings.append(
                    f"Skipping skill at '{skill_path}': {error}"
                )
                continue
            if not registered:
                continue
            existing = skills.get(skill.name)
            if existing is not None:
                discovery_warnings.append(
                    f"Skipping duplicate skill '{skill.name}' at "
                    f"'{skill.directory}'; already registered from "
                    f"'{existing.directory}'"
                )
                continue
            skills[skill.name] = skill
        return cls(skills.values(), discovery_warnings)

    def __bool__(self) -> bool:
        return bool(self._skills)

    def __iter__(self):
        return iter(self._skills.values())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    def get(self, name: str) -> Skill:
        skill = self._skills.get(name)
        if skill is None:
            known = ", ".join(self._skills) or "none"
            raise ValueError(
                f"unknown skill '{name}'; available skills: {known}"
            )
        return skill

    def prompt_section(self) -> str:
        """Return the compact skill catalog placed in model context."""
        if not self._skills:
            return ""
        entries = "\n".join(
            f"- {skill.name}: {skill.description}"
            for skill in self._skills.values()
        )
        return (
            "## Available Skills\n\n"
            "Load a skill's instructions with `read_skill` when it is "
            "relevant. Only the catalog is provided initially; skill files "
            "are loaded progressively.\n\n"
            f"{entries}"
        )


def _load_skill(path: Path) -> tuple[Skill, bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"skill file must be UTF-8: {path}") from error
    front_matter = _front_matter(text, path)
    name = _required_string(front_matter, "name", path)
    description = _required_string(front_matter, "description", path)
    return (
        Skill(
            name=name,
            description=description,
            directory=path.parent.resolve(),
        ),
        _registration_enabled(front_matter.get("metadata"), path),
    )


def _front_matter(text: str, path: Path) -> dict[str, object]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"skill file is missing YAML front matter: {path}")
    end = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        ),
        None,
    )
    if end is None:
        raise ValueError(f"skill front matter is not closed: {path}")
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as error:
        raise ValueError(
            f"invalid skill front matter in {path}: {error}"
        ) from error
    if not isinstance(data, dict):
        raise ValueError(f"skill front matter must be an object: {path}")
    return data


def _required_string(
    data: dict[str, object],
    field: str,
    path: Path,
) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"skill field '{field}' must be a non-empty string: {path}"
        )
    return value.strip()


def _registration_enabled(metadata: object, path: Path) -> bool:
    if metadata is None:
        return True
    if not isinstance(metadata, dict):
        raise ValueError(f"skill field 'metadata' must be an object: {path}")
    nosis = metadata.get("nosis")
    if nosis is None:
        return True
    if not isinstance(nosis, dict):
        raise ValueError(
            f"skill field 'metadata.nosis' must be an object: {path}"
        )
    registered = nosis.get("register", True)
    if not isinstance(registered, bool):
        raise ValueError(
            "skill field 'metadata.nosis.register' must be a boolean: "
            f"{path}"
        )
    return registered

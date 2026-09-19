"""Skill sources, loading, and metadata exposed to Agent runtimes."""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import yaml


@dataclass(frozen=True)
class SkillLocation:
    """A source-owned ``SKILL.md`` location and its optional namespace."""

    path: Path
    namespace: str | None = None


class SkillSource(Protocol):
    """Locate skills without deciding how they are parsed or registered."""

    def skill_locations(self) -> Iterable[SkillLocation]: ...


@dataclass(frozen=True)
class DirectorySkillSource:
    """Standalone skills stored as direct children of one directory."""

    directory: Path
    namespace: str | None = None

    def skill_locations(self) -> Iterable[SkillLocation]:
        root = self.directory.expanduser().resolve()
        if not root.is_dir():
            return ()
        return tuple(
            SkillLocation(child / "SKILL.md", self.namespace)
            for child in sorted(root.iterdir(), key=lambda path: path.name)
            if child.is_dir() and (child / "SKILL.md").is_file()
        )


@dataclass(frozen=True)
class Skill:
    """One registered skill and the directory that owns its resources."""

    name: str
    description: str
    directory: Path
    namespace: str | None = None

    @property
    def identifier(self) -> str:
        if self.namespace is None:
            return self.name
        return f"{self.namespace}:{self.name}"


class SkillRegistry:
    """Parsed skills keyed by their externally visible identifier."""

    def __init__(
        self,
        skills: Iterable[Skill] = (),
        warnings: Iterable[str] = (),
    ) -> None:
        self._skills: dict[str, Skill] = {}
        self._warnings = tuple(warnings)
        for skill in skills:
            identifier = skill.identifier
            if identifier in self._skills:
                raise ValueError(
                    f"skill '{identifier}' is already registered"
                )
            self._skills[identifier] = skill

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
            f"- {skill.identifier}: {skill.description}"
            for skill in self._skills.values()
        )
        return (
            "## Available Skills\n\n"
            "Load a skill's instructions with `read_skill` when it is "
            "relevant. Only the catalog is provided initially; skill files "
            "are loaded progressively.\n\n"
            f"{entries}"
        )


class SkillLoader:
    """Parse Skill locations from any source into one registry."""

    def load(
        self,
        sources: Iterable[SkillSource],
        *,
        warnings: Iterable[str] = (),
    ) -> SkillRegistry:
        skills: dict[str, Skill] = {}
        load_warnings = list(warnings)
        for source in sources:
            for location in source.skill_locations():
                try:
                    skill, registered = _load_skill(location)
                except (OSError, ValueError) as error:
                    load_warnings.append(
                        f"Skipping skill at '{location.path}': {error}"
                    )
                    continue
                if not registered:
                    continue
                existing = skills.get(skill.identifier)
                if existing is not None:
                    load_warnings.append(
                        f"Skipping duplicate skill '{skill.identifier}' at "
                        f"'{skill.directory}'; already registered from "
                        f"'{existing.directory}'"
                    )
                    continue
                skills[skill.identifier] = skill
        return SkillRegistry(skills.values(), load_warnings)


def _load_skill(location: SkillLocation) -> tuple[Skill, bool]:
    path = location.path.expanduser().resolve()
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
            directory=path.parent,
            namespace=location.namespace,
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

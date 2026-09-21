"""Plugin name normalization and validation shared by the skill scripts."""

import re

# The loader accepts this pattern for `plugin.json` `name`.
PLUGIN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_PLUGIN_NAME_LENGTH = 64


def normalize_plugin_name(plugin_name: str) -> str:
    """Normalize a requested name to the lower-case hyphen-case used for new plugins."""
    normalized = re.sub(r"[^a-z0-9]+", "-", plugin_name.strip().lower())
    return re.sub(r"-{2,}", "-", normalized).strip("-")


def validate_plugin_name(plugin_name: str) -> None:
    """Reject names the plugin loader would refuse."""
    if PLUGIN_NAME_PATTERN.fullmatch(plugin_name) is None:
        raise ValueError(
            "plugin name may contain only letters, numbers, '.', '_' and '-', "
            "and must start with a letter or number"
        )


def display_name(plugin_name: str) -> str:
    return " ".join(
        part.capitalize() for part in re.split(r"[-_.]+", plugin_name) if part
    )

"""Path helpers shared by runtime containment checks."""

from pathlib import Path


def path_for_comparison(path: Path) -> Path:
    """Normalize Windows extended prefixes for path comparisons only."""
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)

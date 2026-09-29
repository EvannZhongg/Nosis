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


def is_path_within(path: Path, root: Path) -> bool:
    """Return whether a path stays inside a root after resolution."""
    return path_for_comparison(path.resolve()).is_relative_to(
        path_for_comparison(root)
    )


def relative_to_root(path: Path, root: Path) -> Path:
    """Return a comparison-normalized path relative to *root*."""
    return path_for_comparison(path).relative_to(path_for_comparison(root))

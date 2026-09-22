from fnmatch import fnmatchcase
from pathlib import Path


def matches_path_glob(path: Path, pattern: str) -> bool:
    """Match a complete relative path with ``**`` as recursive segments."""
    path_parts = path.as_posix().split("/")
    pattern_parts = pattern.replace("\\", "/").split("/")
    return _matches_parts(path_parts, pattern_parts, 0, 0)


def _matches_parts(
    path_parts: list[str],
    pattern_parts: list[str],
    path_index: int,
    pattern_index: int,
) -> bool:
    while pattern_index < len(pattern_parts):
        part = pattern_parts[pattern_index]
        if part == "**":
            while (
                pattern_index + 1 < len(pattern_parts)
                and pattern_parts[pattern_index + 1] == "**"
            ):
                pattern_index += 1
            if pattern_index + 1 == len(pattern_parts):
                return True
            return any(
                _matches_parts(
                    path_parts,
                    pattern_parts,
                    candidate,
                    pattern_index + 1,
                )
                for candidate in range(path_index, len(path_parts) + 1)
            )
        if path_index >= len(path_parts) or not fnmatchcase(
            path_parts[path_index], part
        ):
            return False
        path_index += 1
        pattern_index += 1
    return path_index == len(path_parts)

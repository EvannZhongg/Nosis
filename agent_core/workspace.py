from dataclasses import dataclass
from pathlib import Path

from .path_utils import path_for_comparison


@dataclass(frozen=True)
class Workspace:
    path: Path

    def __post_init__(self) -> None:
        resolved_path = self.path.expanduser().resolve()
        if not resolved_path.is_dir():
            raise ValueError(
                f"workspace must be an existing directory: {resolved_path}"
            )
        object.__setattr__(self, "path", resolved_path)

    def resolve_path(self, relative_path: str) -> Path:
        path = Path(relative_path)
        if path.is_absolute():
            raise ValueError("workspace path must be relative")

        resolved_path = (self.path / path).resolve()
        try:
            path_for_comparison(resolved_path).relative_to(
                path_for_comparison(self.path)
            )
        except ValueError as error:
            raise ValueError(
                "workspace path must stay within the workspace"
            ) from error
        return resolved_path

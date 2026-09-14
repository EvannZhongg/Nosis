import json
from pathlib import Path
from urllib.parse import quote

from .session_paths import session_directory, workspace_key
from .session_paths import default_sessions_directory
from .tools import ToolResult
from .workspace import Workspace


DEFAULT_MAX_TOOL_RESULT_CHARS = 16 * 1024
DEFAULT_TOOL_RESULT_PREVIEW_CHARS = 1200


class ToolResultNormalizer:
    def __init__(
        self,
        workspace: Workspace,
        session_id: str,
        max_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
        preview_chars: int = DEFAULT_TOOL_RESULT_PREVIEW_CHARS,
        sessions_directory: Path | None = None,
    ) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be a positive integer")
        if preview_chars < 1:
            raise ValueError("preview_chars must be a positive integer")

        self._workspace = workspace
        self._session_id = session_id
        self._max_chars = max_chars
        self._preview_chars = preview_chars
        self._sessions_directory = (
            sessions_directory or default_sessions_directory()
        ).expanduser().resolve()
        session_directory(
            self._sessions_directory,
            self._workspace.path,
            self._session_id,
        )

    def normalize(self, result: ToolResult) -> str:
        content = result.to_content()
        size_chars = len(content)
        artifact_path = (
            Path(".nosis", "sessions")
            / workspace_key(self._workspace.path)
            / self._session_id
            / f"{quote(result.tool_call_id, safe='')}.txt"
        )
        # A spooled result is already known to be larger than the inline
        # budget.  Let its writer stream the complete payload directly from
        # disk instead of materializing it in memory again.
        if result.artifact_writer is not None:
            try:
                absolute_path = self._absolute_artifact_path(artifact_path)
                absolute_path.parent.mkdir(parents=True, exist_ok=True)
                size_chars = result.artifact_writer(absolute_path)
            finally:
                if result.artifact_cleanup is not None:
                    result.artifact_cleanup()
        elif size_chars > self._max_chars:
            try:
                absolute_path = self._absolute_artifact_path(artifact_path)
                absolute_path.parent.mkdir(parents=True, exist_ok=True)
                absolute_path.write_text(content, encoding="utf-8")
            finally:
                if result.artifact_cleanup is not None:
                    result.artifact_cleanup()
        else:
            if result.artifact_cleanup is not None:
                result.artifact_cleanup()
            return content

        relative_path = artifact_path.as_posix()
        return json.dumps(
            {
                "artifact_path": relative_path,
                "size_chars": size_chars,
                "preview": content[: self._preview_chars],
                "read_instruction": (
                    "Use read_file with path "
                    f"'{relative_path}' to read the complete tool result."
                ),
            },
            ensure_ascii=False,
        )

    def _absolute_artifact_path(self, artifact_path: Path) -> Path:
        absolute_path = (
            session_directory(
                self._sessions_directory,
                self._workspace.path,
                self._session_id,
            )
            / artifact_path.name
        ).resolve()
        try:
            absolute_path.relative_to(self._sessions_directory)
        except ValueError as error:
            raise ValueError(
                "session artifact path must stay within sessions"
            ) from error
        return absolute_path

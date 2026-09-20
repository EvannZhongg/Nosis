import json
from pathlib import Path
from urllib.parse import quote

from .path_utils import path_for_comparison
from .session_paths import default_sessions_directory, session_directory
from .tools import ToolResult
from .tools.budget import MAX_TOOL_RESULT_CHARS
from .workspace import Workspace


DEFAULT_TOOL_RESULT_PREVIEW_CHARS = 1200


def artifact_body(result: ToolResult) -> str:
    """Render *result* the way it is stored on disk.

    An artifact is written to be read back by ``read_file``, line by
    line, not to be parsed again.  So it holds the payload itself rather
    than the ``{"ok": ...}`` envelope: wrapping it would turn a report's
    newlines into literal ``\\n`` and collapse the whole file onto one
    line, which no amount of paging can walk through.
    """
    if result.error is not None:
        return (
            f"Tool '{result.name}' failed.\n"
            f"error_type: {result.error.type}\n\n"
            f"{result.error.message}"
        )
    output = result.output
    if isinstance(output, str):
        return output
    # Indented so a structured payload still has the line breaks that
    # paging needs to advance through it.
    return json.dumps(output, ensure_ascii=False, indent=2)


class ToolResultNormalizer:
    def __init__(
        self,
        workspace: Workspace,
        session_id: str,
        max_chars: int = MAX_TOOL_RESULT_CHARS,
        preview_chars: int = DEFAULT_TOOL_RESULT_PREVIEW_CHARS,
        sessions_directory: Path | None = None,
        artifact_directory: Path | None = None,
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
        default_artifact_directory = session_directory(
            self._sessions_directory,
            self._workspace.path,
            self._session_id,
        )
        self._artifact_directory = (
            artifact_directory or default_artifact_directory
        ).expanduser().resolve()
        try:
            artifact_relative = path_for_comparison(
                self._artifact_directory
            ).relative_to(
                path_for_comparison(self._sessions_directory)
            )
        except ValueError as error:
            raise ValueError(
                "session artifact path must stay within sessions"
            ) from error
        self._artifact_path_prefix = (
            Path(".nosis", "sessions") / artifact_relative
        )

    def normalize(self, result: ToolResult) -> str:
        content = result.to_content()
        # What decides spilling is the envelope, because that is what
        # would enter the model's context.  Rendering the payload is
        # only worth doing once that spill is certain: the common case
        # returns the envelope and never touches the payload again.
        spooled = result.artifact_writer is not None
        if not spooled and len(content) <= self._max_chars:
            if result.artifact_cleanup is not None:
                result.artifact_cleanup()
            return content

        # What gets written is the payload, because that is what a
        # reader needs back.
        body = artifact_body(result)
        size_chars = len(body)
        artifact_path = (
            self._artifact_path_prefix
            / f"{quote(result.tool_call_id, safe='')}.txt"
        )
        try:
            absolute_path = self._absolute_artifact_path(artifact_path)
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            if spooled:
                # A spooled result is already known to be larger than the
                # inline budget.  Let its writer stream the complete
                # payload directly from disk instead of materializing it
                # in memory again.
                size_chars = result.artifact_writer(absolute_path)
            else:
                absolute_path.write_text(body, encoding="utf-8")
        finally:
            if result.artifact_cleanup is not None:
                result.artifact_cleanup()

        relative_path = artifact_path.as_posix()
        return json.dumps(
            {
                "artifact_path": relative_path,
                "size_chars": size_chars,
                "preview": body[: self._preview_chars],
                "read_instruction": (
                    "Use read_file with path "
                    f"'{relative_path}' to read the complete tool result. "
                    "It holds the raw result text, so page through it "
                    "with 'offset' when one read does not reach the end."
                ),
            },
            ensure_ascii=False,
        )

    def _absolute_artifact_path(self, artifact_path: Path) -> Path:
        absolute_path = (
            self._artifact_directory / artifact_path.name
        ).resolve()
        try:
            path_for_comparison(absolute_path).relative_to(
                path_for_comparison(self._sessions_directory)
            )
        except ValueError as error:
            raise ValueError(
                "session artifact path must stay within sessions"
            ) from error
        return absolute_path

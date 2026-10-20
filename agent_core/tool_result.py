import json
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from urllib.parse import quote

from .path_utils import path_for_comparison
from .session_paths import default_sessions_directory, session_directory
from .tools import JSONValue, ToolResult
from .tools.budget import MAX_TOOL_RESULT_CHARS
from .tools.text import write_text_artifact
from .workspace import Workspace


DEFAULT_TOOL_RESULT_PREVIEW_CHARS = 1200


def artifact_body(result: ToolResult) -> str:
    """Render a reading view, retaining nested text without JSON escaping."""
    if result.error is not None:
        return (
            f"Tool '{result.name}' failed.\n"
            f"error_type: {result.error.type}\n\n"
            f"{result.error.message}"
        )
    output = result.output
    if isinstance(output, str):
        return output
    return "\n\n".join(_artifact_sections(output))


def _artifact_sections(value: JSONValue, path: str = "$") -> Iterator[str]:
    if isinstance(value, dict) and value:
        for key, item in value.items():
            field = json.dumps(key, ensure_ascii=False)
            yield from _artifact_sections(item, f"{path}[{field}]")
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from _artifact_sections(item, f"{path}[{index}]")
    else:
        is_text = isinstance(value, str)
        kind = "text" if is_text else "json"
        body = value if is_text else json.dumps(value, ensure_ascii=False)
        yield f"--- {path} ({kind}) ---\n{body}"


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
        has_writer = result.artifact_writer is not None
        if not has_writer and len(content) <= self._max_chars:
            if result.artifact_cleanup is not None:
                result.artifact_cleanup()
            return content

        body = None
        artifact_path = (
            self._artifact_path_prefix
            / f"{quote(result.tool_call_id, safe='')}.txt"
        )
        try:
            absolute_path = self._absolute_artifact_path(artifact_path)
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            if has_writer:
                # Spool writers share the same bounded reading view as
                # ordinary results, without loading their streams in memory.
                source_path = absolute_path.with_suffix(".source")
                try:
                    result.artifact_writer(source_path)
                    with source_path.open(
                        "r", encoding="utf-8", newline=None
                    ) as source:
                        size_chars = write_text_artifact(absolute_path, source)
                finally:
                    source_path.unlink(missing_ok=True)
            else:
                body = artifact_body(result)
                size_chars = write_text_artifact(
                    absolute_path, StringIO(body, newline=None)
                )

            relative_path = artifact_path.as_posix()
            summary: dict[str, JSONValue] = {
                "artifact_path": relative_path,
                "size_chars": size_chars,
                "preview": None,
                "read_instruction": (
                    "Use read_file with path "
                    f"'{relative_path}' to read the complete tool result. "
                    "It is a reading view with long lines split; page "
                    "through it with 'offset' when one read does not reach the end."
                ),
            }
            if result.artifact_preview is not None:
                # JSON null occupies four characters in the empty summary.
                json_budget = (
                    self._max_chars
                    - len(json.dumps(summary, ensure_ascii=False))
                    + 4
                )
                summary["preview"] = result.artifact_preview(json_budget)
            else:
                if body is None:
                    body = artifact_body(result)
                summary["preview"] = body[: self._preview_chars]
            normalized = json.dumps(summary, ensure_ascii=False)
            if (
                result.artifact_preview is not None
                and len(normalized) > self._max_chars
            ):
                preview_chars = len(json.dumps(summary["preview"], ensure_ascii=False))
                raise ValueError(
                    f"artifact_preview for tool '{result.name}' exceeds its JSON "
                    f"budget: serialized preview has {preview_chars} characters, "
                    f"allowed {json_budget}; normalized result has "
                    f"{len(normalized)} characters, limit {self._max_chars}"
                )
            return normalized
        finally:
            if result.artifact_cleanup is not None:
                result.artifact_cleanup()

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

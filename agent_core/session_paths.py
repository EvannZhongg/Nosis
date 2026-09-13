from pathlib import Path
from urllib.parse import quote


def default_sessions_directory() -> Path:
    """Return the user-wide Session root used by library callers."""
    return Path.home() / ".nosis" / "sessions"


def workspace_key(workspace: Path | str) -> str:
    """Return a filesystem-safe stable key for an absolute workspace path."""
    return quote(str(Path(workspace).expanduser().resolve()), safe="")


def workspace_directory(sessions_directory: Path, workspace: Path | str) -> Path:
    return sessions_directory / workspace_key(workspace)


def session_directory(
    sessions_directory: Path,
    workspace: Path | str,
    session_id: str,
) -> Path:
    _validate_session_id(session_id)
    return workspace_directory(sessions_directory, workspace) / session_id


def session_log_path(
    sessions_directory: Path,
    workspace: Path | str,
    session_id: str,
) -> Path:
    return session_directory(sessions_directory, workspace, session_id) / (
        f"{session_id}.jsonl"
    )


def _validate_session_id(session_id: str) -> None:
    if (
        not session_id
        or session_id in {".", ".."}
        or "/" in session_id
        or "\\" in session_id
    ):
        raise ValueError(f"Invalid session id: {session_id!r}")

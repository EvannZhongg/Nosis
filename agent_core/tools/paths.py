"""Path resolution shared by the tools that read runtime files.

Two roots are readable: the workspace, and the Runtime's Session root.
Session artifacts are addressed with a ``.nosis/sessions/`` prefix even
though they live outside the workspace, so one resolver serves both and
every tool enforces the same containment rules.
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..content import ImagePart
from ..media import ImageInfo, probe_image
from ..path_utils import relative_to_root
from .context import ToolExecutionContext
from .files import BoundFile, bind_file


SESSION_MARKER = Path(".nosis", "sessions")


def resolve_readable_path(
    path: str,
    context: ToolExecutionContext,
) -> tuple[Path, str]:
    """Resolve a workspace or Session artifact path for display."""
    _root, resolved, display = _resolve_readable_location(path, context)
    return resolved, display


@contextmanager
def bind_readable_file(
    path: str, context: ToolExecutionContext
) -> Iterator[tuple[BoundFile, str]]:
    root, resolved, display = _resolve_readable_location(path, context)
    with bind_file(root, relative_to_root(resolved, root)) as file:
        yield file, display


def _resolve_readable_location(
    path: str, context: ToolExecutionContext
) -> tuple[Path, Path, str]:
    relative = Path(path)
    if relative == SESSION_MARKER or SESSION_MARKER in relative.parents:
        sessions_directory = context.sessions_directory
        resolved = (
            sessions_directory / relative.relative_to(SESSION_MARKER)
        ).resolve()
        try:
            relative_to_root(resolved, sessions_directory)
        except ValueError as error:
            raise ValueError(
                "session artifact path must stay within sessions"
            ) from error
        return sessions_directory, resolved, relative.as_posix()
    workspace = context.workspace
    resolved = workspace.resolve_path(path)
    return workspace.path, resolved, relative_to_root(resolved, workspace.path).as_posix()


def resolve_image(
    path: str,
    context: ToolExecutionContext,
) -> tuple[ImagePart, ImageInfo]:
    """Resolve *path* to an :class:`ImagePart` ready to send inline.

    The media type comes from the file's own bytes rather than its
    extension, because a provider that is handed ``image/png`` wrapping
    JPEG data rejects the whole request.

    The stored path stays workspace-relative when the image lives in the
    workspace, which keeps a session log portable between machines.  A
    Session artifact has no workspace-relative form -- resolving one
    against the workspace root would point at a file that does not exist
    -- so it is stored absolute.
    """
    resolved, _display = resolve_readable_path(path, context)
    info = probe_image(resolved)
    try:
        stored = relative_to_root(resolved, context.workspace.path).as_posix()
    except ValueError:
        stored = str(resolved)
    return ImagePart(
        path=stored,
        mime_type=info.mime_type,
        filename=resolved.name,
        size_bytes=info.size_bytes,
    ), info

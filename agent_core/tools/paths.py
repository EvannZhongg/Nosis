"""Path resolution shared by the tools that read runtime files.

Two roots are readable: the workspace, and the Runtime's Session root.
Session artifacts are addressed with a ``.nosis/sessions/`` prefix even
though they live outside the workspace, so one resolver serves both and
every tool enforces the same containment rules.
"""

from pathlib import Path

from ..content import ImagePart
from ..media import ImageInfo, probe_image
from .context import ToolExecutionContext


SESSION_MARKER = Path(".nosis", "sessions")


def resolve_readable_path(
    path: str,
    context: ToolExecutionContext,
) -> tuple[Path, str]:
    """Resolve *path* to a real file location plus its display form.

    A ``.nosis/sessions/...`` path is resolved against the Session root;
    anything else is a workspace-relative path.  Both branches reject a
    result that escapes its root, so a tool never has to repeat the
    check.
    """
    relative = Path(path)
    if relative == SESSION_MARKER or SESSION_MARKER in relative.parents:
        sessions_directory = context.sessions_directory
        resolved = (
            sessions_directory / relative.relative_to(SESSION_MARKER)
        ).resolve()
        try:
            resolved.relative_to(sessions_directory)
        except ValueError as error:
            raise ValueError(
                "session artifact path must stay within sessions"
            ) from error
        return resolved, relative.as_posix()
    workspace = context.workspace
    resolved = workspace.resolve_path(path)
    return resolved, resolved.relative_to(workspace.path).as_posix()


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
        stored = resolved.relative_to(context.workspace.path).as_posix()
    except ValueError:
        stored = str(resolved)
    return ImagePart(path=stored, mime_type=info.mime_type), info

"""Resolve incoming attachment metadata against the bound workspace."""

from agent_core import AttachmentPart, FilePart, ImagePart, UnsupportedImageError, Workspace, probe_image
from agent_core.path_utils import path_for_comparison


def parse_attachments(
    value: object,
    workspace: Workspace,
) -> tuple[AttachmentPart, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("user_turn.attachments must be an array")
    result = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") not in {"image", "file"}:
            raise ValueError("attachments must contain image or file objects")
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("attachment path must be a non-empty string")
        resolved = workspace.resolve_path(path)
        if not resolved.is_file():
            raise ValueError(f"attachment does not exist: {path}")
        filename = item.get("filename")
        mime_type = item.get("mime_type")
        if not isinstance(filename, str) or not filename:
            raise ValueError("attachment filename must be a non-empty string")
        if not isinstance(mime_type, str) or not mime_type:
            raise ValueError("attachment mime_type must be a non-empty string")
        stored_path = path_for_comparison(resolved).relative_to(
            path_for_comparison(workspace.path)
        ).as_posix()
        try:
            image_info = probe_image(resolved, max_bytes=None)
        except UnsupportedImageError:
            image_info = None
        if image_info is not None:
            info = probe_image(resolved)
            result.append(
                ImagePart(
                    path=stored_path,
                    mime_type=info.mime_type,
                    filename=filename,
                    size_bytes=info.size_bytes,
                )
            )
        else:
            result.append(
                FilePart(
                    path=stored_path,
                    filename=filename,
                    mime_type=mime_type,
                    size_bytes=resolved.stat().st_size,
                )
            )
    return tuple(result)

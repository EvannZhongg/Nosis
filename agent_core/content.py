"""Provider-neutral message parts.

Attachments are represented by workspace paths until a provider request is
constructed. This keeps session logs small and leaves file interpretation to
the Agent and its Tools.
"""

from dataclasses import dataclass, field
from typing import Literal, TypeAlias


@dataclass(frozen=True)
class TextPart:
    text: str = ""
    type: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True)
class ImagePart:
    path: str = ""
    mime_type: str = "image/png"
    filename: str = ""
    size_bytes: int = 0
    type: Literal["image"] = field(default="image", init=False)


@dataclass(frozen=True)
class FilePart:
    path: str = ""
    filename: str = ""
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    type: Literal["file"] = field(default="file", init=False)


AttachmentPart: TypeAlias = ImagePart | FilePart
ContentPart: TypeAlias = TextPart | AttachmentPart
Content: TypeAlias = str | tuple[ContentPart, ...] | None


def content_parts(content: Content) -> tuple[ContentPart, ...]:
    if content is None:
        return ()
    if isinstance(content, str):
        return (TextPart(text=content),)
    return content


def text_content(content: Content) -> str | None:
    parts = content_parts(content)
    if not parts:
        return None
    text = "".join(part.text for part in parts if isinstance(part, TextPart))
    return text or None


def historical_content(content: Content) -> str | None:
    """Return text suitable for old turns and context compression.

    Media is intentionally omitted: historical context must not cause the
    same image bytes to be uploaded again on every model call.
    """
    parts = content_parts(content)
    text = text_content(content)
    has_image = any(isinstance(part, ImagePart) for part in parts)
    files = [part for part in parts if isinstance(part, FilePart)]
    sections = [text] if text else []
    if text and has_image:
        sections.append("[Image attachment omitted from historical context]")
    elif has_image:
        sections.append("[Image attachment omitted from historical context]")
    if files:
        sections.append(
            "Attached files:\n"
            + "\n".join(
                f"- {part.filename} ({part.path}, {part.mime_type}, "
                f"{part.size_bytes} bytes)"
                for part in files
            )
        )
    return "\n".join(sections) or None

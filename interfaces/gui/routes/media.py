"""GUI attachment and signed image endpoints."""

import base64
import hashlib
import hmac
import json
import mimetypes
import secrets
import shutil
import time
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from agent_core import UnsupportedImageError, Workspace, image_extension, probe_image
from agent_core.path_utils import path_for_comparison

from ..active_session import ActiveSessionRegistry
from .workspace import WorkspaceResolver


IMAGE_URL_TTL_SECONDS = 5 * 60


def create_media_router(
    resolver: WorkspaceResolver,
    active_sessions: ActiveSessionRegistry,
) -> APIRouter:
    router = APIRouter()
    image_url_secret = secrets.token_bytes(32)

    async def signed_image_workspace(session_id: str | None) -> Workspace:
        if session_id is None:
            return resolver.default_workspace
        active = await active_sessions.get(session_id)
        if active is not None:
            try:
                return Workspace(Path(active.projection.workspace))
            except (OSError, ValueError) as error:
                raise HTTPException(
                    status_code=404,
                    detail="图片不存在。",
                ) from error
        bound = resolver.store.workspace_for(session_id)
        if bound is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        try:
            return Workspace(Path(bound))
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=404, detail="图片不存在。") from error

    def resolve_image_path(
        current_workspace: Workspace,
        path: str,
    ) -> tuple[Path, str]:
        try:
            resolved = current_workspace.resolve_path(path)
            info = probe_image(resolved, max_bytes=None)
        except (UnsupportedImageError, OSError, ValueError) as error:
            raise HTTPException(status_code=404, detail="图片不存在。") from error
        return resolved, info.mime_type

    def sign_image_path(
        current_workspace: Workspace,
        path: str,
        session_id: str | None,
    ) -> tuple[str, int]:
        expires_at = int(time.time()) + IMAGE_URL_TTL_SECONDS
        payload = json.dumps(
            {
                "session_id": session_id,
                "path": path,
                "workspace": hashlib.sha256(
                    str(current_workspace.path).encode("utf-8")
                ).hexdigest(),
                "expires_at": expires_at,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(
            image_url_secret,
            encoded,
            hashlib.sha256,
        ).digest()
        token = (
            encoded.decode("ascii")
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )
        return f"/api/images/{token}", expires_at

    async def decode_image_token(token: str) -> tuple[Workspace, str, int]:
        try:
            encoded, encoded_signature = token.split(".", 1)
            payload_bytes = encoded.encode("ascii")
            signature = base64.urlsafe_b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4)
            )
        except ValueError as error:
            raise HTTPException(
                status_code=404,
                detail="图片链接无效。",
            ) from error
        expected = hmac.new(
            image_url_secret,
            payload_bytes,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=404, detail="图片链接无效。")
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(
                    encoded + "=" * (-len(encoded) % 4)
                )
            )
        except ValueError as error:
            raise HTTPException(
                status_code=404,
                detail="图片链接无效。",
            ) from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=404, detail="图片链接无效。")
        path = payload.get("path")
        session_id = payload.get("session_id")
        workspace_digest = payload.get("workspace")
        expires_at = payload.get("expires_at")
        if (
            not isinstance(path, str)
            or not path
            or (session_id is not None and not isinstance(session_id, str))
            or not isinstance(workspace_digest, str)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
        ):
            raise HTTPException(status_code=404, detail="图片链接无效。")
        if expires_at <= int(time.time()):
            raise HTTPException(status_code=410, detail="图片链接已过期。")
        current_workspace = await signed_image_workspace(session_id)
        current_digest = hashlib.sha256(
            str(current_workspace.path).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(workspace_digest, current_digest):
            raise HTTPException(status_code=404, detail="图片链接无效。")
        return current_workspace, path, expires_at

    @router.post("/api/attachments")
    async def upload_attachments(
        files: list[UploadFile] = File(...),
        session_id: str | None = None,
    ) -> dict[str, object]:
        """Persist browser files as workspace-relative attachment paths."""
        current_workspace = resolver.for_session(session_id)
        attachment_root = (
            current_workspace.path / ".nosis" / "attachments"
        ).resolve()
        attachment_root.mkdir(parents=True, exist_ok=True)
        attachments = []
        for upload in files:
            path = attachment_root / uuid4().hex
            try:
                with path.open("wb") as target:
                    shutil.copyfileobj(upload.file, target)
            except OSError as error:
                path.unlink(missing_ok=True)
                raise HTTPException(status_code=500, detail=str(error)) from error
            filename = Path(upload.filename or "attachment").name
            try:
                info = probe_image(path, max_bytes=None)
            except UnsupportedImageError:
                info = None
            suffix = (
                image_extension(info.mime_type)
                if info is not None
                else Path(filename).suffix
            )
            named = path.with_name(path.name + suffix)
            path.replace(named)
            mime_type = (
                info.mime_type
                if info is not None
                else mimetypes.guess_type(filename)[0]
                or upload.content_type
                or "application/octet-stream"
            )
            attachments.append(
                {
                    "type": "image" if info is not None else "file",
                    "path": f".nosis/attachments/{named.name}",
                    "filename": filename,
                    "mime_type": mime_type,
                    "size_bytes": named.stat().st_size,
                }
            )
        return {"attachments": attachments}

    @router.post("/api/image-url")
    async def create_image_url(payload: object = Body(...)) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求必须是对象。")
        path = payload.get("path")
        session_id = payload.get("session_id")
        if not isinstance(path, str) or not path:
            raise HTTPException(status_code=400, detail="path 不能为空。")
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id
        ):
            raise HTTPException(status_code=400, detail="session_id 无效。")
        current_workspace = await signed_image_workspace(session_id)
        resolve_image_path(current_workspace, path)
        url, expires_at = sign_image_path(current_workspace, path, session_id)
        return {"url": url, "expires_at": expires_at}

    @router.get("/api/images/{token}")
    async def get_signed_image(token: str) -> FileResponse:
        current_workspace, path, expires_at = await decode_image_token(token)
        resolved, mime_type = resolve_image_path(current_workspace, path)
        return FileResponse(
            resolved,
            media_type=mime_type,
            headers={
                "Cache-Control": (
                    "private, max-age="
                    f"{max(0, expires_at - int(time.time()))}"
                )
            },
        )

    @router.get("/api/attachments/{filename}")
    def get_attachment(
        filename: str,
        session_id: str | None = None,
        download_name: str | None = None,
    ) -> FileResponse:
        attachment_root = (
            resolver.for_session(session_id).path / ".nosis" / "attachments"
        ).resolve()
        path = (attachment_root / filename).resolve()
        try:
            path_for_comparison(path).relative_to(
                path_for_comparison(attachment_root)
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail="附件不存在。") from error
        if not path.is_file():
            raise HTTPException(status_code=404, detail="附件不存在。")
        try:
            probe_image(path, max_bytes=None)
        except UnsupportedImageError:
            return FileResponse(
                path,
                media_type="application/octet-stream",
                filename=Path(download_name or filename).name,
            )
        raise HTTPException(status_code=404, detail="请使用签名图片链接。")

    return router

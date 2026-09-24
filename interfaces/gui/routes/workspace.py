"""Workspace selection, creation, and read-only browsing endpoints."""

from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from agent_core import (
    JsonlSessionStore,
    ListDirectoryTool,
    Session,
    ToolExecutionContext,
    Workspace,
)
from agent_runtime.config import load_scratch_workspace_root
from agent_runtime.settings import SettingsStore
from interfaces.bridge.managed_workspaces import create_scratch_workspace


class WorkspaceResolver:
    def __init__(
        self,
        default_workspace: Workspace,
        store: JsonlSessionStore,
    ) -> None:
        self.default_workspace = default_workspace
        self.store = store

    def for_session(self, session_id: str | None) -> Workspace:
        """Resolve a stored Session workspace, falling back for new Sessions."""
        if session_id:
            bound = self.store.workspace_for(session_id)
            if bound:
                try:
                    return Workspace(Path(bound))
                except (OSError, ValueError) as error:
                    raise HTTPException(
                        status_code=400,
                        detail=str(error),
                    ) from error
        return self.default_workspace


def create_workspace_router(
    resolver: WorkspaceResolver,
    settings: SettingsStore,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/select-workspace")
    def select_workspace() -> dict[str, str | None]:
        """Open a native folder picker for the local GUI server."""
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(title="选择工作区")
            root.destroy()
        except Exception as error:
            raise HTTPException(
                status_code=500,
                detail=f"无法打开文件夹选择器：{error}",
            ) from error
        return {"workspace": selected or None}

    @router.post("/api/workspaces/scratch")
    def create_scratch(payload: object = Body(...)) -> dict[str, str]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求必须是对象。")
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise HTTPException(status_code=400, detail="session_id 不能为空。")
        try:
            scratch = create_scratch_workspace(
                load_scratch_workspace_root(settings.agent_path),
                session_id,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"workspace": str(scratch.path)}

    @router.get("/api/workspace")
    def get_workspace(
        path: str = ".",
        cursor: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        current_workspace = resolver.for_session(session_id)
        try:
            arguments = {"path": path}
            if cursor is not None:
                arguments["cursor"] = cursor
            listing = ListDirectoryTool().execute(
                arguments,
                ToolExecutionContext(
                    workspace=current_workspace,
                    session=Session(),
                ),
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"root": str(current_workspace.path), **listing}

    return router

"""GUI settings, model, and memory endpoints."""

from fastapi import APIRouter, Body, HTTPException

from agent_core import MemoryDocument
from agent_runtime.config import memory_store
from agent_runtime.settings import SettingsStore


def create_settings_router(settings: SettingsStore) -> APIRouter:
    router = APIRouter()

    @router.get("/api/models")
    def list_models() -> dict[str, object]:
        default_provider, models = settings.model_options()
        return {
            "default": default_provider,
            "models": [
                {"id": name, "model": model}
                for name, model in models.items()
            ],
        }

    @router.get("/api/settings")
    def get_settings() -> dict[str, object]:
        try:
            return settings.snapshot()
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.get("/api/memory")
    def get_memory() -> dict[str, object]:
        try:
            global_memory, workspaces = memory_store(settings.directory).load_all()
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "global": _memory_document_to_dict(global_memory),
            "workspaces": [
                {
                    "workspace": workspace_path,
                    "memory": _memory_document_to_dict(document),
                }
                for workspace_path, document in sorted(workspaces.items())
            ],
        }

    @router.put("/api/settings/providers/{provider_id}")
    def save_provider_settings(
        provider_id: str,
        payload: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            revision = payload.pop("expected_revision", None)
            return settings.save_provider(
                provider_id,
                payload,
                revision if isinstance(revision, str) else None,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.put("/api/settings/agent")
    def save_agent_settings(
        payload: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            revision = payload.pop("expected_revision", None)
            return settings.save_agent(
                payload,
                revision if isinstance(revision, str) else None,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.put("/api/settings/routing")
    def save_routing_settings(
        payload: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            revision = payload.pop("expected_revision", None)
            return settings.save_routing(
                payload,
                revision if isinstance(revision, str) else None,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    return router


def _memory_document_to_dict(
    document: MemoryDocument,
) -> dict[str, list[str]]:
    return {
        "preferences": list(document.preferences),
        "facts": list(document.facts),
        "decisions": list(document.decisions),
    }

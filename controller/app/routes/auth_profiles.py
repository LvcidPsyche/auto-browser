from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..models import ImportAuthProfileRequest, SaveAuthProfileRequest, SaveStorageStateRequest
from ._utils import require_safe_segment, typed_route_errors

logger = logging.getLogger(__name__)


def create_auth_profiles_router(*, manager: Any, settings: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/sessions/{session_id}/auth-state")
    async def get_session_auth_state(session_id: str) -> dict[str, Any]:
        return await manager.get_auth_state_info(session_id)

    @router.get("/auth-profiles")
    async def list_auth_profiles() -> list[dict[str, Any]]:
        return await manager.list_auth_profiles()

    @router.get("/auth-profiles/{profile_name}")
    async def get_auth_profile(profile_name: str) -> dict[str, Any]:
        try:
            return await manager.get_auth_profile(profile_name)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request") from None
        except PermissionError:
            raise HTTPException(status_code=403, detail="Not permitted") from None

    @router.post("/sessions/{session_id}/storage-state")
    async def save_storage_state(session_id: str, payload: SaveStorageStateRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "save storage state failed for session %s", session_id):
            return await manager.save_storage_state(session_id, payload.path)

    @router.post("/sessions/{session_id}/auth-profiles")
    async def save_auth_profile(session_id: str, payload: SaveAuthProfileRequest) -> dict[str, Any]:
        try:
            return await manager.save_auth_profile(session_id, payload.profile_name)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request") from None
        except PermissionError:
            raise HTTPException(status_code=403, detail="Not permitted") from None

    @router.get("/auth-profiles/{profile_name}/export")
    async def export_auth_profile(profile_name: str):
        safe_profile_name = require_safe_segment(profile_name, field="profile_name")
        with typed_route_errors(logger, "auth profile export failed for profile %s", profile_name):
            result = await manager.export_auth_profile(safe_profile_name)

        auth_root = Path(settings.auth_root).resolve()
        archive_name = str(result["archive_name"])
        archive_path: Path | None = None
        if auth_root.is_dir():
            for child in auth_root.iterdir():
                if child.name == archive_name and child.is_file():
                    candidate = child.resolve()
                    if candidate.is_relative_to(auth_root):
                        archive_path = candidate
                    break
        if archive_path is None:
            raise HTTPException(status_code=500, detail="archive file not found after export")

        return FileResponse(
            path=str(archive_path),
            media_type="application/gzip",
            filename=archive_path.name,
        )

    @router.post("/auth-profiles/import")
    async def import_auth_profile(payload: ImportAuthProfileRequest) -> dict[str, Any]:
        with typed_route_errors(logger, "auth profile import failed"):
            try:
                return await manager.import_auth_profile(payload.archive_path, overwrite=payload.overwrite)
            except FileExistsError:
                raise HTTPException(status_code=409, detail="Conflict") from None

    return router

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..models import ImportAuthProfileRequest, SaveAuthProfileRequest, SaveStorageStateRequest
from ._utils import internal_error, require_safe_segment

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
        try:
            return await manager.save_storage_state(session_id, payload.path)
        except PermissionError:
            raise HTTPException(status_code=403, detail="Not permitted") from None

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
        try:
            result = await manager.export_auth_profile(safe_profile_name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Not found") from None
        except PermissionError:
            # Must precede the catch-all below, which would otherwise turn a
            # refusal to hand over someone else's cookies into a 500.
            raise HTTPException(status_code=403, detail="Not permitted") from None
        except Exception:
            raise internal_error(logger, "auth profile export failed for profile %s", profile_name) from None

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
        try:
            return await manager.import_auth_profile(payload.archive_path, overwrite=payload.overwrite)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Not found") from None
        except FileExistsError:
            raise HTTPException(status_code=409, detail="Conflict") from None
        except Exception:
            raise internal_error(logger, "auth profile import failed") from None

    return router

"""Pillar 3 — Network inspector routes (/sessions/{session_id}/network)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

network_router = APIRouter(prefix="/sessions/{session_id}/network", tags=["network"])


@network_router.get("/requests")
async def network_get_requests(
    session_id: str,
    request: Request,
    url_filter: str = "",
    method: str = "",
    resource_type: str = "",
    limit: int = 50,
):
    inspector = _get_inspector(request.app, session_id)
    if inspector is None:
        raise HTTPException(404, f"No network inspector for session {session_id!r}")
    entries = inspector.entries(limit=limit, method=method or None, url_contains=url_filter or None)
    if resource_type:
        entries = [entry for entry in entries if entry.get("resource_type") == resource_type]
    return {"requests": entries, "summary": inspector.summary()}


@network_router.post("/hooks")
async def network_register_hook(session_id: str, body: dict[str, Any], request: Request):
    inspector = _get_inspector(request.app, session_id)
    if inspector is None:
        raise HTTPException(404, f"No network inspector for session {session_id!r}")
    pattern = body.get("url_pattern", "")
    if not pattern:
        raise HTTPException(422, "url_pattern required")
    # Hooks via API are logged-only (no external callback for security)

    async def _log_hook(req: dict):
        logger.info("network.hook pattern=%r matched url=%r", pattern, req.get("url"))

    inspector.register_hook(pattern, _log_hook)
    return {"status": "registered", "pattern": pattern}


@network_router.get("/hooks")
async def network_list_hooks(session_id: str, request: Request):
    inspector = _get_inspector(request.app, session_id)
    if inspector is None:
        raise HTTPException(404, f"No network inspector for session {session_id!r}")
    return {"hooks": inspector.list_hooks()}


@network_router.delete("/hooks/{pattern}")
async def network_remove_hook(session_id: str, pattern: str, request: Request):
    inspector = _get_inspector(request.app, session_id)
    if inspector is None:
        raise HTTPException(404, f"No network inspector for session {session_id!r}")
    removed = inspector.remove_hook(pattern)
    return {"removed": removed, "pattern": pattern}


def _get_inspector(app, session_id: str):
    inspectors = getattr(app.state, "network_inspectors", {})
    inspector = inspectors.get(session_id)
    if inspector is not None:
        return inspector
    manager = getattr(app.state, "browser_manager", None)
    if manager is None:
        return None
    session = manager.sessions.get(session_id)
    return getattr(session, "network_inspector", None) if session is not None else None

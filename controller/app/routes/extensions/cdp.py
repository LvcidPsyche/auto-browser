"""Pillar 3 — CDP routes (/sessions/{session_id}/cdp)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

cdp_router = APIRouter(prefix="/sessions/{session_id}/cdp", tags=["cdp"])


@cdp_router.get("/element")
async def cdp_element_intelligence(session_id: str, selector: str, request: Request):
    cdp = _get_cdp(request.app, session_id)
    if cdp is None:
        raise HTTPException(404, f"No CDP session for {session_id!r}")
    result = await cdp.get_element_intelligence(selector)
    return result


@cdp_router.post("/raw")
async def cdp_raw_command(session_id: str, body: dict[str, Any], request: Request):
    cdp = _get_cdp(request.app, session_id)
    if cdp is None:
        raise HTTPException(404, f"No CDP session for {session_id!r}")
    method = body.get("method", "")
    params = body.get("params", {})
    try:
        result = await cdp.raw_cdp_command(method, params)
    except ValueError:
        raise HTTPException(403, "CDP command is not permitted")
    return result


def _get_cdp(app, session_id: str):
    cdps = getattr(app.state, "cdp_sessions", {})
    return cdps.get(session_id)

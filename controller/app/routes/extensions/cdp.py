"""Pillar 3 — CDP routes (/sessions/{session_id}/cdp)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ...cdp.passthrough import CDPPassthrough

logger = logging.getLogger(__name__)

cdp_router = APIRouter(prefix="/sessions/{session_id}/cdp", tags=["cdp"])


@cdp_router.get("/element")
async def cdp_element_intelligence(session_id: str, selector: str, request: Request):
    cdp = await _get_cdp(request.app, session_id)
    if cdp is None:
        raise HTTPException(404, f"No CDP session for {session_id!r}")
    result = await cdp.get_element_intelligence(selector)
    return result


@cdp_router.post("/raw")
async def cdp_raw_command(session_id: str, body: dict[str, Any], request: Request):
    cdp = await _get_cdp(request.app, session_id)
    if cdp is None:
        raise HTTPException(404, f"No CDP session for {session_id!r}")
    method = body.get("method", "")
    params = body.get("params", {})
    try:
        result = await cdp.raw_cdp_command(method, params)
    except ValueError:
        raise HTTPException(403, "CDP command is not permitted")
    return result


async def _get_cdp(app, session_id: str):
    cdps = getattr(app.state, "cdp_sessions", {})
    cdp = cdps.get(session_id)
    if cdp is None:
        return None
    # The CDP session is attached to one page: the session's first tab. After
    # switching tabs these routes inspected the old tab, and after closing it
    # every call failed. Follow the session's active page instead.
    manager = getattr(app.state, "browser_manager", None)
    session = manager.sessions.get(session_id) if manager is not None else None
    page = getattr(session, "page", None)
    if page is None or getattr(cdp, "page", None) is page:
        return cdp
    try:
        fresh = await CDPPassthrough.from_page(page)
    except Exception as exc:
        logger.warning("cdp: could not attach to the active tab of %s: %s", session_id, exc)
        return cdp
    cdps[session_id] = fresh
    await cdp.detach()
    return fresh

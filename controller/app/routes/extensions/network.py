"""Pillar 3 — Network inspector routes (/sessions/{session_id}/network)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...browser.page_read import SESSION_BUSY_DETAIL, SessionBusyError, locked_read
from ...utils import quoted_for_log
from .._utils import OFF_LIST_PAGE_DETAIL

logger = logging.getLogger(__name__)

network_router = APIRouter(prefix="/sessions/{session_id}/network", tags=["network"])


class NetworkHookRequest(BaseModel):
    """The body of a hook registration. Its pattern is typed, so one that is no string answers 422.

    The body was any object, and a number was stored as the pattern: fnmatch raised on it for every finished
    request, so no hook registered after it fired again, and the DELETE route, whose pattern is text, could not
    remove it. A list or an object, which the inspector's dict cannot key, answered 500. The default keeps a
    missing pattern answered by the route, with the text an empty one gets, and so is a null: it is no pattern
    either, and a client that sends one for "none" read that text.
    """

    url_pattern: str | None = ""


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
        raise _no_inspector(session_id)
    async with _reading_an_allowed_page(request.app, session_id):
        entries = inspector.entries(limit=limit, method=method or None, url_contains=url_filter or None)
        if resource_type:
            entries = [entry for entry in entries if entry.get("resource_type") == resource_type]
        return {"requests": entries, "summary": inspector.summary()}


@contextlib.asynccontextmanager
async def _reading_an_allowed_page(app, session_id: str) -> AsyncIterator[None]:
    """Hold the session lock while its active page is one ALLOWED_HOSTS allows, as the raw CDP routes do (cdp.py).

    The inspector's entries are the URLs of every request, the refused page's included: the same refusal, on every
    surface that serves them (browser/services/diagnostics.py has the others). The lock is waited for within a bound,
    and the page checked before and under it (locked_read). An inspector with no live session has no page to judge
    and so no log to give: it answers as a missing inspector does.
    """
    manager = getattr(app.state, "browser_manager", None)
    session = manager.sessions.get(session_id) if manager is not None else None
    if session is None:
        raise _no_inspector(session_id)
    try:
        async with locked_read(manager, session):
            yield
    except SessionBusyError:
        raise HTTPException(409, SESSION_BUSY_DETAIL) from None
    except PermissionError:
        raise HTTPException(403, OFF_LIST_PAGE_DETAIL) from None


@network_router.post("/hooks")
async def network_register_hook(session_id: str, body: NetworkHookRequest, request: Request):
    inspector = _get_inspector(request.app, session_id)
    if inspector is None:
        raise _no_inspector(session_id)
    pattern = body.url_pattern
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
        raise _no_inspector(session_id)
    return {"hooks": inspector.list_hooks()}


# `:path` because a pattern such as "*/api/*" contains "/", which a plain {pattern}
# never matches (the server decodes %2F before routing), so that hook could not be removed.
@network_router.delete("/hooks/{pattern:path}")
async def network_remove_hook(session_id: str, pattern: str, request: Request):
    inspector = _get_inspector(request.app, session_id)
    if inspector is None:
        raise _no_inspector(session_id)
    removed = inspector.remove_hook(pattern)
    return {"removed": removed, "pattern": pattern}


def _no_inspector(session_id: str) -> HTTPException:
    """The 404 of a session with no inspector. Its id is the caller's, as long as the path: it is named cut."""
    return HTTPException(404, f"No network inspector for session {quoted_for_log(session_id)}")


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

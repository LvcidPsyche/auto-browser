"""Pillar 3 — CDP routes (/sessions/{session_id}/cdp)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ...browser.page_read import start_read
from ...cdp.passthrough import CDPPassthrough
from .._utils import OFF_LIST_PAGE_DETAIL

logger = logging.getLogger(__name__)

cdp_router = APIRouter(prefix="/sessions/{session_id}/cdp", tags=["cdp"])

# How long a CDP route waits for a session another call holds, and for the browser to answer.
# The route holds the session lock while it waits, and a renderer stuck in a script never
# answers: unbounded, that stalled every action on the session and the page guard with it.
CDP_TIMEOUT_SECONDS = 10.0


@cdp_router.get("/element")
async def cdp_element_intelligence(session_id: str, selector: str, request: Request):
    async with _allowed_cdp(request.app, session_id) as cdp:
        return await _answered(cdp.get_element_intelligence(selector))


@cdp_router.post("/raw")
async def cdp_raw_command(session_id: str, body: dict[str, Any], request: Request):
    method = body.get("method", "")
    params = body.get("params", {})
    async with _allowed_cdp(request.app, session_id) as cdp:
        try:
            return await _answered(cdp.raw_cdp_command(method, params))
        except ValueError:
            raise HTTPException(403, "CDP command is not permitted")


@contextlib.asynccontextmanager
async def _allowed_cdp(app, session_id: str) -> AsyncIterator[Any]:
    """The session's CDP passthrough, under the session lock, while its active page is on ALLOWED_HOSTS.

    Both routes read the page, and they checked neither: a page that had moved onto a
    refused host answered them, as observe refuses to. The lock keeps the page guard and
    actions from moving the page while it is read.
    """
    manager = getattr(app.state, "browser_manager", None)
    session = manager.sessions.get(session_id) if manager is not None else None
    if session is None:
        raise HTTPException(404, f"No CDP session for {session_id!r}")
    try:
        await asyncio.wait_for(session.lock.acquire(), timeout=CDP_TIMEOUT_SECONDS)
    except TimeoutError:
        raise HTTPException(409, "The session is busy with another call. Try again.") from None
    try:
        try:
            read = start_read(manager, session)
        except PermissionError:
            raise HTTPException(403, OFF_LIST_PAGE_DETAIL) from None
        cdp = await _get_cdp(app, session_id)
        if cdp is None:
            raise HTTPException(404, f"No CDP session for {session_id!r}")
        try:
            yield cdp
        except Exception:
            # Its error too: a page that moved meanwhile was not checked by anyone.
            if read.moved():
                raise HTTPException(403, OFF_LIST_PAGE_DETAIL) from None
            raise
        # And after the call: the page can move while it is read, and come back.
        if read.moved():
            raise HTTPException(403, OFF_LIST_PAGE_DETAIL)
    finally:
        session.lock.release()


async def _answered(call: Any) -> Any:
    try:
        return await asyncio.wait_for(call, timeout=CDP_TIMEOUT_SECONDS)
    except TimeoutError:
        raise HTTPException(504, "The page did not answer the CDP command in time.") from None


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
    # Attach and detach are bounded too: they run under the session lock (_allowed_cdp releases it).
    try:
        fresh = await asyncio.wait_for(CDPPassthrough.from_page(page), timeout=CDP_TIMEOUT_SECONDS)
    except TimeoutError:
        raise HTTPException(504, "The browser did not attach to the session's active tab in time.") from None
    except Exception as exc:
        # Not the old passthrough: it reads a tab other than the one checked, maybe a refused one.
        logger.warning("cdp: could not attach to the active tab of %s: %s", session_id, exc)
        raise HTTPException(503, "Could not attach to the session's active tab. Try again.") from None
    cdps[session_id] = fresh
    try:
        await asyncio.wait_for(cdp.detach(), timeout=CDP_TIMEOUT_SECONDS)
    except TimeoutError:
        raise HTTPException(504, "The browser did not detach from the session's previous tab in time.") from None
    return fresh

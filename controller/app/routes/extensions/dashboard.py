"""Operator dashboard route (/dashboard)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .dashboard_html import _DASHBOARD_HTML

dashboard_router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@dashboard_router.get("", response_class=HTMLResponse)
async def dashboard_root(request: Request):
    html = _DASHBOARD_HTML.replace("__OPERATOR_ID_HEADER__", request.app.state.settings.operator_id_header)
    html = html.replace("__OPERATOR_NAME_HEADER__", request.app.state.settings.operator_name_header)
    if "__OPERATOR_ID_HEADER__" in html or "__OPERATOR_NAME_HEADER__" in html:
        raise HTTPException(500, "dashboard header placeholder rendering failed")
    return HTMLResponse(html)

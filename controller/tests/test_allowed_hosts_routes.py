"""REST answers 403, not 500, when ALLOWED_HOSTS refuses a page read or a tab switch.

Observe, screenshots and tab activation now raise PermissionError for a page on a
host the list refuses (the page moved itself there, or a popup opened there).
These routes caught only KeyError or ValueError and answered anything else with a
500 "Internal error", so a caller could not tell a refused read from a crash.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes._utils import OFF_LIST_PAGE_DETAIL, OFF_LIST_TAB_DETAIL
from app.routes.session_diagnostics import create_session_diagnostics_router
from app.routes.sessions import create_sessions_router

REFUSED = PermissionError("Host 'evil.example' is not allowlisted")


def _client() -> TestClient:
    manager = SimpleNamespace(
        observe=AsyncMock(side_effect=REFUSED),
        capture_screenshot=AsyncMock(side_effect=REFUSED),
        screenshot_diff=AsyncMock(side_effect=REFUSED),
        activate_tab=AsyncMock(side_effect=REFUSED),
    )
    app = FastAPI()
    app.include_router(create_sessions_router(manager=manager))
    app.include_router(create_session_diagnostics_router(manager=manager, settings=SimpleNamespace()))
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("method", "path", "body", "detail"),
    [
        ("get", "/sessions/s1/observe", None, OFF_LIST_PAGE_DETAIL),
        ("post", "/sessions/s1/observe", {}, OFF_LIST_PAGE_DETAIL),
        ("post", "/sessions/s1/screenshot", {"label": "manual"}, OFF_LIST_PAGE_DETAIL),
        ("post", "/sessions/s1/screenshot/compare", None, OFF_LIST_PAGE_DETAIL),
        ("post", "/sessions/s1/tabs/activate", {"index": 1}, OFF_LIST_TAB_DETAIL),
    ],
)
def test_a_refused_page_answers_403_with_a_fixed_reason(method: str, path: str, body: dict | None, detail: str) -> None:
    client = _client()
    response = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert response.status_code == 403, response.text
    # A fixed text: the exception's own message names hosts, and these routes
    # answer generically everywhere else.
    assert response.json() == {"detail": detail}

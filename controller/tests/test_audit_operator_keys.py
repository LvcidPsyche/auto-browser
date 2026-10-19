"""Audit views read the keys an `AuditEvent` actually carries.

An event serves `operator` (an object with `id`, `name`, `source`) and `details`.
The dashboard's audit table and a session's HTML replay page read `operator_id`
and `data`, which no event has, so both showed a blank operator and the replay a
blank details column: the one place an operator looks to see who did what.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.browser_manager import BrowserManager
from app.models import AuditEvent, OperatorIdentity
from app.routes.session_diagnostics import create_session_diagnostics_router
from tests._chromium import requires_chromium
from tests.test_browser_manager_create_session import _settings
from tests.test_dashboard_errors import Stub, drive

HOSTILE_OPERATOR = '<img src=x onerror="window.__pwned = 1">'


def test_the_replay_page_shows_each_events_operator_and_details(tmp_path: Path) -> None:
    manager = BrowserManager(_settings(tmp_path))

    async def record() -> None:
        await manager.audit.append(
            event_type="browser_action",
            status="ok",
            action="navigate",
            session_id="sess-replay",
            operator=OperatorIdentity(id="alice", name="Alice", source="token"),
            details={"target": {"url": "https://example.com/cart"}},
        )
        await manager.audit.append(
            event_type="browser_action",
            status="ok",
            session_id="sess-replay",
            operator=OperatorIdentity(id=HOSTILE_OPERATOR, source="header"),
            details={"note": "<script>window.__pwned = 1</script>"},
        )

    asyncio.run(record())
    app = FastAPI()
    app.include_router(create_session_diagnostics_router(manager=manager, settings=manager.settings))

    with TestClient(app) as client:
        response = client.get("/sessions/sess-replay/replay")

    assert response.status_code == 200, response.text
    body = response.text
    assert "<td>alice</td>" in body
    assert "https://example.com/cart" in body
    # Both columns stay escaped: an operator id and a detail are caller-supplied.
    assert "<img src=x" not in body and "<script>window" not in body
    assert "&lt;img src=x" in body and "&lt;script&gt;window.__pwned" in body


@requires_chromium
def test_the_dashboard_audit_table_shows_the_operator_an_event_carries() -> None:
    event = AuditEvent(
        id="evt-1",
        timestamp="2026-01-01T00:00:00+00:00",
        event_type="browser_action",
        status="ok",
        action="navigate",
        session_id="sess-123456789abc",
        operator=OperatorIdentity(id="alice", name="Alice", source="token"),
        details={"target": {"url": "https://example.com/"}},
    ).model_dump()
    hostile = {**event, "id": "evt-2", "operator": {**event["operator"], "id": HOSTILE_OPERATOR}}
    stub = Stub({("GET", "/audit/events"): (200, json.loads(json.dumps([event, hostile], default=str)))})

    async def scenario(page, dialogs) -> dict:
        await page.wait_for_selector("#audit-tbody tr td.mono")
        return {
            "rows": await page.locator("#audit-tbody tr").all_text_contents(),
            "images": await page.locator("#audit-tbody img").count(),
            "pwned": await page.evaluate("window.__pwned === 1"),
        }

    seen = drive(stub, scenario)

    assert "alice" in seen["rows"][0]
    # Rendered as text, never as markup.
    assert HOSTILE_OPERATOR in seen["rows"][1]
    assert seen["images"] == 0 and seen["pwned"] is False

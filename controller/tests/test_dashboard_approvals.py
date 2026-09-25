"""The dashboard lists pending approvals and decides them.

An agent that hit an approval gate waited on an operator who had no queue to
look at: the dashboard showed approvals only inside a finished job's replay,
and deciding one meant a hand-written POST. The queue renders untrusted
reasons as text, never shows a sensitive action's typed text, and sends the
decision with the operator's identity headers.
"""

from __future__ import annotations

import asyncio
import glob
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from app.routes.extensions import _DASHBOARD_HTML
from app.tool_gateway import McpToolGateway
from app.tool_inputs import EvalJsInput

ORIGIN = "http://dashboard.test"
HOSTILE_REASON = '<img src=x onerror="window.__pwned = 1">Approve browser.eval_js with {"expression": "1+1"}'
PENDING = [
    {
        "id": "appr-type01",
        "session_id": "sess-123456789abc",
        "kind": "write",
        "status": "pending",
        "created_at": "2026-09-24T12:00:00Z",
        "updated_at": "2026-09-24T12:00:00Z",
        "reason": HOSTILE_REASON,
        "action": {"action": "type", "element_id": "op-pw", "text": "hunter2", "sensitive": True},
    },
    {
        "id": "appr-nav002",
        "session_id": "sess-123456789abc",
        "kind": "account_change",
        "status": "pending",
        "created_at": "2026-09-24T12:01:00Z",
        "updated_at": "2026-09-24T12:01:00Z",
        "reason": "Change the account email",
        "action": {"action": "type", "selector": "#email", "text": "new@example.com"},
    },
    {
        # How the gateway asks to approve a governed tool call (see _governed_call_decision).
        "id": "appr-tool03",
        "session_id": "sess-123456789abc",
        "kind": "write",
        "status": "pending",
        "created_at": "2026-09-24T12:02:00Z",
        "updated_at": "2026-09-24T12:02:00Z",
        "reason": 'Approve browser.set_cookies with {"cookies": "<redacted>"}',
        "action": {"action": "request_human_takeover", "text": "browser.set_cookies sha256:" + "ab" * 32},
    },
]


def test_the_queue_is_wired_and_text_only() -> None:
    assert 'id="approvals"' in _DASHBOARD_HTML
    assert "api('/approvals?status=pending')" in _DASHBOARD_HTML
    assert "loadApprovals()" in _DASHBOARD_HTML.split("async function loadAll()")[1].split("}")[0]
    assert "(sensitive text hidden)" in _DASHBOARD_HTML
    assert "appendCell(row, a.reason" in _DASHBOARD_HTML
    assert "innerHTML" not in _DASHBOARD_HTML


def test_the_tool_call_pattern_matches_the_gateways_stand_in() -> None:
    # The dashboard recognises a governed tool call by the stand-in text the
    # gateway builds; if that format changes, this fails instead of the queue
    # silently showing a digest again.
    js_pattern = re.search(r"&& /(.+?)/\.exec", _DASHBOARD_HTML).group(1)
    gateway = McpToolGateway(
        manager=SimpleNamespace(), orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
    )
    spec = gateway._registry.get("browser.eval_js")
    decision, _ = gateway._governed_call_decision(spec, EvalJsInput(session_id="s", expression="1+1"))

    assert decision.action == "request_human_takeover"
    assert re.fullmatch(js_pattern.strip("^$"), decision.text).group(1) == "browser.eval_js"


def _chromium() -> str | None:
    candidates = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return candidates[-1] if candidates else None


@pytest.mark.skipif(_chromium() is None, reason="no local Chromium binary")
def test_real_browser_approves_and_rejects_from_the_queue() -> None:
    from playwright.async_api import async_playwright

    html = _DASHBOARD_HTML.replace("__OPERATOR_ID_HEADER__", "X-Operator-Id").replace(
        "__OPERATOR_NAME_HEADER__", "X-Operator-Name"
    )
    decisions: list[dict[str, Any]] = []
    pending = list(PENDING)

    async def handle(route) -> None:
        request = route.request
        path = request.url[len(ORIGIN) :]
        if path == "/dashboard":
            await route.fulfill(status=200, content_type="text/html", body=html)
        elif path == "/approvals?status=pending":
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(pending))
        elif path.startswith("/approvals/") and request.method == "POST":
            approval_id, decision = path.split("/")[2:4]
            decisions.append(
                {
                    "id": approval_id,
                    "decision": decision,
                    "body": json.loads(request.post_data or "null"),
                    "operator": request.headers.get("x-operator-id"),
                }
            )
            pending[:] = [item for item in pending if item["id"] != approval_id]
            status = "approved" if decision == "approve" else "rejected"
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": status}))
        else:
            await route.fulfill(status=200, content_type="application/json", body="[]")

    async def run() -> dict[str, Any]:
        dialogs: list[tuple[str, str]] = []

        async def on_dialog(dialog) -> None:
            dialogs.append((dialog.type, dialog.message))
            if "operator id" in dialog.message:
                await dialog.accept("ops-alice")
            elif "rejecting" in dialog.message:
                await dialog.accept("not this account")
            else:
                await dialog.accept("")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=_chromium())
            try:
                page = await browser.new_page()
                page.on("dialog", lambda dialog: asyncio.ensure_future(on_dialog(dialog)))
                await page.route(f"{ORIGIN}/**", handle)
                await page.goto(f"{ORIGIN}/dashboard")
                await page.wait_for_selector("#approvals-tbody button")
                before = {
                    "stat": await page.text_content("#stat-approvals"),
                    "table": await page.text_content("#approvals-tbody"),
                    "images": await page.locator("#approvals-tbody img").count(),
                }
                await page.locator("#approvals-tbody tr").nth(0).get_by_role("button", name="Approve").click()
                await page.wait_for_function("document.querySelectorAll('#approvals-tbody tr').length === 2")
                approved_status = await page.text_content("#approvals-status")
                await page.locator("#approvals-tbody tr").nth(0).get_by_role("button", name="Reject").click()
                await page.wait_for_function("document.getElementById('stat-approvals').textContent === '1'")
                return {
                    **before,
                    "approved_status": approved_status,
                    "after_table": await page.text_content("#approvals-tbody"),
                    "pwned": await page.evaluate("window.__pwned === 1"),
                    "dialogs": dialogs,
                }
            finally:
                await browser.close()

    result = asyncio.run(run())

    assert result["stat"] == "3"
    # The hostile reason is shown as text and never becomes markup.
    assert '<img src=x onerror="window.__pwned = 1">' in result["table"]
    assert result["images"] == 0 and result["pwned"] is False
    # A sensitive action's typed text is never shown, in the table or the confirm dialog.
    assert "hunter2" not in result["table"]
    assert "(sensitive text hidden)" in result["table"]
    assert not any("hunter2" in message for _, message in result["dialogs"])
    # A non-sensitive action's text is shown so the operator knows what they approve.
    assert '"new@example.com"' in result["table"]

    assert decisions == [
        {"id": "appr-type01", "decision": "approve", "body": {"comment": None}, "operator": "ops-alice"},
        {"id": "appr-nav002", "decision": "reject", "body": {"comment": "not this account"}, "operator": "ops-alice"},
    ]
    assert result["approved_status"] == "Approval appr-type01 approved."
    # A governed tool call reads as the tool, not as its stand-in action and digest.
    assert "tool call browser.set_cookies (arguments under Reason)" in result["after_table"]
    assert "sha256" not in result["after_table"] and "request_human_takeover" not in result["after_table"]
    confirm_messages = [message for kind, message in result["dialogs"] if kind == "confirm"]
    assert len(confirm_messages) == 1 and "type · op-pw · (sensitive text hidden)" in confirm_messages[0]

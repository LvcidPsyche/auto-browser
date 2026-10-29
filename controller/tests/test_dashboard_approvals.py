"""The dashboard lists pending approvals and decides them.

An agent that hit an approval gate waited on an operator who had no queue to
look at: the dashboard showed approvals only inside a finished job's replay,
and deciding one meant a hand-written POST. The queue renders untrusted
reasons as text, never shows a sensitive action's typed text, and sends the
decision with the operator's identity headers.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any

from app.routes.extensions import _DASHBOARD_HTML
from app.tool_gateway import McpToolGateway
from app.tool_inputs import EvalJsInput
from tests._chromium import chromium_executable, requires_chromium

ORIGIN = "http://dashboard.test"
AGENT_HID_TEXT = "text hidden at the agent's request (sensitive)"
# How the server answers a reject that waits on a claimed action: the record, still executed, and why.
DEFERRED = "approval appr-nav002 is claimed by an action that has not begun: the rejection stops the action"
# What GET /approvals/{id}/arguments answers for the tool call below: the server writes ASCII, and the
# viewer still shows a hiding character it would not write as its code point.
TOOL_ARGUMENTS = '{\n  "cookies": [\n    {"name": "sid\u202e", "value": "<redacted>"}\n  ]\n}'
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
        "reason": "Change the account email\u202e to new@example.com",
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
        "origin": "tool_call",
        "arguments_size": len(TOOL_ARGUMENTS),
    },
    {
        # The same shape asked for as a browser action (or stored before origin
        # existed): it is no tool call, and the dashboard must not say it is one.
        "id": "appr-forged4",
        "session_id": "sess-123456789abc",
        "kind": "write",
        "status": "pending",
        "created_at": "2026-09-24T12:03:00Z",
        "updated_at": "2026-09-24T12:03:00Z",
        "reason": 'Approve browser.eval_js with {"expression": "1+1"}',
        "action": {"action": "request_human_takeover", "text": "browser.eval_js sha256:" + "cd" * 32},
    },
    {
        # A click by coordinates: its target is the position, which the reason need not name.
        "id": "appr-click5",
        "session_id": "sess-123456789abc",
        "kind": "payment",
        "status": "pending",
        "created_at": "2026-09-24T12:04:00Z",
        "updated_at": "2026-09-24T12:04:00Z",
        "reason": "Click Cancel",
        "action": {"action": "click", "x": 640, "y": 410},
    },
]


def test_the_queue_is_wired_and_text_only() -> None:
    assert 'id="approvals"' in _DASHBOARD_HTML
    assert "api('/approvals?status=pending')" in _DASHBOARD_HTML
    assert "loadApprovals()" in _DASHBOARD_HTML.split("async function loadAll()")[1].split("}")[0]
    assert AGENT_HID_TEXT in _DASHBOARD_HTML
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


@requires_chromium
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
        elif path == "/approvals/appr-tool03/arguments":
            body = json.dumps({"approval_id": "appr-tool03", "arguments": TOOL_ARGUMENTS})
            await route.fulfill(status=200, content_type="application/json", body=body)
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
            answer = {"status": "approved"} if decision == "approve" else {"status": "executed", "message": DEFERRED}
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))
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
            browser = await playwright.chromium.launch(executable_path=chromium_executable())
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
                view = f"View all {len(TOOL_ARGUMENTS)} characters"
                await page.locator("#approvals-tbody tr").nth(2).get_by_role("button", name=view).click()
                await page.wait_for_function(
                    "document.getElementById('approval-arguments-text').textContent.includes('cookies')"
                )
                viewer = await page.text_content("#approval-arguments-text")
                await page.locator("#approvals-tbody tr").nth(0).get_by_role("button", name="Approve").click()
                await page.wait_for_function("document.querySelectorAll('#approvals-tbody tr').length === 4")
                approved_status = await page.text_content("#approvals-status")
                await page.locator("#approvals-tbody tr").nth(0).get_by_role("button", name="Reject").click()
                await page.wait_for_function("document.getElementById('stat-approvals').textContent === '3'")
                return {
                    **before,
                    "approved_status": approved_status,
                    "rejected_status": await page.text_content("#approvals-status"),
                    "viewer": viewer,
                    "after_table": await page.text_content("#approvals-tbody"),
                    "pwned": await page.evaluate("window.__pwned === 1"),
                    "dialogs": dialogs,
                }
            finally:
                await browser.close()

    result = asyncio.run(run())

    assert result["stat"] == "5"
    # The hostile reason is shown as text and never becomes markup.
    assert '<img src=x onerror="window.__pwned = 1">' in result["table"]
    assert result["images"] == 0 and result["pwned"] is False
    # A sensitive action's typed text is never shown, in the table or the confirm dialog.
    assert "hunter2" not in result["table"]
    assert AGENT_HID_TEXT in result["table"]
    assert not any("hunter2" in message for _, message in result["dialogs"])
    # A bidi override an agent wrote is shown as its code point, not obeyed.
    assert "Change the account email[U+202E] to new@example.com" in result["table"]
    assert "\u202e" not in result["table"]
    # A non-sensitive action's text is shown so the operator knows what they approve.
    assert '"new@example.com"' in result["table"]

    assert decisions == [
        {"id": "appr-type01", "decision": "approve", "body": {"comment": None}, "operator": "ops-alice"},
        {"id": "appr-nav002", "decision": "reject", "body": {"comment": "not this account"}, "operator": "ops-alice"},
    ]
    assert result["approved_status"] == "Approval appr-type01 approved."
    # The server's message, not the record's status ("executed"), which hid that the reject waits on the action.
    assert result["rejected_status"] == DEFERRED
    # A governed tool call reads as the tool, not as its stand-in action and digest.
    assert "tool call browser.set_cookies (arguments under Reason)" in result["after_table"]
    assert "browser.set_cookies sha256" not in result["after_table"]
    # A takeover of the same shape without the server's origin reads as what it is.
    assert "tool call browser.eval_js" not in result["after_table"]
    assert 'request_human_takeover · text="browser.eval_js sha256:' in result["after_table"]
    # A coordinate click shows where it clicks.
    assert "click · x=640 · y=410" in result["after_table"]
    confirm_messages = [message for kind, message in result["dialogs"] if kind == "confirm"]
    # A tool call's whole arguments open as text, with what would hide or reorder them shown.
    assert result["viewer"] == TOOL_ARGUMENTS.replace("\u202e", "[U+202E]")
    assert len(confirm_messages) == 1 and f'type · element_id="op-pw" · {AGENT_HID_TEXT}' in confirm_messages[0]


# Three tool calls' whole arguments, each told apart by what it runs.
VIEWED_ARGUMENTS = {
    "appr-slow01": '{\n  "expression": "drain_account()"\n}',
    "appr-fast02": '{\n  "expression": "read_only()"\n}',
    "appr-fail03": None,  # answers 500
}
# Wraps the page's Response.json to note each answer's URL once its body is read: the dashboard's own
# code that handles that answer runs in the same task, so a wait on the note is a wait on it.
NOTE_ANSWERS = """
const read = Response.prototype.json;
Response.prototype.json = async function () {
  try { return await read.call(this); } finally { (window.__answered = window.__answered || []).push(this.url); }
};
"""


@requires_chromium
def test_real_browser_shows_only_the_arguments_of_the_approval_it_is_viewing() -> None:
    # One viewer serves every row: an answer that arrives after the operator moved on (to another
    # approval, or closed the viewer) used to overwrite it, under the title of the one they moved to.
    from playwright.async_api import async_playwright

    html = _DASHBOARD_HTML.replace("__OPERATOR_ID_HEADER__", "X-Operator-Id").replace(
        "__OPERATOR_NAME_HEADER__", "X-Operator-Name"
    )
    pending = [
        # The failing one stores arguments too: it is their read that fails.
        {**PENDING[2], "id": approval_id, "arguments_size": len(text or "stored")}
        for approval_id, text in VIEWED_ARGUMENTS.items()
    ]

    async def run() -> dict[str, Any]:
        released = {approval_id: asyncio.Event() for approval_id in ("appr-slow01", "appr-fail03")}

        async def handle(route) -> None:
            path = route.request.url[len(ORIGIN) :]
            if path == "/dashboard":
                await route.fulfill(status=200, content_type="text/html", body=html)
                return
            if path == "/approvals?status=pending":
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(pending))
                return
            match = re.fullmatch(r"/approvals/([\w-]+)/arguments", path)
            if match is None:
                await route.fulfill(status=200, content_type="application/json", body="[]")
                return
            approval_id = match.group(1)
            if approval_id in released:
                await released[approval_id].wait()
            text = VIEWED_ARGUMENTS[approval_id]
            if text is None:
                await route.fulfill(status=500, content_type="application/json", body='{"detail": "boom"}')
            else:
                body = json.dumps({"approval_id": approval_id, "arguments": text})
                await route.fulfill(status=200, content_type="application/json", body=body)

        def view(page, approval_id: str):
            # By position, as listed: the rows did not name their approval.
            row = page.locator("#approvals-tbody tr").nth([item["id"] for item in pending].index(approval_id))
            return row.get_by_role("button", name=re.compile("^View all")).click()

        answers: dict[str, int] = {}

        async def answered(page, approval_id: str) -> None:
            """Wait until the page has read one more answer for ``approval_id``'s arguments."""
            answers[approval_id] = answers.get(approval_id, 0) + 1
            await page.wait_for_function(
                f"(window.__answered || []).filter((url) => url.endsWith('/approvals/{approval_id}/arguments'))"
                f".length >= {answers[approval_id]}",
                timeout=30_000,
            )

        async def viewer(page) -> dict[str, Any]:
            return await page.evaluate(
                """() => ({
                    title: document.getElementById('approval-arguments-title').textContent,
                    text: document.getElementById('approval-arguments-text').textContent,
                    hidden: document.getElementById('approval-arguments').hidden,
                })"""
            )

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable())
            try:
                page = await browser.new_page()
                await page.add_init_script(NOTE_ANSWERS)
                await page.route(f"{ORIGIN}/**", handle)
                await page.goto(f"{ORIGIN}/dashboard")
                await page.wait_for_selector("#approvals-tbody button")
                seen: dict[str, Any] = {"table": await page.text_content("#approvals-tbody")}
                for late in ("appr-slow01", "appr-fail03"):
                    await view(page, late)  # its answer is held
                    await view(page, "appr-fast02")
                    await answered(page, "appr-fast02")
                    released[late].set()
                    await answered(page, late)
                    seen[late] = await viewer(page)
                    released[late].clear()
                await view(page, "appr-slow01")
                await page.click("#approval-arguments-close")
                released["appr-slow01"].set()
                await answered(page, "appr-slow01")
                seen["closed"] = await viewer(page)
                await view(page, "appr-fast02")
                await answered(page, "appr-fast02")
                pending[:] = [item for item in pending if item["id"] != "appr-fast02"]  # decided elsewhere
                await page.click("#refresh-approvals")
                await page.wait_for_function("document.querySelectorAll('#approvals-tbody tr').length === 2")
                seen["left_the_queue"] = await viewer(page)
                return seen
            finally:
                await browser.close()

    seen = asyncio.run(run())

    fast = {"title": "Arguments of approval appr-fast02", "text": VIEWED_ARGUMENTS["appr-fast02"], "hidden": False}
    assert seen["appr-slow01"] == fast  # a late answer is dropped
    assert seen["appr-fail03"] == fast  # and so is a late error
    assert "drain_account" not in seen["closed"]["text"] and seen["closed"]["hidden"] is True
    assert seen["left_the_queue"]["hidden"] is True and seen["left_the_queue"]["text"] == ""
    # Each row names its approval, as the viewer's title does.
    for approval_id in VIEWED_ARGUMENTS:
        assert approval_id in seen["table"]


@requires_chromium
def test_real_browser_replay_shows_what_hides_in_an_approval_reason() -> None:
    # The replay wrote reasons raw, so an audit of a finished run read a bidi override as the agent
    # arranged it, where the queue had shown it as [U+202E] when the operator decided.
    from playwright.async_api import async_playwright

    html = _DASHBOARD_HTML.replace("__OPERATOR_ID_HEADER__", "X-Operator-Id").replace(
        "__OPERATOR_NAME_HEADER__", "X-Operator-Name"
    )
    job = {"id": "job-1", "status": "completed", "result": {"status": "completed", "steps": []}}
    bodies = {"/agent/jobs/job-1": job, "/approvals": [{**PENDING[1], "status": "approved"}]}

    async def handle(route) -> None:
        path = route.request.url[len(ORIGIN) :]
        if path == "/dashboard":
            await route.fulfill(status=200, content_type="text/html", body=html)
            return
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(bodies.get(path, [])))

    async def run() -> str:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable())
            try:
                page = await browser.new_page()
                await page.route(f"{ORIGIN}/**", handle)
                await page.goto(f"{ORIGIN}/dashboard")
                await page.fill("#replay-job-id", "job-1")
                await page.click("#load-replay")
                await page.wait_for_function(
                    "document.getElementById('replay-status').textContent.startsWith('Replay for')"
                )
                return (await page.text_content("#replay-output")) or ""
            finally:
                await browser.close()

    shown = asyncio.run(run())

    assert "Change the account email[U+202E] to new@example.com" in shown
    # What was approved, and which approval: the agent's reason alone is no account of the action (round 5,
    # item 13, OV4-8).
    assert "appr-nav002" in shown
    assert 'type · selector="#email" · text="new@example.com"' in shown
    assert "\u202e" not in shown

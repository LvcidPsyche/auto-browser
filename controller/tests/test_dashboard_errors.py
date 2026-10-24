"""The dashboard says what went wrong instead of reporting a failed request as done.

`api()` caught every 4xx, 5xx and network error and resolved with `{}`, so the
auth-profile wizard said "Saved auth profile" over a 500, the approval queue said
"request failed" with no reason, and a table fed by a dead endpoint read "No
sessions". These tests drive the page's real JavaScript in Chromium against a
stubbed controller: a failed request rejects with the server's own text and every
caller shows it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.routes.extensions import _DASHBOARD_HTML
from tests._chromium import chromium_executable, requires_chromium

ORIGIN = "http://dashboard.test"
HTML = _DASHBOARD_HTML.replace("__OPERATOR_ID_HEADER__", "X-Operator-Id").replace(
    "__OPERATOR_NAME_HEADER__", "X-Operator-Name"
)
NETWORK_DOWN = "network"  # a route value that aborts the request instead of answering it

Routes = dict[tuple[str, str], Any]


class Stub:
    """A controller double: `routes[(method, path)] = (status, body)` or NETWORK_DOWN.

    Unlisted GETs answer an empty list and unlisted writes an empty object, so a
    test names only the endpoint it breaks.
    """

    def __init__(self, routes: Routes | None = None) -> None:
        self.routes: Routes = dict(routes or {})
        self.requests: list[tuple[str, str]] = []

    async def handle(self, route) -> None:
        request = route.request
        path = request.url[len(ORIGIN) :].split("?")[0]
        if path == "/dashboard":
            await route.fulfill(status=200, content_type="text/html", body=HTML)
            return
        self.requests.append((request.method, path))
        answer = self.routes.get((request.method, path), (200, [] if request.method == "GET" else {}))
        if answer == NETWORK_DOWN:
            await route.abort("connectionrefused")
            return
        status, body = answer
        text = body if isinstance(body, str) else json.dumps(body)
        await route.fulfill(status=status, content_type="application/json", body=text)


def drive(stub: Stub, scenario) -> Any:
    """Open the dashboard against `stub` and return what `scenario(page, dialogs)` returns."""
    from playwright.async_api import async_playwright

    async def run() -> Any:
        dialogs: list[tuple[str, str]] = []

        async def on_dialog(dialog) -> None:
            dialogs.append((dialog.type, dialog.message))
            await dialog.accept("ops-alice" if dialog.type == "prompt" else "")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable())
            try:
                page = await browser.new_page()
                page.set_default_timeout(5000)  # a missing message should fail fast, not after 30 s
                page.on("dialog", lambda dialog: asyncio.ensure_future(on_dialog(dialog)))
                await page.route(f"{ORIGIN}/**", stub.handle)
                await page.goto(f"{ORIGIN}/dashboard")
                return await scenario(page, dialogs)
            finally:
                await browser.close()

    return asyncio.run(run())


async def text_after(page, selector: str, settled_when: str) -> str:
    """The text of `selector` once the JS expression `settled_when` holds."""
    await page.wait_for_function(settled_when)
    return await page.text_content(selector)


WIZARD_FAILURES = {
    "a 500 with a detail": ((500, {"detail": "Internal error"}), ["Internal error"]),
    "a 404 for the session": ((404, {"detail": "Session sess-1 not found"}), ["Session sess-1 not found"]),
    "a BrowserActionError body": (
        (400, {"ok": False, "error": "Login is not complete", "code": "auth_state_missing"}),
        ["Login is not complete", "auth_state_missing"],
    ),
    "a 422 validation list": (
        (422, {"detail": [{"loc": ["body", "profile_name"], "msg": "String should have at least 1 character"}]}),
        ["String should have at least 1 character"],
    ),
    "a 401": ((401, {}), ["unauthorized"]),
    "a body that is not JSON": ((502, "<html>bad gateway</html>"), ["HTTP 502"]),
    "the network down": (NETWORK_DOWN, ["network error"]),
}


@requires_chromium
def test_the_wizard_never_says_saved_over_a_failed_request() -> None:
    stub = Stub({("POST", "/sessions"): (200, {"id": "sess-1", "takeover_url": "http://novnc.test/"})})
    save_path = ("POST", "/sessions/sess-1/auth-profiles")

    async def scenario(page, dialogs) -> dict[str, str]:
        await page.fill("#wizard-profile-name", "shop")
        await page.click("#wizard-start")
        await page.wait_for_function("document.getElementById('wizard-status').textContent.includes('ready')")
        seen: dict[str, str] = {}
        for label, (answer, _) in WIZARD_FAILURES.items():
            stub.routes[save_path] = answer
            await page.evaluate("document.getElementById('wizard-status').textContent = ''")
            await page.click("#wizard-save")
            seen[label] = await text_after(
                page,
                "#wizard-status",
                "document.getElementById('wizard-status').textContent.length > 0 && "
                "document.getElementById('wizard-status').textContent !== 'Saving auth profile...'",
            )
        stub.routes[save_path] = (200, {"profile_name": "shop"})
        await page.click("#wizard-save")
        seen["success"] = await text_after(
            page, "#wizard-status", "document.getElementById('wizard-status').textContent.startsWith('Saved')"
        )
        return seen

    seen = drive(stub, scenario)

    for label, (_, expected) in WIZARD_FAILURES.items():
        assert "Saved" not in seen[label], f"{label}: {seen[label]!r}"
        for fragment in expected:
            assert fragment in seen[label], f"{label}: {seen[label]!r} lacks {fragment!r}"
    assert seen["success"] == 'Saved auth profile "shop".'


@requires_chromium
def test_every_loader_shows_why_it_could_not_load() -> None:
    down = (500, {"detail": "backend down"})
    stub = Stub(
        {
            ("GET", "/mesh/identity"): down,
            ("GET", "/sessions"): down,
            ("GET", "/agent/jobs"): down,
            ("GET", "/approvals"): down,
            ("GET", "/mesh/peers"): down,
            ("GET", "/audit/events"): (500, {"ok": False, "error": "audit store offline", "code": "audit_down"}),
            ("GET", "/auth-profiles"): down,
            ("GET", "/agent/jobs/job-ok"): (200, {"id": "job-ok", "status": "completed", "steps": []}),
        }
    )

    async def scenario(page, dialogs) -> dict[str, str]:
        await page.wait_for_function("document.querySelector('#audit-tbody').textContent.includes('audit store')")
        await page.wait_for_function("document.querySelector('#wizard-status').textContent.includes('profiles')")
        # The run itself replays; only its approvals are missing, and the page says so.
        await page.fill("#replay-job-id", "job-ok")
        await page.click("#load-replay")
        await page.wait_for_function("document.getElementById('replay-status').textContent.startsWith('Replay for')")
        return {
            selector: (await page.text_content(selector)) or ""
            for selector in (
                "#replay-output",
                "#node-id",
                "#sessions-tbody",
                "#agent-jobs-tbody",
                "#approvals-tbody",
                "#peers-tbody",
                "#audit-tbody",
                "#wizard-status",
            )
        }

    seen = drive(stub, scenario)

    for selector in ("#sessions-tbody", "#agent-jobs-tbody", "#approvals-tbody", "#peers-tbody", "#wizard-status"):
        assert "backend down" in seen[selector], f"{selector}: {seen[selector]!r}"
    assert "audit store offline (audit_down)" in seen["#audit-tbody"]
    assert "backend down" in seen["#node-id"]
    assert "Could not load approvals: backend down" in seen["#replay-output"]
    # A dead endpoint is not an empty one.
    for selector in ("#sessions-tbody", "#agent-jobs-tbody", "#approvals-tbody", "#peers-tbody", "#audit-tbody"):
        assert not any(
            empty in seen[selector]
            for empty in ("No sessions", "No agent jobs", "No approvals", "No peers", "No audit")
        )


@requires_chromium
def test_every_action_shows_why_it_failed() -> None:
    approval = {
        "id": "appr-1",
        "session_id": "sess-123456789abc",
        "kind": "write",
        "status": "pending",
        "created_at": "2026-09-24T12:00:00Z",
        "reason": "Change the account email",
        "action": {"action": "type", "selector": "#email", "text": "new@example.com"},
    }
    job = {"id": "job-123456789abc", "kind": "agent", "status": "running", "checkpoints": [], "request": {}}
    peer = {"node_id": "node-abcdef0123456789", "display_name": "p", "endpoint": "http://p.test", "last_seen": 0}
    stub = Stub(
        {
            ("GET", "/approvals"): (200, [approval]),
            ("GET", "/agent/jobs"): (200, [job]),
            ("GET", "/mesh/peers"): (200, {"peers": [peer]}),
            ("POST", "/approvals/appr-1/approve"): (409, {"detail": "approval already decided"}),
            ("POST", "/agent/jobs/job-123456789abc/cancel"): (500, {"detail": "cancel exploded"}),
            ("GET", "/agent/jobs/job-missing"): (404, {"detail": "no such job"}),
            ("POST", "/sessions"): (500, {"detail": "cannot start a browser"}),
        }
    )

    async def scenario(page, dialogs) -> dict[str, str]:
        seen: dict[str, str] = {}
        await page.wait_for_selector("#approvals-tbody button")
        await page.wait_for_selector("#agent-jobs-tbody button")
        await page.wait_for_selector("#peers-tbody tr td")

        await page.get_by_role("button", name="Approve").click()
        seen["approve"] = await text_after(
            page, "#approvals-status", "document.getElementById('approvals-status').textContent.includes('decided')"
        )
        await page.get_by_role("button", name="Cancel").click()
        seen["cancel"] = await text_after(
            page, "#agent-jobs-status", "document.getElementById('agent-jobs-status').textContent.includes('exploded')"
        )
        # Peers are managed in the peers file: the list offers no way to change one.
        seen["peer_buttons"] = str(await page.locator("#peers-tbody button").count())
        await page.fill("#replay-job-id", "job-missing")
        await page.click("#load-replay")
        seen["replay"] = await text_after(
            page, "#replay-status", "document.getElementById('replay-status').textContent.includes('no such job')"
        )
        await page.fill("#wizard-profile-name", "shop")
        await page.click("#wizard-start")
        seen["start"] = await text_after(
            page, "#wizard-status", "document.getElementById('wizard-status').textContent.includes('cannot start')"
        )
        await page.evaluate(
            "const o = document.createElement('option'); o.value = 'shop'; o.textContent = 'shop';"
            "document.getElementById('wizard-profile-select').appendChild(o);"
            "document.getElementById('wizard-profile-select').value = 'shop';"
        )
        await page.click("#wizard-reopen")
        seen["reopen"] = await text_after(
            page,
            "#wizard-status",
            "document.getElementById('wizard-status').textContent.startsWith('Could not reopen')",
        )
        return seen

    seen = drive(stub, scenario)

    assert "approval already decided" in seen["approve"] and not seen["approve"].startswith("Approval appr-1 approved")
    assert "cancel exploded" in seen["cancel"]
    assert seen["peer_buttons"] == "0"
    assert "no such job" in seen["replay"]
    assert "cannot start a browser" in seen["start"]
    assert "cannot start a browser" in seen["reopen"]

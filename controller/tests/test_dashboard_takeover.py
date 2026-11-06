"""The dashboard asks for the takeover token while it is in front, then opens noVNC (W2-18).

`openTakeover` opened the about:blank window first and prompted for the takeover token after: Chrome and Edge do not
show `prompt()` in a tab that is not in front, so the token was never asked for and every "Open noVNC" of an
isolated session opened the password-less fallback; the re-ask after a 403 was suppressed the same way. It now asks
first, then opens the window, clears the new window's copy of the dashboard's sessionStorage, and on a 403 clears the
stored token, closes the window and says so on the page, with no second prompt. Only an isolated
(`docker_ephemeral`) row reads the takeover link; a shared browser-node row opens its listed URL directly.
"""

from __future__ import annotations

import asyncio

from tests._chromium import requires_chromium
from tests.test_dashboard_errors import Stub, drive

LISTED = "http://novnc.test/vnc.html?autoconnect=true"
FULL = LISTED + "#password=Ab3dEf7h"
SESSION = "sess-abcdef123456"
LINK_PATH = f"/sessions/{SESSION}/takeover-link"
TOKEN = "ops-alice"  # what drive()'s dialog handler answers every prompt with
TAKEOVER_PROMPT = "Enter the takeover token"


class TakeoverStub(Stub):
    """Requires X-Takeover-Token on the takeover link, serves `refusals` 403s first, and can hold its answer."""

    def __init__(self, mode: str = "docker_ephemeral", refusals: int = 0) -> None:
        row = {"id": SESSION, "status": "active", "takeover_url": LISTED}
        if mode:
            row["isolation"] = {"mode": mode}
        super().__init__({("GET", "/sessions"): (200, [row])})
        self.refusals = refusals
        self.headers: list[str | None] = []
        self.gate: asyncio.Event | None = None

    async def handle(self, route) -> None:
        if route.request.url.endswith(LINK_PATH):
            self.requests.append((route.request.method, LINK_PATH))
            token = route.request.headers.get("x-takeover-token")
            self.headers.append(token)
            if self.gate is not None:
                await self.gate.wait()
            if self.refusals > 0 or token != TOKEN:
                self.refusals -= 1
                await route.fulfill(status=403, content_type="application/json", body='{"detail": "refused"}')
                return
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=f'{{"session_id": "{SESSION}", "takeover_url": "{FULL}"}}',
            )
            return
        await super().handle(route)


def _takeover_prompts(events: list) -> list:
    return [event for event in events if event[0] == "prompt" and event[1].startswith(TAKEOVER_PROMPT)]


async def _novnc(page) -> None:
    await page.context.route("http://novnc.test/**", lambda route: route.fulfill(status=200, body="novnc"))
    await page.wait_for_selector("#sessions-tbody a")


@requires_chromium
def test_the_prompt_comes_before_the_popup_and_the_header_is_sent() -> None:
    stub = TakeoverStub()

    async def scenario(page, events) -> dict:
        await _novnc(page)
        stub.gate = asyncio.Event()
        page.on("popup", lambda popup: events.append(("popup", popup.url)))
        async with page.expect_popup() as popup_info:
            await page.click("#sessions-tbody a")
        popup = await popup_info.value
        # Still about:blank (the link is held): its copy of the dashboard's sessionStorage is gone.
        while not stub.headers:
            await asyncio.sleep(0.02)
        storage = await popup.evaluate("sessionStorage.length")
        stub.gate.set()
        await popup.wait_for_url("http://novnc.test/**")
        # Only the new window's copy was cleared: the dashboard tab keeps both tokens.
        opener = await page.evaluate(
            "[sessionStorage.getItem('ab_token'), sessionStorage.getItem('ab_takeover_token')]"
        )
        return {"events": list(events), "storage": storage, "url": popup.url, "opener": opener}

    seen = drive(stub, scenario)

    kinds = [event[0] if event[0] == "popup" else event[1][:24] for event in seen["events"]]
    assert kinds.index(TAKEOVER_PROMPT[:24]) < kinds.index("popup"), kinds
    assert len(_takeover_prompts(seen["events"])) == 1
    assert stub.headers == [TOKEN]
    assert seen["storage"] == 0
    assert seen["opener"] == [TOKEN, TOKEN]
    assert seen["url"] == FULL


@requires_chromium
def test_a_refused_token_is_cleared_without_a_second_prompt() -> None:
    stub = TakeoverStub(refusals=1)

    async def scenario(page, events) -> dict:
        await _novnc(page)
        async with page.expect_popup() as popup_info:
            await page.click("#sessions-tbody a")
        popup = await popup_info.value
        if not popup.is_closed():  # it may already have closed by now
            await popup.wait_for_event("close")
        await page.wait_for_function("document.getElementById('takeover-note').textContent.includes('refused')")
        after_refusal = {
            "prompts": len(_takeover_prompts(events)),
            "stored": await page.evaluate("sessionStorage.getItem('ab_takeover_token')"),
            "note": await page.text_content("#takeover-note"),
        }
        async with page.expect_popup() as popup_info:
            await page.click("#sessions-tbody a")
        popup = await popup_info.value
        await popup.wait_for_url("http://novnc.test/**")
        return {**after_refusal, "prompts_after_retry": len(_takeover_prompts(events)), "url": popup.url}

    seen = drive(stub, scenario)

    assert seen["prompts"] == 1
    assert seen["stored"] is None
    assert "click Open noVNC again" in seen["note"]
    assert seen["prompts_after_retry"] == 2  # the next click asks again, with the dashboard in front
    assert seen["url"] == FULL


@requires_chromium
def test_a_blocked_popup_says_to_click_again_and_keeps_the_token() -> None:
    stub = TakeoverStub()

    async def scenario(page, events) -> dict:
        await _novnc(page)
        await page.evaluate("window.open = () => null")
        await page.click("#sessions-tbody a")
        await page.wait_for_function("document.getElementById('takeover-note').textContent.includes('blocked')")
        return {
            "stored": await page.evaluate("sessionStorage.getItem('ab_takeover_token')"),
            "requests": [request for request in stub.requests if request[1] == LINK_PATH],
        }

    seen = drive(stub, scenario)

    assert seen["stored"] == TOKEN
    assert seen["requests"] == []


_HEIGHT = "(selector) => document.querySelector(selector).getBoundingClientRect().height"


@requires_chromium
def test_an_empty_takeover_note_takes_no_space_and_a_shown_one_is_visible() -> None:
    # The notes used to take `.empty`'s 32px padding: an empty 64px band under the Sessions header and the wizard.
    stub = TakeoverStub()
    stub.routes[("POST", "/sessions")] = (
        200,
        {"id": SESSION, "takeover_url": LISTED, "isolation": {"mode": "docker_ephemeral"}},
    )
    wizard_note = "#wizard-takeover div"

    async def scenario(page, events) -> dict:
        await _novnc(page)
        await page.fill("#wizard-profile-name", "shop")
        await page.click("#wizard-start")
        await page.wait_for_selector(wizard_note, state="attached")
        empty = {
            "sessions": await page.evaluate(_HEIGHT, "#takeover-note"),
            "wizard": await page.evaluate(_HEIGHT, wizard_note),
        }
        await page.evaluate("window.open = () => null")
        await page.click("#sessions-tbody a")
        await page.click("#wizard-takeover a")
        await page.wait_for_function("document.querySelector('#wizard-takeover div').textContent.includes('blocked')")
        shown = {
            "sessions": await page.is_visible("#takeover-note"),
            "wizard": await page.is_visible(wizard_note),
        }
        return {"empty": empty, "shown": shown}

    seen = drive(stub, scenario)

    assert seen["empty"] == {"sessions": 0, "wizard": 0}
    assert seen["shown"] == {"sessions": True, "wizard": True}


@requires_chromium
def test_a_shared_browser_node_row_opens_its_listed_url_with_no_prompt() -> None:
    stub = TakeoverStub(mode="shared_browser_node")

    async def scenario(page, events) -> dict:
        await _novnc(page)
        async with page.expect_popup() as popup_info:
            await page.click("#sessions-tbody a")
        popup = await popup_info.value
        await popup.wait_for_url("http://novnc.test/**")
        return {"prompts": len(_takeover_prompts(events)), "url": popup.url}

    seen = drive(stub, scenario)

    assert seen["prompts"] == 0
    assert seen["url"] == LISTED
    assert [request for request in stub.requests if request[1] == LINK_PATH] == []

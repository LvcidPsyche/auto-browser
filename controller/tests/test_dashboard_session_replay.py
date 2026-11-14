"""The dashboard opens a session's replay page on a controller that asks for a token (AUTH-27).

`GET /sessions/{id}/replay` and the `/artifacts` screenshots it names sit behind the API token, which neither a
browser's navigation nor an `<img src>` can send: with a token set the page could not be opened in a browser at all,
and every screenshot on it was a 401. Nothing in the dashboard led to it either. Each row of the Sessions table now
has an "Open replay" button: the dashboard reads the page and each of its screenshots with the token and opens a
copy that holds them, in a window that has no copy of the dashboard's tokens and can run no script and load nothing.
The route and its page are as they were.

These drive the dashboard's own script in Chromium against a stubbed controller.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from tests._chromium import requires_chromium
from tests.test_dashboard_errors import NETWORK_DOWN, ORIGIN, PIXEL_PNG, Stub, drive
from tests.test_session_diagnostics import SESSION, _replay

BEARER = "Bearer ops-alice"  # drive() answers the dashboard's token prompt with ops-alice
BUTTON = "#sessions-tbody button"
NOTE = "document.getElementById('takeover-note').textContent"
ELSEWHERE_ORIGIN = "http://elsewhere.test"
ELSEWHERE = f"{ELSEWHERE_ORIGIN}/shot.png"
REPLAY_COPY_POLICY = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'"
PNG_DATA_URL = "data:image/png;base64,"


def _real_page(tmp_path: Path, *shots: str) -> str:
    """The route's own page for a session whose artifact folder holds these screenshots."""
    (tmp_path / SESSION).mkdir()
    for shot in shots:
        (tmp_path / SESSION / shot).write_bytes(PIXEL_PNG)
    return _replay(tmp_path, "https://example.com/page")


class ReplayStub(Stub):
    """Lists one session and serves its replay page and screenshots to a request that carries the API token.

    `page` is the page's HTML, or a (status, body) or NETWORK_DOWN to answer instead; `missing` names the
    screenshots that answer 404.
    """

    def __init__(self, page: Any, *, session: str = SESSION, missing: tuple[str, ...] = ()) -> None:
        super().__init__({("GET", "/sessions"): (200, [{"id": session, "status": "closed"}])})
        self.page = page
        self.missing = missing
        self.asked: list[str] = []  # every request, with its query
        self.tokens: dict[str, list[str | None]] = {}  # the Authorization header of each one this class answers

    async def handle(self, route) -> None:
        request = route.request
        path = request.url[len(ORIGIN) :] if request.url.startswith(ORIGIN) else request.url
        self.asked.append(path)
        if not (path.endswith("/replay") or path.startswith("/artifacts/") or path == ELSEWHERE):
            await super().handle(route)
            return
        token = request.headers.get("authorization")
        self.tokens.setdefault(path, []).append(token)
        if path == ELSEWHERE:
            await route.fulfill(status=200, content_type="image/png", body=PIXEL_PNG)
        elif token != BEARER:
            await route.fulfill(status=401, content_type="application/json", body='{"detail": "unauthorized"}')
        elif path.startswith("/artifacts/"):
            if path.rsplit("/", 1)[-1] in self.missing:
                await route.fulfill(status=404, content_type="application/json", body='{"detail": "Not Found"}')
            else:
                await route.fulfill(status=200, content_type="image/png", body=PIXEL_PNG)
        elif self.page == NETWORK_DOWN:
            await route.abort("connectionrefused")
        elif isinstance(self.page, tuple):
            status, body = self.page
            await route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        else:
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=self.page)


async def _opened_copy(page):
    """The window "Open replay" opens, once it shows the copy and its images have settled."""
    await page.wait_for_selector(BUTTON)
    async with page.expect_popup() as popup_info:
        await page.click(BUTTON)
    popup = await popup_info.value
    popup.set_default_timeout(5000)
    await popup.wait_for_url(lambda url: url.startswith("blob:"))

    async def settled() -> None:
        # Asked from here: the copy's policy refuses the string wait_for_function would evaluate inside it.
        while not await popup.evaluate(_SETTLED):
            await asyncio.sleep(0.02)

    await asyncio.wait_for(settled(), timeout=5)
    return popup


_SETTLED = (
    "document.readyState === 'complete' && "
    "[...document.images].every((img) => !img.getAttribute('src') || img.complete)"
)
# Each image of the copy: how its src starts, and whether it rendered.
_IMAGES = (
    "[...document.images].map((img) => "
    f"[(img.getAttribute('src') || '').slice(0, {len(PNG_DATA_URL)}), img.naturalWidth])"
)


@requires_chromium
def test_open_replay_opens_a_copy_of_the_page_that_holds_its_screenshots(tmp_path: Path) -> None:
    stub = ReplayStub(_real_page(tmp_path, "step-1.png", "step-2.png"))

    async def scenario(page, dialogs) -> dict:
        popup = await _opened_copy(page)
        return {
            "label": await page.text_content(BUTTON),
            "images": await popup.evaluate(_IMAGES),
            "scripts": await popup.evaluate("document.scripts.length"),
            "heading": await popup.text_content("h1"),
            "id": await popup.text_content(".meta strong"),
            "storage": await popup.evaluate("sessionStorage.length"),
            "opener": await popup.evaluate("window.opener"),
            "dashboard_token": await page.evaluate("sessionStorage.getItem('ab_token')"),
            "note": await page.evaluate(NOTE),
            "dialogs": [dialog for dialog in dialogs if dialog[0] != "prompt"],
        }

    seen = drive(stub, scenario)

    assert seen["label"] == "Open replay"
    # Each screenshot is in the copy itself, and rendered: under a token the page's own <img src> is a 401.
    assert seen["images"] == [[PNG_DATA_URL, 1], [PNG_DATA_URL, 1]]
    assert (seen["heading"], seen["id"]) == ("Session Replay", SESSION)
    # The page and each screenshot were read once, with the token.
    assert stub.tokens == {
        f"/sessions/{SESSION}/replay": [BEARER],
        f"/artifacts/{SESSION}/step-1.png": [BEARER],
        f"/artifacts/{SESSION}/step-2.png": [BEARER],
    }
    # The route's page has no script, and the copy's window no copy of the dashboard's tokens and no way back.
    assert seen["scripts"] == 0
    assert (seen["storage"], seen["opener"]) == (0, None)
    assert seen["dashboard_token"] == "ops-alice"
    assert (seen["note"], seen["dialogs"]) == ("", [])


HOSTILE_PAGE = f"""<!DOCTYPE html>
<html><head><title>Replay</title><script>window.ran = 'a script in the head';</script></head>
<body><h1>Session Replay</h1>
<img id="shot" src="/artifacts/{SESSION}/step-1.png">
<img id="off-artifacts" src="/audit/events?limit=1&amp;name=x.png">
<img id="same-origin" src="{ORIGIN}/audit/events?limit=1&amp;name=y.png">
<img id="climbs" src="/artifacts/../sessions/{SESSION}/replay">
<img id="elsewhere" src="{ELSEWHERE}">
<img id="handler" src="/artifacts/{SESSION}/step-2.png" onload="window.ran = 'an image handler'">
<script>window.ran = 'a script in the body';</script>
</body></html>"""


@requires_chromium
def test_the_copy_runs_no_script_and_the_token_goes_only_to_this_controllers_artifacts() -> None:
    # The route's page holds no script and escapes what it shows. The copy is opened on the dashboard's own origin,
    # so it would not run one either way, and reads with the token only what is a screenshot of this controller.
    # A path-absolute src cannot be loaded from the copy (a blob: document has no base to resolve it by): the
    # absolute one of this origin outside /artifacts is what the copy's policy alone keeps from loading.
    stub = ReplayStub(HOSTILE_PAGE)

    async def scenario(page, dialogs) -> dict:
        await page.context.route("http://elsewhere.test/**", stub.handle)
        # What the copy asks of this origin: drive()'s route is the dashboard page's alone.
        await page.context.route(f"{ORIGIN}/**", stub.handle)
        popup = await _opened_copy(page)
        return {
            "ran": await popup.evaluate("window.ran"),
            "policy": await popup.evaluate("document.head.firstElementChild.content"),
            "widths": await popup.evaluate(
                "Object.fromEntries([...document.images].map((img) => [img.id, img.naturalWidth]))"
            ),
        }

    seen = drive(stub, scenario)

    assert seen["ran"] is None
    assert seen["policy"] == REPLAY_COPY_POLICY
    assert seen["widths"] == {
        "shot": 1,
        "off-artifacts": 0,
        "same-origin": 0,
        "climbs": 0,
        "elsewhere": 0,
        "handler": 1,
    }
    # Nothing but the page and the two screenshots was asked for, by the dashboard or by the copy.
    assert stub.tokens == {
        f"/sessions/{SESSION}/replay": [BEARER],
        f"/artifacts/{SESSION}/step-1.png": [BEARER],
        f"/artifacts/{SESSION}/step-2.png": [BEARER],
    }
    assert [path for path in stub.asked if "name=x.png" in path or "name=y.png" in path] == []


NAVIGATING_PAGE = f"""<!DOCTYPE html>
<html><head><title>Replay</title><base href="{ELSEWHERE_ORIGIN}/based/"></head>
<body><h1>Session Replay</h1>
<form action="{ELSEWHERE_ORIGIN}/submitted" method="get">
<input name="q" value="page text"><button id="submit">go</button>
</form>
<a id="relative" href="relative-link">a relative link</a>
</body></html>"""
REFRESH_PAGE = f"""<!DOCTYPE html>
<html><head><title>Replay</title><meta http-equiv="refresh" content="1;url={ELSEWHERE_ORIGIN}/refreshed"></head>
<body><h1>Session Replay</h1></body></html>"""
# The copy is parsed with scripting off and the window parses it with scripting on: in the copy the refresh is the
# text of a <style>, and in the window a tag after a <noscript> that ends at its first </noscript>.
NOSCRIPT_REFRESH_PAGE = f"""<!DOCTYPE html>
<html><head><title>Replay</title>
<noscript><style></noscript><meta http-equiv="refresh" content="1;url={ELSEWHERE_ORIGIN}/refreshed"></style></noscript>
</head>
<body><h1>Session Replay</h1></body></html>"""
# Each way a page could move the copy's window elsewhere with no script: its markup, and what is clicked.
NAVIGATIONS = {
    "a form": (NAVIGATING_PAGE, "#submit"),
    "a base": (NAVIGATING_PAGE, "#relative"),
    "a refresh": (REFRESH_PAGE, None),
    "a refresh behind a noscript": (NOSCRIPT_REFRESH_PAGE, None),
}
# Longer than the refresh's second: a window that is moving has moved by then.
NAVIGATION_WAIT_MS = 2500


@requires_chromium
@pytest.mark.parametrize("case", list(NAVIGATIONS))
def test_the_copy_is_not_moved_elsewhere_by_a_form_a_base_or_a_refresh(case: str) -> None:
    # None is in the route's page, which escapes what it shows: the copy does not rest on that. With its <base>
    # refused, a relative link has nothing to resolve against in a blob: document, and Chromium refuses it too
    # (about:blank#blocked).
    markup, clicked = NAVIGATIONS[case]
    stub = ReplayStub(markup)

    async def scenario(page, dialogs) -> dict:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        await page.context.route(f"{ELSEWHERE_ORIGIN}/**", stub.handle)
        popup = await _opened_copy(page)
        base = await popup.evaluate("document.baseURI")
        if clicked:
            # A refused submit leaves a navigation scheduled that never ends: the click does not wait for it.
            await popup.click(clicked, no_wait_after=True)
        try:
            await popup.wait_for_url(lambda url: url.startswith(ELSEWHERE_ORIGIN), timeout=NAVIGATION_WAIT_MS)
        except PlaywrightTimeoutError:
            pass
        return {"base": base.split(":", 1)[0], "at": popup.url.split(":", 1)[0]}

    seen = drive(stub, scenario)

    assert seen["base"] == "blob"
    assert seen["at"] in ("blob", "about"), seen
    assert [path for path in stub.asked if path.startswith(ELSEWHERE_ORIGIN)] == []


@requires_chromium
def test_a_screenshot_that_cannot_be_read_says_so_in_the_copy_and_the_rest_are_shown(tmp_path: Path) -> None:
    stub = ReplayStub(_real_page(tmp_path, "step-1.png", "step-2.png"), missing=("step-1.png",))

    async def scenario(page, dialogs) -> dict:
        popup = await _opened_copy(page)
        return {
            "images": await popup.evaluate(_IMAGES),
            "alt": await popup.evaluate("document.images[0].alt"),
            "note": await page.evaluate(NOTE),
        }

    seen = drive(stub, scenario)

    assert seen["images"] == [["", 0], [PNG_DATA_URL, 1]]
    assert seen["alt"] == "screenshot unavailable: Not Found"
    assert seen["note"] == ""


PAGE_FAILURES = {
    "a 404": ((404, {"detail": "Unknown session"}), "Unknown session", "ops-alice"),
    "a 403": ((403, {"detail": "This session belongs to another operator"}), "another operator", "ops-alice"),
    "a 401": ((401, {"detail": "unauthorized"}), "unauthorized: token cleared", None),
    "a 500": ((500, {"detail": "Internal error"}), "Internal error", "ops-alice"),
    "the network down": (NETWORK_DOWN, "network error", "ops-alice"),
}


@requires_chromium
@pytest.mark.parametrize("case", list(PAGE_FAILURES))
def test_a_page_that_cannot_be_read_closes_the_window_and_says_why_with_no_dialog(case: str) -> None:
    failure, reason, token_after = PAGE_FAILURES[case]
    stub = ReplayStub(failure)

    async def scenario(page, dialogs) -> dict:
        await page.wait_for_selector(BUTTON)
        async with page.expect_popup() as popup_info:
            await page.click(BUTTON)
        popup = await popup_info.value
        if not popup.is_closed():
            await popup.wait_for_event("close")
        await page.wait_for_function(f"{NOTE}.length > 0")
        return {
            "note": await page.evaluate(NOTE),
            "dialogs": [dialog for dialog in dialogs if dialog[0] != "prompt"],
            "token": await page.evaluate("sessionStorage.getItem('ab_token')"),
        }

    seen = drive(stub, scenario)

    assert seen["note"].startswith(f"Could not open the replay of session {SESSION}: "), seen["note"]
    assert reason in seen["note"]
    # The new window may be in front of the dashboard: an alert() there is not seen.
    assert seen["dialogs"] == []
    assert seen["token"] == token_after


@requires_chromium
def test_a_blocked_window_says_to_click_again_and_reads_nothing() -> None:
    stub = ReplayStub("<!DOCTYPE html><html><head></head><body></body></html>")

    async def scenario(page, dialogs) -> str:
        await page.wait_for_selector(BUTTON)
        await page.evaluate("window.open = () => null")
        await page.click(BUTTON)
        await page.wait_for_function(f"{NOTE}.includes('blocked')")
        return await page.evaluate(NOTE)

    note = drive(stub, scenario)

    assert note == "The browser blocked the replay window. Click Open replay again."
    assert stub.tokens == {}


@requires_chromium
def test_a_session_id_is_encoded_into_the_pages_url_and_shown_as_text() -> None:
    # An id is a path segment the server checks; the dashboard still writes it as one, and as text.
    session = 'a b/../c?d#e<img src=x onerror="window.ran=1">'
    stub = ReplayStub((404, {"detail": "Unknown session"}), session=session)

    async def scenario(page, dialogs) -> dict:
        await page.wait_for_selector(BUTTON)
        async with page.expect_popup() as popup_info:
            await page.click(BUTTON)
        popup = await popup_info.value
        if not popup.is_closed():
            await popup.wait_for_event("close")
        await page.wait_for_function(f"{NOTE}.length > 0")
        return {"note": await page.evaluate(NOTE), "ran": await page.evaluate("window.ran")}

    seen = drive(stub, scenario)

    assert list(stub.tokens) == [f"/sessions/{quote(session, safe='')}/replay"]
    assert "%2F..%2F" in quote(session, safe="")
    assert seen["note"] == f"Could not open the replay of session {session}: Unknown session"
    assert seen["ran"] is None


@requires_chromium
def test_opening_another_copy_lets_the_one_before_go(tmp_path: Path) -> None:
    # A copy holds every screenshot of its session: the dashboard keeps the last one it opened, not each.
    stub = ReplayStub(_real_page(tmp_path, "step-1.png"))

    async def scenario(page, dialogs) -> dict:
        first = await _opened_copy(page)
        second = await _opened_copy(page)
        readable = "(url) => fetch(url).then(() => true, () => false)"
        return {
            "first": await page.evaluate(readable, first.url),
            "second": await page.evaluate(readable, second.url),
            "first_still_shows": await first.evaluate(_IMAGES),
        }

    seen = drive(stub, scenario)

    assert seen == {"first": False, "second": True, "first_still_shows": [[PNG_DATA_URL, 1]]}

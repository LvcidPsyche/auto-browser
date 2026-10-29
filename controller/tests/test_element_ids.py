"""An element_id names the element observe described, and a page cannot change which one that is.

Observe used to write each element's id into the page as a `data-operator-id`
attribute, and an action found the element again with the selector
`[data-operator-id="<id>"]`. The page owns its DOM, so it could set that
attribute itself: keep its own ids (observe only assigned one where none was
set), give a hidden "Pay now" button the id of the visible "Cancel" button, or
copy an id onto another element once observe had written it. An agent that
clicked "Cancel" by the id it was shown paid. An element_id was also pasted
into the selector unescaped, so a crafted one could add selectors of its own.

These run the real BrowserManager against real Chromium. No traffic leaves the
machine: a local server answers every request. One Chromium serves the module,
on the module's event loop; each test gets its own server, manager and contexts.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from playwright.async_api import Browser, async_playwright

from app.action_errors import BrowserActionError
from app.browser_manager import BrowserManager
from tests._chromium import chromium_executable, requires_chromium
from tests.test_browser_manager_create_session import _settings

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

_SHARED: dict[str, Browser] = {}

# A page that sets data-operator-id itself: the hidden "Pay now" button comes
# first and carries the id the page also gives "Cancel". Observe lists only
# "Cancel" (it skips aria-hidden content), so the agent never sees "Pay now".
FORGED_IDS = """<title>checkout</title>
<div aria-hidden="true"><button id="pay" data-operator-id="op-cancel"
  onclick="document.title='PAID'">Pay now</button></div>
<button id="cancel" data-operator-id="op-cancel" onclick="document.title='CANCELLED'">Cancel</button>
"""

# A page that waits for observe to tag "Cancel" and then gives "Pay now", placed
# first in the document, the same id.
COPIES_IDS = """<title>checkout</title>
<div aria-hidden="true" id="slot"></div>
<button id="cancel" onclick="document.title='CANCELLED'">Cancel</button>
<script>
new MutationObserver(() => {
  const tagged = document.querySelector('#cancel').getAttribute('data-operator-id');
  if (!tagged || document.querySelector('#pay')) return;
  const pay = document.createElement('button');
  pay.id = 'pay';
  pay.textContent = 'Pay now';
  pay.setAttribute('data-operator-id', tagged);
  pay.onclick = () => { document.title = 'PAID'; };
  document.querySelector('#slot').appendChild(pay);
}).observe(document, {subtree: true, attributes: true});
</script>
"""

TWO_BUTTONS = """<title>checkout</title>
<button id="pay" onclick="document.title='PAID'">Pay now</button>
<button id="cancel" onclick="document.title='CANCELLED'">Cancel</button>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        env: Env = self.server.env  # type: ignore[attr-defined]
        path = urlparse(self.path).path
        body = env.pages.get(path, "<title>blank</title>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for name, value in env.headers.get(path, {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@dataclass
class Env:
    manager: BrowserManager
    pages: dict[str, str]
    headers: dict[str, dict[str, str]] = field(default_factory=dict)
    port: int = 0
    contexts: list = field(default_factory=list)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def create(self, path: str) -> str:
        summary = await self.manager.create_session(name="t", start_url=f"{self.origin}{path}")
        return summary["id"]

    def page(self, session_id: str):
        return self.manager.sessions[session_id].page

    async def observe(self, session_id: str) -> list[dict]:
        observation = await self.manager.observe(session_id, preset="text")
        return observation["interactables"]

    async def element(self, session_id: str, label: str) -> dict:
        matches = [item for item in await self.observe(session_id) if item["label"] == label]
        assert len(matches) == 1, f"observe listed {len(matches)} elements labelled {label!r}"
        return matches[0]


class _TrackedBrowser:
    def __init__(self, real: Browser, env: Env) -> None:
        self._real = real
        self._env = env

    async def new_context(self, **kwargs):
        context = await self._real.new_context(**kwargs)
        self._env.contexts.append(context)
        return context

    async def new_browser_cdp_session(self):
        # The pre-request gate's session, on the real browser.
        return await self._real.new_browser_cdp_session()


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _shared_chromium() -> AsyncIterator[None]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
        _SHARED["browser"] = browser
        try:
            yield
        finally:
            _SHARED.clear()
            await browser.close()


@contextlib.asynccontextmanager
async def browser_env(tmp_path: Path, pages: dict[str, str]) -> AsyncIterator[Env]:
    settings = _settings(tmp_path)
    settings.allowed_hosts = "127.0.0.1"
    settings.action_timeout_ms = 3000
    settings.require_approval_for_uploads = False
    manager = BrowserManager(settings)
    manager._maybe_provision_session_tunnel = AsyncMock()
    env = Env(manager=manager, pages=pages)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.env = env  # type: ignore[attr-defined]
    env.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        manager._acquire_session_browser = AsyncMock(return_value=(_TrackedBrowser(_SHARED["browser"], env), None))
        try:
            yield env
        finally:
            for context in list(env.contexts):
                with contextlib.suppress(Exception):
                    await context.close()
            # The gate's session stays on the shared browser otherwise, pausing every later test's navigations.
            await manager.request_gates.release_all()
    finally:
        server.shutdown()
        server.server_close()


# ── The reported attacks ─────────────────────────────────────────────────────


async def test_ids_the_page_sets_itself_do_not_redirect_a_click(tmp_path: Path) -> None:
    """ACT-02: the page gave a hidden "Pay now" the id of "Cancel"; clicking "Cancel" paid."""
    async with browser_env(tmp_path, {"/": FORGED_IDS}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")

        await env.manager.click(session_id, element_id=cancel["element_id"])

        assert await env.page(session_id).title() == "CANCELLED"


async def test_an_id_the_page_copies_after_observe_does_not_redirect_a_click(tmp_path: Path) -> None:
    """NAV-11: the page copied the id observe had just written onto a "Pay now" placed first."""
    async with browser_env(tmp_path, {"/": COPIES_IDS}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")

        await env.manager.click(session_id, element_id=cancel["element_id"])

        assert await env.page(session_id).title() == "CANCELLED"


@pytest.mark.parametrize(
    "crafted",
    [
        'x"], #pay, [data-x="',
        'op-1"] , #pay , [x="',
    ],
)
async def test_a_crafted_element_id_cannot_add_selectors(tmp_path: Path, crafted: str) -> None:
    """An element_id went into the selector unescaped, so it could name any element on the page."""
    async with browser_env(tmp_path, {"/": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        error = None

        try:
            await env.manager.click(session_id, element_id=crafted)
        except BrowserActionError as exc:
            error = exc

        assert await env.page(session_id).title() == "checkout", "the crafted id clicked an element"
        assert error is not None and error.code == "invalid_element_id"
        assert "is not one browser.observe issued" in error.message


# ── What observe issues ──────────────────────────────────────────────────────

ID_FORMAT = re.compile(r"op-[0-9a-f]{24}")

# Records every attribute change, so a test can tell whether the controller wrote anything.
WATCHED = """<title>watched</title>
<a href="/next" id="link">Next page</a>
<input id="email" aria-label="Email">
<button id="cancel" onclick="document.title='CANCELLED'">Cancel</button>
<script>
window.attributeChanges = [];
new MutationObserver((records) => {
  for (const record of records) window.attributeChanges.push(record.attributeName);
}).observe(document, {subtree: true, attributes: true});
</script>
"""


async def test_observe_writes_nothing_into_the_page_and_its_ids_are_controller_ids(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": WATCHED}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        first = await env.observe(session_id)
        assert await page.evaluate("window.attributeChanges") == []
        again = (await env.manager.observe(session_id))["interactables"]
        # A screenshot (the default preset takes one) has Playwright hide the
        # text caret through the focused field's style, and nothing else.
        assert set(await page.evaluate("window.attributeChanges")) <= {"style"}

        assert await page.locator("[data-operator-id]").count() == 0
        ids = [item["element_id"] for item in first]
        assert len(ids) == 3 and len(set(ids)) == 3
        assert all(ID_FORMAT.fullmatch(element_id) for element_id in ids), ids
        assert [item["selector_hint"] for item in first] == [f'[data-operator-id="{i}"]' for i in ids]
        # The same element keeps its id from one observation to the next.
        assert [item["element_id"] for item in again] == ids


WATCHED_FORM = WATCHED.replace(
    '<input id="email"',
    '<select id="size" aria-label="Size"><option value="s">small</option><option value="l">large</option></select>\n'
    '<input id="email"',
)


async def test_click_type_and_select_by_element_id_write_nothing_into_the_page(tmp_path: Path) -> None:
    """Nothing tells the page which element an action is about to use: no marker, no attribute."""
    async with browser_env(tmp_path, {"/": WATCHED_FORM}) as env:
        session_id = await env.create("/")
        size = await env.element(session_id, "Size")
        email = await env.element(session_id, "Email")
        cancel = await env.element(session_id, "Cancel")

        await env.manager.select_option(session_id, element_id=size["element_id"], value="l")
        await env.manager.type(session_id, element_id=email["element_id"], text="a@b.c")
        await env.manager.click(session_id, element_id=cancel["element_id"])

        page = env.page(session_id)
        assert [await page.input_value("#size"), await page.input_value("#email"), await page.title()] == [
            "l",
            "a@b.c",
            "CANCELLED",
        ]
        # The action snapshots' screenshots hide the caret through the focused field's style.
        assert set(await page.evaluate("window.attributeChanges")) <= {"style"}


async def test_the_focused_element_is_reported_by_its_controller_id(tmp_path: Path) -> None:
    forged = "op-" + "a" * 24
    page = f'<title>t</title><input id="email" aria-label="Email" data-operator-id="{forged}">'
    async with browser_env(tmp_path, {"/": page}) as env:
        session_id = await env.create("/")
        email = await env.element(session_id, "Email")
        await env.page(session_id).focus("#email")

        observation = await env.manager.observe(session_id, preset="text")

        assert email["element_id"] != forged
        assert observation["active_element"]["element_id"] == email["element_id"]


# ── Ids that are refused ─────────────────────────────────────────────────────


async def test_an_id_this_tab_was_never_given_is_refused(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        await env.observe(session_id)

        with pytest.raises(BrowserActionError) as raised:
            await env.manager.click(session_id, element_id="op-" + "0" * 24)

        assert raised.value.code == "unknown_element_id"
        assert "browser.observe" in raised.value.message
        assert await env.page(session_id).title() == "checkout"


@pytest.mark.parametrize("move", ["navigate", "reload"])
async def test_an_id_from_before_a_navigation_is_refused_as_stale(tmp_path: Path, move: str) -> None:
    async with browser_env(tmp_path, {"/": TWO_BUTTONS, "/next": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")
        if move == "navigate":
            await env.manager.navigate(session_id, f"{env.origin}/next")
        else:
            await env.manager.reload(session_id)

        with pytest.raises(BrowserActionError) as raised:
            await env.manager.click(session_id, element_id=cancel["element_id"])

        assert raised.value.code == "stale_element_id"
        assert "earlier document" in raised.value.message and "browser.observe" in raised.value.message
        assert await env.page(session_id).title() == "checkout"
        fresh = await env.element(session_id, "Cancel")
        assert fresh["element_id"] != cancel["element_id"]
        await env.manager.click(session_id, element_id=fresh["element_id"])
        assert await env.page(session_id).title() == "CANCELLED"


async def test_an_id_for_an_element_the_page_removed_is_refused_as_stale(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")
        await env.page(session_id).evaluate("document.querySelector('#cancel').remove()")

        with pytest.raises(BrowserActionError) as raised:
            await env.manager.click(session_id, element_id=cancel["element_id"])

        assert raised.value.code == "stale_element_id"
        assert "no longer on the page" in raised.value.message
        assert await env.page(session_id).title() == "checkout"


# Cancel sits over Pay at the top left; a same-origin frame waits further down.
# Moved into the frame, Cancel is still attached, to the frame's document, where
# its box is the frame's: (0, 0) there is Pay's corner in the page.
ADOPTED_BY_A_FRAME = """<title>checkout</title><body style="margin:0">
<button id="pay" style="position:absolute;left:0;top:0;width:120px;height:40px"
  onclick="document.title='PAID'">Pay now</button>
<button id="cancel" style="position:absolute;left:0;top:0;width:120px;height:40px;z-index:1"
  onclick="document.title='CANCELLED'">Cancel</button>
<iframe id="frame" srcdoc="<body style='margin:0'></body>"
  style="position:absolute;left:300px;top:200px;width:300px;height:200px;border:0"></iframe>
<script>
window.log = [];
document.addEventListener('mouseover', (event) => window.log.push('over:' + event.target.id));
</script>
</body>"""


@pytest.mark.parametrize("action", ["click", "hover"])
async def test_an_element_the_page_moves_into_a_frame_is_refused_as_stale(tmp_path: Path, action: str) -> None:
    """Codex, round 4d: the registry checked only that the element was attached, so one adopted by a
    same-origin frame was still found, and its frame-local box sent the pointer to Pay's corner."""
    async with browser_env(tmp_path, {"/": ADOPTED_BY_A_FRAME}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        cancel = await env.element(session_id, "Cancel")
        await page.evaluate(
            """() => new Promise((done) => {
              const frame = document.getElementById('frame');
              const move = () => { frame.contentDocument.body.appendChild(document.getElementById('cancel')); done(); };
              if (frame.contentDocument && frame.contentDocument.readyState === 'complete') move();
              else frame.addEventListener('load', move);
            })"""
        )

        with pytest.raises(BrowserActionError) as raised:
            await getattr(env.manager, action)(session_id, element_id=cancel["element_id"])

        assert raised.value.code == "stale_element_id"
        assert await page.title() == "checkout"
        assert "over:pay" not in await page.evaluate("window.log")


# ── A page working against the ids ───────────────────────────────────────────


async def test_a_page_that_writes_the_real_id_onto_another_element_changes_nothing(tmp_path: Path) -> None:
    """Even an id the page somehow learned is no use to it: no element is found by an attribute."""
    async with browser_env(tmp_path, {"/": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")
        page = env.page(session_id)
        await page.evaluate(
            """(id) => {
              const pay = document.querySelector('#pay');
              pay.setAttribute('data-operator-id', id);
              document.body.prepend(pay);
            }""",
            cancel["element_id"],
        )

        await env.manager.click(session_id, element_id=cancel["element_id"])
        assert await page.title() == "CANCELLED"

        # The selector_hint observe reported, used as a selector, names the same element.
        await page.evaluate("document.title = 'checkout'")
        await env.manager.click(session_id, selector=cancel["selector_hint"])
        assert await page.title() == "CANCELLED"


# Whatever attribute anyone adds to Cancel or Size, the page adds to Pay or Tip too.
COPIES_EVERY_ATTRIBUTE = """<title>checkout</title>
<button id="pay" onclick="document.title='PAID'">Pay now</button>
<button id="cancel" onclick="document.title='CANCELLED'">Cancel</button>
<select id="size" aria-label="Size"><option value="s">small</option><option value="l">large</option></select>
<select id="tip" aria-label="Tip"><option value="s">none</option><option value="l">large tip</option></select>
<script>
const twin = { cancel: 'pay', size: 'tip' };
new MutationObserver((records) => {
  for (const record of records) {
    const copy = twin[record.target.id];
    const value = record.target.getAttribute(record.attributeName);
    if (copy && value !== null) document.getElementById(copy).setAttribute(record.attributeName, value);
  }
}).observe(document, {subtree: true, attributes: true});
</script>
"""


async def test_a_page_that_copies_every_attribute_has_nothing_to_copy(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": COPIES_EVERY_ATTRIBUTE}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")
        size = await env.element(session_id, "Size")
        page = env.page(session_id)

        await env.manager.select_option(session_id, element_id=size["element_id"], value="l")
        await env.manager.click(session_id, element_id=cancel["element_id"])

        assert await page.title() == "CANCELLED"
        assert [await page.input_value("#size"), await page.input_value("#tip")] == ["l", "s"]


# Whatever attribute anyone adds to an element on the left, the page moves to the one on the right.
MOVES_EVERY_ATTRIBUTE = """<title>form</title>
<button id="pay" onclick="document.title='PAID'">Pay now</button>
<button id="cancel" onclick="document.title='CANCELLED'">Cancel</button>
<input id="other" aria-label="Other">
<input id="name" aria-label="Name">
<input id="plain" aria-label="Plain">
<input id="pw" type="password" aria-label="Password">
<select id="tip" aria-label="Tip"><option value="s">none</option><option value="l">large tip</option></select>
<select id="size" aria-label="Size"><option value="s">small</option><option value="l">large</option></select>
<script>
const decoy = { cancel: 'pay', name: 'other', pw: 'plain', size: 'tip' };
new MutationObserver((records) => {
  for (const record of records) {
    const to = decoy[record.target.id];
    const value = record.target.getAttribute(record.attributeName);
    if (!to || value === null) continue;
    record.target.removeAttribute(record.attributeName);
    document.getElementById(to).setAttribute(record.attributeName, value);
  }
}).observe(document, {subtree: true, attributes: true});
</script>
"""


async def test_a_page_that_moves_the_actions_marker_cannot_move_the_click(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": MOVES_EVERY_ATTRIBUTE}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")

        await env.manager.click(session_id, element_id=cancel["element_id"])

        assert await env.page(session_id).title() == "CANCELLED"


async def test_a_page_that_moves_the_actions_marker_cannot_move_the_keystrokes(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": MOVES_EVERY_ATTRIBUTE}) as env:
        session_id = await env.create("/")
        name = await env.element(session_id, "Name")

        await env.manager.type(session_id, element_id=name["element_id"], text="hello")

        page = env.page(session_id)
        assert [await page.input_value("#name"), await page.input_value("#other")] == ["hello", ""]


async def test_the_secret_field_check_reads_the_element_not_the_marker(tmp_path: Path) -> None:
    """Moving the marker to a plain field must not get a password's preview into the action log."""
    async with browser_env(tmp_path, {"/": MOVES_EVERY_ATTRIBUTE}) as env:
        session_id = await env.create("/")
        password = await env.element(session_id, "Password")

        result = await env.manager.type(session_id, element_id=password["element_id"], text="hunter2")

        assert result["target"].get("text_redacted") is True
        assert "text_preview" not in result["target"]
        page = env.page(session_id)
        assert [await page.input_value("#pw"), await page.input_value("#plain")] == ["hunter2", ""]


async def test_a_page_that_moves_every_attribute_cannot_move_the_selection(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": MOVES_EVERY_ATTRIBUTE}) as env:
        session_id = await env.create("/")
        size = await env.element(session_id, "Size")

        await env.manager.select_option(session_id, element_id=size["element_id"], value="l")

        page = env.page(session_id)
        assert [await page.input_value("#size"), await page.input_value("#tip")] == ["l", "s"]


COVERED = """<title>covered</title><body style="margin:40px">
<button id="cancel" style="display:block;width:200px;height:40px" onclick="document.title='CANCELLED'">Cancel</button>
<button id="evil" style="position:absolute;left:40px;top:40px;width:200px;height:40px;opacity:0;z-index:9"
  onclick="document.title='EVIL'">x</button>
</body>"""


async def test_an_element_named_by_id_is_not_clicked_through_a_cover(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": COVERED}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")

        with pytest.raises(BrowserActionError) as raised:
            await env.manager.click(session_id, element_id=cancel["element_id"])

        assert raised.value.code == "element_covered"
        assert cancel["element_id"] in raised.value.message and "nothing was clicked" in raised.value.message
        assert await env.page(session_id).title() == "covered"


# ── Coverage: tabs, policies, every action, the records ─────────────────────

POPUP = """<title>popup</title><button id="confirm" onclick="document.title='CONFIRMED'">Confirm</button>"""


async def test_a_popup_has_ids_of_its_own(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": TWO_BUTTONS, "/popup": POPUP}) as env:
        session_id = await env.create("/")
        opener_cancel = await env.element(session_id, "Cancel")
        session = env.manager.sessions[session_id]
        async with session.context.expect_page() as opened:
            await session.page.evaluate("(url) => window.open(url)", f"{env.origin}/popup")
        popup = await opened.value
        await popup.wait_for_load_state()
        await env.manager.activate_tab(session_id, 1)
        confirm = await env.element(session_id, "Confirm")

        with pytest.raises(BrowserActionError) as raised:
            await env.manager.click(session_id, element_id=opener_cancel["element_id"])
        assert raised.value.code == "unknown_element_id"
        assert "not issued for the page in the current tab" in raised.value.message

        await env.manager.click(session_id, element_id=confirm["element_id"])
        assert await popup.title() == "CONFIRMED"
        assert await session.context.pages[0].title() == "checkout"


CSP_PAGE = """<title>csp</title><input id="agree" type="checkbox" aria-label="Agree">
<input id="note" aria-label="Note">"""
STRICT_CSP = "default-src 'none'; script-src 'none'; require-trusted-types-for 'script'"


async def test_a_page_with_a_strict_content_security_policy_is_observed_and_acted_on(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": CSP_PAGE}) as env:
        env.headers["/"] = {"Content-Security-Policy": STRICT_CSP}
        session_id = await env.create("/")
        note = await env.element(session_id, "Note")
        agree = await env.element(session_id, "Agree")

        await env.manager.type(session_id, element_id=note["element_id"], text="ok")
        await env.manager.click(session_id, element_id=agree["element_id"])

        page = env.page(session_id)
        assert await page.input_value("#note") == "ok"
        assert await page.is_checked("#agree"), "the click by element_id did not reach the checkbox"


CONTROLS = """<title>controls</title>
<button id="menu" onmouseenter="document.title='HOVERED'">Menu</button>
<select id="size" aria-label="Size" onchange="document.title='SIZE ' + this.value">
  <option value="s">small</option><option value="l">large</option></select>
<input id="file" type="file" aria-label="Attachment" onchange="document.title='FILE ' + this.files[0].name">
"""


async def test_hover_select_and_upload_by_element_id(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": CONTROLS}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        upload = Path(env.manager.settings.upload_root) / "report.txt"
        upload.write_text("quarterly", encoding="utf-8")

        await env.manager.hover(session_id, element_id=(await env.element(session_id, "Menu"))["element_id"])
        assert await page.title() == "HOVERED"
        size = await env.element(session_id, "Size")
        await env.manager.select_option(session_id, element_id=size["element_id"], value="l")
        assert await page.title() == "SIZE l"
        attachment = await env.element(session_id, "Attachment")
        await env.manager.upload(
            session_id, file_path="report.txt", approved=False, element_id=attachment["element_id"]
        )
        assert await page.title() == "FILE report.txt"


async def test_the_action_log_and_audit_record_the_element_id_and_its_hint(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": TWO_BUTTONS}) as env:
        session_id = await env.create("/")
        cancel = await env.element(session_id, "Cancel")

        await env.manager.click(session_id, element_id=cancel["element_id"])

        session = env.manager.sessions[session_id]
        logged = json.loads((session.artifact_dir / "actions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        audited = [
            event.details["target"]
            for event in await env.manager.audit.list(session_id=session_id, limit=20)
            if event.action == "click"
        ]
        expected = {"element_id": cancel["element_id"], "selector": cancel["selector_hint"]}
        for target in (logged["target"], audited[0]):
            assert {key: target.get(key) for key in expected} == expected
        assert logged["verification"]["target_seen_after"] is True

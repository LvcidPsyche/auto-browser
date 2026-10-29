"""ALLOWED_HOSTS has to hold wherever a page can end up, not only where the controller sent it.

The list was checked on the URL a caller asked for and, after an action, on the
URL the page landed on. Four places moved a session onto a disallowed host
without either check, and one more let the page get there by itself:

* create_session followed a redirect from an allowed start URL and kept the session.
* activate_tab switched to a popup that was sitting on a disallowed host.
* a blocked action's rollback (`go_back`) does nothing after `location.replace`,
  so the session stayed on the disallowed page and the 403 carried its content.
* a page that navigates itself after an action has finished (a timer) was then observed.

These run the real BrowserManager against real Chromium. No traffic leaves the
machine: every context answers each request itself, by host.

One Chromium serves the whole module (starting it costs seconds per test), so the
tests share an event loop for the module. Each test still gets its own server,
manager and browser contexts, and closes the contexts it opened.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from playwright.async_api import Browser, async_playwright

from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError
from app.browser.services.diagnostics import BrowserDiagnosticsService
from app.browser_manager import BrowserManager
from app.mcp_transport import McpHttpTransport
from app.models import BrowserActionDecision
from app.tool_gateway import McpToolGateway
from app.tool_inputs import (
    DragDropInput,
    EvalJsInput,
    FindElementsInput,
    GetPageHtmlInput,
    GetStorageInput,
    SetStorageInput,
    WaitForSelectorInput,
)
from tests._background import pending
from tests._chromium import chromium_executable, requires_chromium
from tests.test_browser_manager_create_session import _settings

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

_SHARED: dict[str, Browser] = {}


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def _shared_chromium() -> AsyncIterator[None]:
    """Launch one Chromium for every test in this module, on the module's event loop."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
        _SHARED["browser"] = browser
        try:
            yield
        finally:
            _SHARED.clear()
            await browser.close()


SECRET = "TOP-SECRET-INTERNAL-TEXT"
EVIL_PAGE = f"<title>EVIL-TITLE</title><p>{SECRET}</p>"
ALLOWED_HOST = "localhost"
EVIL_HOST = "127.0.0.1"


def redirect(location: str) -> tuple[int, str]:
    return (302, location)


class _Handler(BaseHTTPRequestHandler):
    """Serves env.pages on `localhost` and EVIL_PAGE on `127.0.0.1`: one machine, two hostnames."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        env: Env = self.server.env  # type: ignore[attr-defined]
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        path = urlparse(self.path).path
        entry = EVIL_PAGE if host == EVIL_HOST else env.pages.get(path, "<title>allowed</title><p>hello</p>")
        if isinstance(entry, tuple):
            status, location = entry
            self.send_response(status)
            self.send_header("Location", env.fill(location))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = env.fill(entry).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@dataclass
class Env:
    """A BrowserManager browsing a local server. `localhost` is allowed; `127.0.0.1`, the same server, is not.

    Page bodies may use {ALLOWED} and {EVIL} for the two origins. A real server
    rather than context.route: Chromium does not re-route the second leg of a
    route.fulfill(302), so a redirect can only be tested against a real one.
    """

    manager: BrowserManager
    pages: dict[str, str | tuple[int, str]]
    port: int = 0
    contexts: list = field(default_factory=list)
    closed_contexts: list = field(default_factory=list)

    @property
    def allowed(self) -> str:
        return f"http://{ALLOWED_HOST}:{self.port}"

    @property
    def evil(self) -> str:
        return f"http://{EVIL_HOST}:{self.port}"

    def fill(self, text: str) -> str:
        return text.replace("{ALLOWED}", self.allowed).replace("{EVIL}", self.evil)

    async def create(self, path: str = "/") -> str:
        summary = await self.manager.create_session(name="t", start_url=f"{self.allowed}{path}")
        return summary["id"]


async def wait_for(condition: Callable[[], bool | Awaitable[bool]], *, timeout: float = 10.0) -> None:
    """Poll until `condition` holds; fail with the timeout instead of sleeping a fixed time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout}s")


class _TrackedBrowser:
    """Hands the manager the real browser, and remembers each context it opens."""

    def __init__(self, real, env: Env) -> None:
        self._real = real
        self._env = env

    async def new_context(self, **kwargs):
        context = await self._real.new_context(**kwargs)
        self._env.contexts.append(context)
        context.on("close", lambda *_: self._env.closed_contexts.append(context))
        return context


@contextlib.asynccontextmanager
async def browser_env(
    tmp_path: Path,
    pages: dict[str, str | tuple[int, str]],
    *,
    allowed_hosts: str = ALLOWED_HOST,
) -> AsyncIterator[Env]:
    settings = _settings(tmp_path)
    settings.allowed_hosts = allowed_hosts
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
    finally:
        server.shutdown()
        server.server_close()


def switch_off_page_guard(monkeypatch) -> None:
    """Disable the framenavigated guard, to test what the rest of the code does without it."""
    monkeypatch.setattr(
        BrowserDiagnosticsService, "_on_frame_navigated", lambda self, session, page, frame, **_: None, raising=False
    )


# ── H-4a: create_session ─────────────────────────────────────────────────────


async def test_create_session_refuses_a_start_url_that_redirects_off_the_list(tmp_path: Path) -> None:
    """An allowed start URL that 302s to a disallowed host must not leave a live session there."""
    async with browser_env(tmp_path, {"/redir": redirect("{EVIL}/")}) as env:
        with pytest.raises(PermissionError, match="127.0.0.1"):
            await env.create("/redir")

        assert env.manager.sessions == {}
        assert len(env.contexts) == 1
        assert env.closed_contexts == env.contexts, "the failed session's browser context was left open"


async def test_create_session_still_follows_a_redirect_between_allowed_pages(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/hop": redirect("{ALLOWED}/final"), "/final": "<p>final</p>"}) as env:
        session_id = await env.create("/hop")

        assert env.manager.sessions[session_id].page.url == f"{env.allowed}/final"


# ── H-4c: activate_tab ───────────────────────────────────────────────────────


async def open_popup(env: Env, session_id: str, url: str):
    """Open `url` in a second tab of the session, the way a page's own window.open does."""
    session = env.manager.sessions[session_id]
    async with session.context.expect_page() as opened:
        await session.page.evaluate("(url) => window.open(url)", url)
    popup = await opened.value
    await popup.wait_for_load_state()
    return popup


async def test_activate_tab_refuses_a_popup_on_a_disallowed_host(tmp_path: Path, monkeypatch) -> None:
    switch_off_page_guard(monkeypatch)  # the popup has to stay on the disallowed host for this test
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        original = session.page
        popup = await open_popup(env, session_id, f"{env.evil}/")
        assert popup.url.startswith(env.evil)

        with pytest.raises(PermissionError, match="cannot be activated. Close it"):
            await env.manager.activate_tab(session_id, 1)

        assert session.page is original, "the session was switched onto the disallowed tab"
        observed = await env.manager.observe(session_id, preset="text")
        assert SECRET not in str(observed)


async def test_activate_tab_still_switches_between_allowed_tabs(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/second": "<title>second</title>"}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        popup = await open_popup(env, session_id, f"{env.allowed}/second")

        await env.manager.activate_tab(session_id, 1)

        assert session.page is popup


# ── H-4b: a blocked action that cannot be rolled back ────────────────────────

REPLACE_PAGE = "<title>allowed</title><button id=go onclick=\"location.replace('{EVIL}/')\">go</button>"


async def popup_with_replace_button(env: Env) -> tuple[str, object]:
    """A session whose active tab is a popup, whose only history entry is a page that replaces itself.

    A popup has no earlier entry to go back to, which is what leaves a blocked
    location.replace() with nowhere to roll back to.
    """
    session_id = await env.create()
    popup = await open_popup(env, session_id, f"{env.allowed}/replace")
    await env.manager.activate_tab(session_id, 1)
    return session_id, popup


async def test_blocked_replace_in_a_popup_leaves_the_page_blank_and_the_snapshot_clean(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/replace": REPLACE_PAGE}) as env:
        session_id, popup = await popup_with_replace_button(env)

        with pytest.raises(BrowserActionError) as blocked:
            await env.manager.click(session_id, selector="#go")

        assert blocked.value.code == "browser_action_blocked"
        assert SECRET not in str(blocked.value.payload), "the 403 carried the disallowed page's content"
        assert popup.url == "about:blank"
        # Blank, and still refused until the agent navigates it to an allowed URL.
        with pytest.raises(PermissionError, match="until it is navigated to an allowed URL") as refused:
            await env.manager.observe(session_id, preset="text")
        assert SECRET not in str(refused.value)


async def test_blocked_action_that_cannot_leave_the_page_withholds_its_content(tmp_path: Path, monkeypatch) -> None:
    """If even the move to about:blank fails, the 403 reports the URL and no page content."""
    switch_off_page_guard(monkeypatch)  # it would try the same refused move and log the failure
    async with browser_env(tmp_path, {"/replace": REPLACE_PAGE}) as env:
        session_id, popup = await popup_with_replace_button(env)
        popup.goto = AsyncMock(side_effect=RuntimeError("navigation refused"))

        with pytest.raises(BrowserActionError) as blocked:
            await env.manager.click(session_id, selector="#go")

        snapshot = blocked.value.details["snapshot"]
        assert SECRET not in str(blocked.value.payload)
        assert snapshot["url"].startswith(env.evil)
        assert "snapshot_error" in snapshot


async def test_blocked_navigation_where_history_exists_is_still_rolled_back(tmp_path: Path) -> None:
    """The case that worked before: location.assign leaves a history entry, and go_back returns to it."""
    page = "<title>allowed</title><button id=go onclick=\"location.assign('{EVIL}/')\">go</button>"
    async with browser_env(tmp_path, {"/assign": page}) as env:
        session_id = await env.create("/assign")

        with pytest.raises(BrowserActionError) as blocked:
            await env.manager.click(session_id, selector="#go")

        assert blocked.value.code == "browser_action_blocked"
        assert SECRET not in str(blocked.value.payload)
        assert env.manager.sessions[session_id].page.url == f"{env.allowed}/assign"
        # Back on the page read before the click, the page is read again.
        observed = await env.manager.observe(session_id, preset="text")
        assert SECRET not in str(observed)


# ── N-1: a page that navigates itself after the action has finished ──────────

TIMER_PAGE = (
    "<title>allowed</title><p>hello</p>"
    "<script>window.later = (url) => setTimeout(() => { location.href = url; }, 100);</script>"
)


async def navigate_later(page, url: str) -> None:
    await page.evaluate("(url) => window.later(url)", url)


def audit_spy(env: Env) -> AsyncMock:
    env.manager.audit.append = AsyncMock(wraps=env.manager.audit.append)
    return env.manager.audit.append


def blocked_events(spy: AsyncMock) -> list:
    return [call for call in spy.call_args_list if call.kwargs.get("event_type") == "navigation_blocked"]


async def test_page_that_navigates_itself_off_the_list_is_returned_to_blank(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/timer": TIMER_PAGE}) as env:
        session_id = await env.create("/timer")
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)

        await navigate_later(session.page, f"{env.evil}/")

        await wait_for(lambda: session.page.url == "about:blank", timeout=5)
        # Still refused on the controller's blank, until the agent navigates it to an allowed URL.
        with pytest.raises(PermissionError, match="until it is navigated to an allowed URL"):
            await env.manager.observe(session_id, preset="text")
        await env.manager.navigate(session_id, url=f"{env.allowed}/back")
        observed = await env.manager.observe(session_id, preset="text")
        assert SECRET not in str(observed)
        (event,) = blocked_events(spy)
        assert event.kwargs["status"] == "blocked"
        assert event.kwargs["session_id"] == session_id
        assert event.kwargs["details"]["url"].startswith(env.evil)
        assert event.kwargs["details"]["source"] == "page"


async def test_popup_that_navigates_itself_off_the_list_is_returned_to_blank(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/timer": TIMER_PAGE}) as env:
        session_id = await env.create()
        popup = await open_popup(env, session_id, f"{env.allowed}/timer")

        await navigate_later(popup, f"{env.evil}/")

        await wait_for(lambda: popup.url == "about:blank", timeout=5)


async def test_guard_leaves_a_page_alone_that_is_back_on_an_allowed_url_by_the_time_it_gets_the_lock(
    tmp_path: Path,
) -> None:
    """The agent's navigate in flight holds the lock and takes the page back itself; the guard must not
    undo that. (Only the agent's own navigation clears the taint the refused commit left, so the
    navigation is marked as the navigate action marks it.)"""
    async with browser_env(tmp_path, {"/timer": TIMER_PAGE}) as env:
        session_id = await env.create("/timer")
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)

        async with session.lock:
            await navigate_later(session.page, f"{env.evil}/")
            await wait_for(lambda: session.page.url.startswith(env.evil), timeout=5)
            with env.manager.page_provenance.controller_navigation(session.page) as navigation:
                navigation.response = await session.page.goto(f"{env.allowed}/back")
        await wait_for(lambda: not pending(), timeout=5)

        assert session.page.url == f"{env.allowed}/back"
        assert blocked_events(spy) == []


async def test_guard_does_nothing_for_a_page_that_closed_while_it_waited(tmp_path: Path, caplog) -> None:
    async with browser_env(tmp_path, {"/timer": TIMER_PAGE}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        popup = await open_popup(env, session_id, f"{env.allowed}/timer")
        spy = audit_spy(env)

        async with session.lock:
            await navigate_later(popup, f"{env.evil}/")
            await wait_for(lambda: popup.url.startswith(env.evil), timeout=5)
            await popup.close()
        await wait_for(lambda: not pending(), timeout=5)

        assert blocked_events(spy) == []
        assert not [record for record in caplog.records if record.levelname == "ERROR"]


@pytest.mark.parametrize("allowed_hosts, target", [("localhost", "allowed"), ("*", "evil")])
async def test_self_navigation_the_list_permits_is_left_alone(tmp_path: Path, allowed_hosts: str, target: str) -> None:
    async with browser_env(tmp_path, {"/timer": TIMER_PAGE}, allowed_hosts=allowed_hosts) as env:
        session_id = await env.create("/timer")
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)
        destination = f"{env.allowed if target == 'allowed' else env.evil}/other"

        await navigate_later(session.page, destination)
        await wait_for(lambda: session.page.url == destination, timeout=5)
        await env.manager.observe(session_id, preset="text")  # queues behind the lock, so after any guard task

        assert session.page.url == destination
        assert blocked_events(spy) == []


# Every way the controller hands page content to a caller. With the guard off and the
# page on a disallowed host, each must refuse before it reads anything.


def _gateway(env: Env) -> McpToolGateway:
    return McpToolGateway(manager=env.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())


READS = {
    "observe-text": lambda env, sid: env.manager.observe(sid, preset="text"),
    "observe-fast": lambda env, sid: env.manager.observe(sid, preset="fast"),
    "observe-normal": lambda env, sid: env.manager.observe(sid, preset="normal"),
    "observe-rich": lambda env, sid: env.manager.observe(sid, preset="rich"),
    "screenshot": lambda env, sid: env.manager.capture_screenshot(sid),
    "screenshot-diff": lambda env, sid: env.manager.screenshot_diff(sid),
    "eval-js": lambda env, sid: _gateway(env)._eval_js(EvalJsInput(session_id=sid, expression="document.title")),
    "wait-for-selector": lambda env, sid: _gateway(env)._wait_for_selector(
        WaitForSelectorInput(session_id=sid, selector="p", state="attached")
    ),
    "get-html": lambda env, sid: _gateway(env)._get_html(GetPageHtmlInput(session_id=sid)),
    "get-text": lambda env, sid: _gateway(env)._get_html(GetPageHtmlInput(session_id=sid, text_only=True)),
    "find-by-query": lambda env, sid: _gateway(env)._find_elements(FindElementsInput(session_id=sid, query="SECRET")),
    "find-by-selector": lambda env, sid: _gateway(env)._find_elements(FindElementsInput(session_id=sid, selector="p")),
    "drag-drop": lambda env, sid: _gateway(env)._drag_drop(
        DragDropInput(session_id=sid, source_selector="p", target_selector="p")
    ),
    "storage-get-key": lambda env, sid: _gateway(env)._get_local_storage(GetStorageInput(session_id=sid, key="k")),
    "storage-get-all": lambda env, sid: _gateway(env)._get_local_storage(GetStorageInput(session_id=sid)),
    "storage-set": lambda env, sid: _gateway(env)._set_local_storage(
        SetStorageInput(session_id=sid, key="k", value="v")
    ),
}


async def off_list_session(env: Env, monkeypatch) -> tuple[str, object]:
    """A session whose page sits on the disallowed host, put there by the test, with the guard off."""
    switch_off_page_guard(monkeypatch)
    session_id = await env.create()
    session = env.manager.sessions[session_id]
    await session.page.goto(f"{env.evil}/")
    return session_id, session


@pytest.mark.parametrize("read", sorted(READS))
async def test_reads_refuse_while_the_page_is_on_a_disallowed_host(tmp_path: Path, monkeypatch, read: str) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id, _ = await off_list_session(env, monkeypatch)

        with pytest.raises(PermissionError, match="about:blank"):
            await READS[read](env, session_id)


async def test_mcp_dom_and_screenshot_resources_return_nothing_for_a_disallowed_page(
    tmp_path: Path, monkeypatch
) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id, _ = await off_list_session(env, monkeypatch)
        transport = McpHttpTransport(tool_gateway=None, server_name="t", server_version="1", manager=env.manager)

        assert await transport._read_resource(f"browser://{session_id}/dom") is None
        assert await transport._read_resource(f"browser://{session_id}/screenshot") is None


async def test_titles_of_a_disallowed_page_are_not_reported_in_summaries(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id, _ = await off_list_session(env, monkeypatch)

        listed = [await env.manager.list_tabs(session_id), await env.manager.get_session_record(session_id)]

        assert "EVIL-TITLE" not in str(listed)


async def test_actions_other_than_navigation_are_refused_while_the_page_is_on_a_disallowed_host(
    tmp_path: Path, monkeypatch
) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id, session = await off_list_session(env, monkeypatch)
        operation = AsyncMock()

        with pytest.raises(BrowserActionError) as refused:
            await env.manager._run_action(session, "type", {}, operation)

        operation.assert_not_awaited()
        assert refused.value.code == "browser_action_blocked"
        assert SECRET not in str(refused.value.payload)
        assert SECRET not in (session.artifact_dir / "actions.jsonl").read_text(encoding="utf-8")
        assert session.page.url == "about:blank"


async def test_reload_is_refused_while_the_page_is_on_a_disallowed_host(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id, session = await off_list_session(env, monkeypatch)
        session.page.reload = AsyncMock()

        with pytest.raises(BrowserActionError) as refused:
            await env.manager.reload(session_id)

        assert refused.value.code == "browser_action_blocked"
        session.page.reload.assert_not_awaited()


async def test_navigate_and_go_back_still_work_from_a_disallowed_page(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {"/ok": "<title>ok</title><p>fine</p>"}) as env:
        session_id, session = await off_list_session(env, monkeypatch)

        went_back = await env.manager.go_back(session_id)
        assert session.page.url == f"{env.allowed}/"
        assert SECRET not in str(went_back)

        await session.page.goto(f"{env.evil}/")
        navigated = await env.manager.navigate(session_id, f"{env.allowed}/ok")
        assert session.page.url == f"{env.allowed}/ok"
        assert SECRET not in str(navigated)


async def test_guard_does_nothing_for_a_session_that_closed_while_it_waited(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/timer": TIMER_PAGE}) as env:
        session_id = await env.create("/timer")
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)

        async with session.lock:
            await navigate_later(session.page, f"{env.evil}/")
            await wait_for(lambda: session.page.url.startswith(env.evil), timeout=5)
            del env.manager.sessions[session_id]  # what closing the session does first
        await wait_for(lambda: not pending(), timeout=5)

        assert session.page.url.startswith(env.evil), "the guard acted on a session that was already gone"
        assert blocked_events(spy) == []


async def test_an_iframe_on_another_host_is_not_filtered(tmp_path: Path, monkeypatch) -> None:
    """Only the top-level page is held to the list: iframes and subresources are not."""
    corrections = AsyncMock()
    monkeypatch.setattr(BrowserDiagnosticsService, "_return_to_blank", corrections, raising=False)
    framed = "<title>allowed</title><p>hello</p><iframe src='{EVIL}/'></iframe>"
    async with browser_env(tmp_path, {"/framed": framed}) as env:
        session_id = await env.create("/framed")
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)

        await wait_for(lambda: any(frame.url.startswith(env.evil) for frame in session.page.frames), timeout=5)
        observed = await env.manager.observe(session_id, preset="text")

        assert session.page.url == f"{env.allowed}/framed"
        assert blocked_events(spy) == []
        assert observed["url"] == f"{env.allowed}/framed"
        corrections.assert_not_called()  # a subframe navigation does not even queue a correction


async def test_an_action_whose_page_moves_off_the_list_after_its_check_reports_no_page_content(
    tmp_path: Path, monkeypatch
) -> None:
    """The page leaves between the action's own check and its after-observation: the action ran, so it is reported."""
    async with browser_env(tmp_path, {}) as env:
        switch_off_page_guard(monkeypatch)
        session_id = await env.create()
        session = env.manager.sessions[session_id]

        async def move_off_the_list(_session):
            await session.page.goto(f"{env.evil}/")

        env.manager._maybe_handle_totp = move_off_the_list

        result = await env.manager.wait(session_id, 10)

        assert SECRET not in str(result)
        assert "ALLOWED_HOSTS" in result["after"]["observation_error"]


async def test_light_snapshot_refuses_a_disallowed_page(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id, session = await off_list_session(env, monkeypatch)

        env.manager.observation.page_summary = AsyncMock()

        with pytest.raises(PermissionError, match="about:blank"):
            await env.manager._light_snapshot(session, label="t")

        # Refused before anything is read, not just because the screenshot beside it was.
        env.manager.observation.page_summary.assert_not_called()


async def test_saved_auth_profile_does_not_record_the_title_of_a_disallowed_page(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id, _ = await off_list_session(env, monkeypatch)

        await env.manager.save_auth_profile(session_id, "p1")

        assert (await env.manager.get_auth_profile("p1"))["metadata"]["saved_from_title"] == ""


async def test_approval_request_does_not_carry_the_title_of_a_disallowed_page(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id, _ = await off_list_session(env, monkeypatch)
        decision = BrowserActionDecision(action="click", reason="test", risk_category="post", selector="p")

        with pytest.raises(ApprovalRequiredError) as raised:
            await env.manager.actions.require_decision_approval(session_id, decision, approval_id=None)

        assert "EVIL-TITLE" not in str(raised.value.payload)

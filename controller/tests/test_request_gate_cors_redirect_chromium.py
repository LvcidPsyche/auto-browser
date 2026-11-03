"""A gated page's credentialed, preflighted fetch still follows a cross-origin redirect (Playwright #43242).

Chromium 155 and later abort a preflighted cross-origin redirect with net::ERR_ABORTED while Fetch interception is
enabled, and the pre-request gate enables it (Fetch.enable on Document requests, app/browser/request_gate.py).
Playwright 1.64 disables AvoidCorsURLLoaderRestartOnRedirect for that, and launched browsers keep Playwright's
list (app/browser/launch_flags.py, browser-node/server.mjs), so the redirect goes through. The control launches
with Playwright 1.62's list, which lacks the feature, and Chromium aborts the same fetch.

The browser is launched as the shadow launch launches one. Loopback hosts only.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from app.browser.launch_flags import PLAYWRIGHT_DISABLED_FEATURES_BUT_PARTITIONING, STORAGE_PARTITIONING_ON
from app.browser.request_gate import REDIRECT_ORIGIN_SWITCH
from tests._chromium import chromium_executable, requires_chromium
from tests.test_request_gate_attach_chromium import attached
from tests.test_request_gate_chromium import ALLOWED, gated

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

API_HOST = "api.localhost"
FILES_HOST = "files.localhost"
BODY = b"\x07" * 4096
AVOID_CORS_RESTART = "AvoidCorsURLLoaderRestartOnRedirect"
# Playwright 1.62's list, without partitioning, as launch_flags carried it before the 1.64 bump.
LIST_1_62 = [feature for feature in PLAYWRIGHT_DISABLED_FEATURES_BUT_PARTITIONING if feature != AVOID_CORS_RESTART]

FETCH_JS = """async (url) => {
  try {
    const response = await fetch(url, { credentials: 'include', headers: { 'X-App-Version': '1.2.3' } });
    return `${response.status} ${(await response.arrayBuffer()).byteLength}`;
  } catch (error) { return `ERR ${error}`; }
}"""


class _CorsServer(BaseHTTPRequestHandler):
    """api.localhost answers 302 to files.localhost, which serves BODY; both allow the credentialed preflight.

    The allowed origins are fixed, not echoed from the request: the page's origin at api.localhost, and "null" at
    files.localhost, the origin a credentialed CORS request carries after a redirect to a third origin.
    """

    hits: list[tuple[str, str]]

    def _cors(self, host: str) -> None:
        allowed = "null" if host == FILES_HOST else self.server.page_origin  # type: ignore[attr-defined]
        self.send_header("Access-Control-Allow-Origin", allowed)
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Headers", "x-app-version")
        self.send_header("Access-Control-Allow-Methods", "GET")

    def _serve(self) -> None:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        self.server.hits.append((host, self.command))  # type: ignore[attr-defined]
        if self.command == "OPTIONS":
            self.send_response(204)
            self._cors(host)
            self.end_headers()
            return
        if host == API_HOST:
            self.send_response(302)
            self._cors(host)
            self.send_header("Location", f"http://{FILES_HOST}:{self.server.server_address[1]}/file")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self._cors(host)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    do_GET = _serve  # noqa: N815
    do_OPTIONS = _serve  # noqa: N815

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def cors_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CorsServer)
    server.hits = []  # type: ignore[attr-defined]
    server.page_origin = ""  # type: ignore[attr-defined]  # set once the gated page has loaded
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


async def _fetch_through_a_gated_page(tmp_path: Path, server: ThreadingHTTPServer, switch: str) -> str:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True, args=[switch])
        try:
            async with gated(tmp_path, browser=browser) as env:
                session_id = await env.create("/")
                assert env.gate() is not None and env.gate().live
                page = env.manager.sessions[session_id].page
                server.page_origin = await page.evaluate("location.origin")  # type: ignore[attr-defined]
                return await page.evaluate(FETCH_JS, f"http://{API_HOST}:{server.server_address[1]}/dl")
        finally:
            await browser.close()


async def test_a_gated_pages_preflighted_fetch_follows_a_cross_origin_redirect(
    tmp_path: Path, cors_server: ThreadingHTTPServer
) -> None:
    result = await _fetch_through_a_gated_page(tmp_path, cors_server, STORAGE_PARTITIONING_ON)

    assert result == f"200 {len(BODY)}"
    assert (FILES_HOST, "GET") in cors_server.hits  # type: ignore[attr-defined]


async def test_control_without_the_feature_disabled_chromium_aborts_that_redirect_under_the_gate(
    tmp_path: Path, cors_server: ThreadingHTTPServer
) -> None:
    """If this starts passing through, Chromium fixed #43242 and the feature can leave Playwright's list."""
    result = await _fetch_through_a_gated_page(tmp_path, cors_server, "--disable-features=" + ",".join(LIST_1_62))

    assert result.startswith("ERR "), result
    assert (FILES_HOST, "GET") not in cors_server.hits  # type: ignore[attr-defined]


# ── attach modes: the operator starts Chrome, so the operator passes the switch ──


async def _fetch_through_an_attached_chrome(
    tmp_path: Path, server: ThreadingHTTPServer, *chrome_args: str
) -> tuple[str, str]:
    """The fetch from a session's page and from the operator's own tab, while the gate is attached."""
    url = f"http://{API_HOST}:{server.server_address[1]}/dl"
    async with attached(tmp_path, *chrome_args, switch_declared=bool(chrome_args)) as env:
        session_id = await env.create("/")
        page = env.manager.sessions[session_id].page
        server.page_origin = await page.evaluate("location.origin")  # type: ignore[attr-defined]
        from_session = await page.evaluate(FETCH_JS, url)
        [tab] = [tab for context in env.browser.contexts for tab in context.pages if tab.url == "about:blank"][:1]
        await tab.goto(env.server.url(ALLOWED, "/operator"))
        server.page_origin = await tab.evaluate("location.origin")  # type: ignore[attr-defined]
        from_operator_tab = await tab.evaluate(FETCH_JS, url)
    return from_session, from_operator_tab


async def test_an_attached_chrome_started_with_the_documented_switch_follows_the_redirect(
    tmp_path: Path, cors_server: ThreadingHTTPServer
) -> None:
    results = await _fetch_through_an_attached_chrome(tmp_path, cors_server, REDIRECT_ORIGIN_SWITCH)

    assert results == (f"200 {len(BODY)}", f"200 {len(BODY)}")


async def test_control_the_old_switch_alone_leaves_every_tab_aborting_the_redirect(
    tmp_path: Path, cors_server: ThreadingHTTPServer
) -> None:
    """The switch 1.12.4 documented: Chrome 155+ still aborts the redirect, in the operator's own tab too."""
    session, operator_tab = await _fetch_through_an_attached_chrome(
        tmp_path, cors_server, "--disable-features=BlockOriginHeaderModificationOnRedirect"
    )

    assert session.startswith("ERR ") and operator_tab.startswith("ERR "), (session, operator_tab)

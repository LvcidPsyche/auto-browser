"""What a page ALLOWED_HOSTS refuses does while it runs must not reach the agent by another channel.

A refused page (a popup opened onto a disallowed host, or a page that moved itself there)
runs until the page guard sends it to about:blank, which waits for any call in flight on
the session. Its console text and page errors were already dropped; these cover the
network log, downloads, failed requests and cookies it leaves behind. The session lock is
held while the popup runs, as an action in flight holds it, so the window is the same on
every run.

Same harness as test_allowed_hosts_navigation: a real BrowserManager and real Chromium
against a local server, where `localhost` is allowed and `127.0.0.1` is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.browser_manager import BrowserManager
from app.tool_gateway import McpToolGateway
from app.tool_inputs import GetCookiesInput
from tests import test_allowed_hosts_navigation as nav
from tests._chromium import requires_chromium
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium is the module's Chromium fixture
    SECRET,
    _gateway,
    _shared_chromium,
    audit_spy,
    browser_env,
    open_popup,
    wait_for,
)
from tests.test_allowed_hosts_popups import settle
from tests.test_browser_manager_create_session import _settings

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

WITHHELD = "page off ALLOWED_HOSTS"
CSV = b"a,b\n1,2\n"


class _AttachmentHandler(nav._Handler):
    """The harness's server, plus /file.csv served as an attachment on either host."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if not self.path.startswith("/file.csv"):
            super().do_GET()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Disposition", "attachment; filename=file.csv")
        self.send_header("Content-Length", str(len(CSV)))
        self.end_headers()
        self.wfile.write(CSV)


# Logged by the popup once its script has run; the controller drops it, the context still reports it.
SCRIPT_DONE = "REFUSED-POPUP-SCRIPT-DONE"
SCRIPT_TIMEOUT_SECONDS = 10.0


async def refused_popup(env: nav.Env, session_id: str, script: str, monkeypatch):
    """Open a popup onto the disallowed host, whose page runs `script`, under the session lock until it has.

    The script runs as the body of an async function, so one that awaits is waited for.
    """
    page_script = f"(async () => {{ {script} }})().finally(() => console.log('{SCRIPT_DONE}'))"
    monkeypatch.setattr(nav, "EVIL_PAGE", nav.EVIL_PAGE + f"<script>{page_script}</script>")
    session = env.manager.sessions[session_id]
    done = asyncio.Event()

    def seen(message) -> None:
        if message.text == SCRIPT_DONE:
            done.set()

    session.context.on("console", seen)
    try:
        async with session.lock:
            popup = await open_popup(env, session_id, f"{env.evil}/")
            await asyncio.wait_for(done.wait(), timeout=SCRIPT_TIMEOUT_SECONDS)
    finally:
        session.context.remove_listener("console", seen)
    await settle(lambda: popup.url == "about:blank")
    return popup


# ── Network log ──────────────────────────────────────────────────────────────


async def network_env_session(env: nav.Env) -> str:
    env.manager.settings.network_inspector_enabled = True
    env.manager.settings.network_inspector_capture_bodies = True
    return await env.create()


def assert_withheld(entry: dict, env: nav.Env) -> None:
    assert entry.get("withheld") == WITHHELD, entry
    assert entry["url"] == env.evil, entry
    assert (entry["request_body"], entry["response_body"]) == (None, None), entry
    assert not entry["request_headers"] and not entry["response_headers"], entry
    assert SECRET not in json.dumps(entry), entry


async def test_the_network_log_withholds_a_refused_popups_document(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id = await network_env_session(env)
        await refused_popup(env, session_id, "", monkeypatch)

        log = await env.manager.get_network_log(session_id, limit=100)

        refused = [e for e in log["entries"] if e["url"].startswith(env.evil)]
        assert refused, log["entries"]
        for entry in refused:
            assert_withheld(entry, env)
        assert refused[-1]["resource_type"] == "document" and refused[-1]["status"] == 200


async def test_the_network_log_withholds_a_refused_pages_own_requests_and_hooks_see_that(
    tmp_path: Path, monkeypatch
) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id = await network_env_session(env)
        hooked: list[dict] = []

        async def hook(entry: dict) -> None:
            hooked.append(dict(entry))

        env.manager.sessions[session_id].network_inspector.register_hook("*", hook)
        await refused_popup(
            env, session_id, "await (await fetch('/data?q=' + document.body.innerText)).text()", monkeypatch
        )
        await settle(lambda: any(e.get("resource_type") == "fetch" for e in hooked))

        log = await env.manager.get_network_log(session_id, limit=100)

        fetches = [e for e in log["entries"] if e["resource_type"] == "fetch"]
        assert len(fetches) == 1, log["entries"]
        assert_withheld(fetches[0], env)
        hooked_fetches = [e for e in hooked if e.get("resource_type") == "fetch"]
        assert len(hooked_fetches) == 1, hooked
        assert_withheld(hooked_fetches[0], env)


async def test_control_an_allowed_pages_network_entries_keep_their_bodies_and_cross_host_subresources(
    tmp_path: Path,
) -> None:
    """Iframes and subresources an allowed page loads from other hosts are not filtered, by design."""
    page = "<title>allowed</title><p>hello</p><script>fetch('{EVIL}/sub', {mode: 'no-cors'})</script>"
    async with browser_env(tmp_path, {"/page": page}) as env:
        env.manager.settings.network_inspector_enabled = True
        session_id = await env.create("/page")
        inspector = env.manager.sessions[session_id].network_inspector
        await wait_for(lambda: any(e["url"] == f"{env.evil}/sub" for e in inspector.entries()), timeout=5)
        await settle(lambda: True)

        entries = (await env.manager.get_network_log(session_id, limit=100))["entries"]

        document = next(e for e in entries if e["url"] == f"{env.allowed}/page")
        assert "hello" in (document["response_body"] or ""), document
        assert "withheld" not in document
        subresource = next(e for e in entries if e["url"] == f"{env.evil}/sub")
        assert "withheld" not in subresource and subresource["status"] == 200, subresource


async def test_the_network_log_withholds_a_refused_pages_post_body(tmp_path: Path, monkeypatch) -> None:
    """The GET-only cases above could not catch a request body that is kept."""
    async with browser_env(tmp_path, {}) as env:
        session_id = await network_env_session(env)
        posted = "await (await fetch('/data', {method: 'POST', body: document.body.innerText})).text()"
        await refused_popup(env, session_id, posted, monkeypatch)
        inspector = env.manager.sessions[session_id].network_inspector
        await settle(lambda: any(e["method"] == "POST" for e in inspector.entries()))

        (post,) = [
            e for e in (await env.manager.get_network_log(session_id, limit=100))["entries"] if e["method"] == "POST"
        ]

        assert_withheld(post, env)


# A service worker a refused page registers outlives the page's blank and fetches from its own origin.
SERVICE_WORKER = (
    "self.addEventListener('install', (e) => self.skipWaiting());"
    "self.addEventListener('activate', (e) => e.waitUntil(fetch('/collect?q=" + SECRET + "',"
    " {method: 'POST', body: '" + SECRET + "'})));"
).encode()


class _ServiceWorkerHandler(nav._Handler):
    """The harness's server, plus /sw.js and a POST endpoint on either host."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if not self.path.startswith("/sw.js"):
            super().do_GET()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript")
        self.send_header("Content-Length", str(len(SERVICE_WORKER)))
        self.end_headers()
        self.wfile.write(SERVICE_WORKER)

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(204)
        self.end_headers()


async def test_the_network_log_withholds_what_a_refused_pages_service_worker_asks_for(
    tmp_path: Path, monkeypatch
) -> None:
    """NET-SW-01: a service worker's request has no frame, so no page it came from; it was logged whole."""
    monkeypatch.setattr(nav, "_Handler", _ServiceWorkerHandler)
    async with browser_env(tmp_path, {}) as env:
        session_id = await network_env_session(env)
        session = env.manager.sessions[session_id]
        from_worker: list = []
        session.context.on(
            "request", lambda r: from_worker.append(r) if r.service_worker and r.method == "POST" else None
        )

        await refused_popup(env, session_id, "await navigator.serviceWorker.register('/sw.js')", monkeypatch)
        await wait_for(lambda: from_worker, timeout=10)
        await settle(lambda: any(e["method"] == "POST" for e in session.network_inspector.entries()))

        log = await env.manager.get_network_log(session_id, limit=100)

        (post,) = [e for e in log["entries"] if e["method"] == "POST"]
        assert_withheld(post, env)
        assert SECRET not in json.dumps(log["entries"]), log["entries"]


# ── Downloads ────────────────────────────────────────────────────────────────


def blocked_downloads(spy) -> list:
    return [call for call in spy.call_args_list if call.kwargs.get("event_type") == "download_blocked"]


def assert_blocked(record: dict, env: nav.Env, session) -> None:
    assert record["status"] == "blocked", record
    assert (record["path"], record["url"], record["filename"]) == (None, None, None), record
    assert record["source_url"] == env.evil, record
    assert record["failure"], record
    assert not list((session.artifact_dir / "downloads").glob("*")), "a refused download was saved"
    logged = (session.artifact_dir / "downloads.jsonl").read_text(encoding="utf-8")
    assert json.loads(logged.splitlines()[-1])["status"] == "blocked"


async def test_a_download_from_a_disallowed_url_is_refused_and_saves_nothing(tmp_path: Path, monkeypatch) -> None:
    """window.open onto an attachment: no page commits there, so only the download's URL shows where it came from."""
    monkeypatch.setattr(nav, "_Handler", _AttachmentHandler)
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)

        await session.page.evaluate("(url) => window.open(url)", f"{env.evil}/file.csv")
        await wait_for(lambda: session.downloads, timeout=5)
        await settle(lambda: True)

        assert_blocked(session.downloads[-1], env, session)
        assert len(blocked_downloads(spy)) == 1


async def test_a_download_an_allowed_host_redirects_to_another_host_is_judged_by_the_host_it_comes_from(
    tmp_path: Path, monkeypatch
) -> None:
    """RD-06, pinned: Playwright reports the URL a download ends at, after its redirects, so a file an
    allowed host redirects to (a CDN, a file host) is refused unless that host is listed too."""
    monkeypatch.setattr(nav, "_Handler", _AttachmentHandler)
    async with browser_env(tmp_path, {"/redir": nav.redirect("{EVIL}/file.csv")}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        reported: list[str] = []
        session.page.on("download", lambda download: reported.append(download.url))

        await session.page.evaluate(
            "(url) => { const a = document.createElement('a'); a.href = url; document.body.appendChild(a); a.click(); }",
            f"{env.allowed}/redir",
        )
        await wait_for(lambda: session.downloads, timeout=5)
        await settle(lambda: True)

        assert reported == [f"{env.evil}/file.csv"]
        assert_blocked(session.downloads[-1], env, session)


async def test_a_download_a_refused_page_generates_is_refused(tmp_path: Path, monkeypatch) -> None:
    """A blob: URL passes the URL check; the page that started it does not."""
    script = (
        "const a = document.createElement('a');"
        "a.href = URL.createObjectURL(new Blob([document.body.innerText]));"
        "a.download = 'leak.txt'; document.body.appendChild(a); a.click();"
    )
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)

        await refused_popup(env, session_id, script, monkeypatch)
        await wait_for(lambda: session.downloads, timeout=5)
        await settle(lambda: True)

        record = session.downloads[-1]
        assert_blocked(record, env, session)
        assert len(blocked_downloads(spy)) == 1
        with pytest.raises(ValueError, match="did not complete"):
            await env.manager.read_download_text(session_id, record["id"])


async def test_control_a_download_from_an_allowed_page_is_still_captured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(nav, "_Handler", _AttachmentHandler)
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]

        await session.page.evaluate(
            "(url) => { const a = document.createElement('a'); a.href = url; a.download = '';"
            " document.body.appendChild(a); a.click(); }",
            f"{env.allowed}/file.csv",
        )
        await wait_for(lambda: session.downloads, timeout=5)
        await settle(lambda: True)

        record = session.downloads[-1]
        assert record["status"] == "completed", record
        assert Path(record["path"]).read_bytes() == CSV
        assert (await env.manager.read_download_text(session_id, record["id"]))["text"] == CSV.decode()


# ── Failed requests ──────────────────────────────────────────────────────────


async def test_a_refused_pages_failed_requests_are_not_listed(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        failed: list = []
        session.context.on("requestfailed", lambda request: failed.append(request))

        await refused_popup(
            env, session_id, "await fetch('http://localhost:1/' + document.body.innerText).catch(() => {})", monkeypatch
        )
        # The failure has been delivered before the list is read, so its absence means something.
        await wait_for(lambda: any(SECRET in request.url for request in failed), timeout=10)
        await settle(lambda: True)
        observed = await env.manager.observe(session_id, preset="text")

        assert session.page.url == f"{env.allowed}/", "precondition: the active page is the allowed one"
        assert SECRET not in str(observed["request_failures"]), observed["request_failures"]


async def test_control_an_allowed_pages_failed_request_is_listed(tmp_path: Path) -> None:
    page = "<title>allowed</title><script>fetch('http://localhost:1/kept')</script>"
    async with browser_env(tmp_path, {"/page": page}) as env:
        session_id = await env.create("/page")
        session = env.manager.sessions[session_id]
        await wait_for(lambda: session.request_failures, timeout=5)

        observed = await env.manager.observe(session_id, preset="text")

        assert "localhost:1/kept" in str(observed["request_failures"]), observed["request_failures"]


# ── Cookies ──────────────────────────────────────────────────────────────────


async def test_get_cookies_does_not_return_a_cookie_a_refused_page_set(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        await session.page.evaluate("document.cookie = 'kept=1; path=/'")
        await refused_popup(
            env, session_id, "document.cookie = 'leak=' + encodeURIComponent('" + SECRET + "')", monkeypatch
        )
        assert any(c["name"] == "leak" for c in await session.context.cookies()), "precondition: the page set it"
        gateway = _gateway(env)

        everything = await gateway._get_cookies(GetCookiesInput(session_id=session_id))
        named = await gateway._get_cookies(GetCookiesInput(session_id=session_id, urls=[f"{env.evil}/"]))

        assert [c["name"] for c in everything["cookies"]] == ["kept"], everything
        assert named["cookies"] == [], named


async def test_control_get_cookies_filters_nothing_when_every_host_is_allowed(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {}, allowed_hosts="*") as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        await session.context.add_cookies([{"name": "any", "value": "1", "url": f"{env.evil}/"}])

        returned = await _gateway(env)._get_cookies(GetCookiesInput(session_id=session_id))

        assert [c["name"] for c in returned["cookies"]] == ["any"], returned


# Cookies as Chromium stores them: host-only ones from a URL, domain ones (".x") from a domain.
COOKIE_JAR = {
    "app": {"url": "https://app.example.com/"},
    "sub": {"url": "https://x.app.example.com/"},
    "apex": {"url": "https://example.com/"},
    "parent": {"domain": ".example.com", "path": "/"},
    "app1": {"domain": ".app1.example.com", "path": "/"},
    "sibling": {"url": "https://evil.example.com/"},
    "other": {"domain": ".other.example", "path": "/"},
    "local": {"url": "http://localhost:1/"},
    "v4": {"url": "http://127.0.0.1:1/"},
    "v6": {"url": "http://[::1]:1/"},
}


@contextlib.asynccontextmanager
async def cookie_gateway(tmp_path: Path, allowed_hosts: str):
    """A gateway whose session holds COOKIE_JAR in a real Chromium context, under `allowed_hosts`."""
    settings = _settings(tmp_path)
    settings.allowed_hosts = allowed_hosts
    manager = BrowserManager(settings)
    context = await nav._SHARED["browser"].new_context()
    try:
        await context.add_cookies([{"name": name, "value": "1", **where} for name, where in COOKIE_JAR.items()])
        manager.get_session = AsyncMock(return_value=SimpleNamespace(context=context))
        yield McpToolGateway(manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())
    finally:
        await context.close()


async def returned_cookies(gateway: McpToolGateway, urls: list[str] | None = None) -> list[str]:
    result = await gateway._get_cookies(GetCookiesInput(session_id="s1", urls=urls))
    return sorted(c["name"] for c in result["cookies"])


@pytest.mark.parametrize(
    ("allowed_hosts", "kept"),
    [
        ("app.example.com", ["app", "parent", "sub"]),
        (".example.com", []),
        ("*.example.com", ["apex", "app", "app1", "parent", "sibling", "sub"]),
        ("app?.example.com", ["app1", "parent"]),
        ("localhost", ["local"]),
        ("127.0.0.1", ["v4"]),
        ("::1", ["v6"]),
        ("[::1]", []),
        ("", sorted(COOKIE_JAR)),
        ("*", sorted(COOKIE_JAR)),
    ],
    ids=["plain", "leading-dot", "star-dot", "glob", "localhost", "ip", "ipv6", "bracketed-ipv6", "empty", "star"],
)
async def test_get_cookies_keeps_a_cookie_only_when_a_host_the_list_allows_would_receive_it(
    tmp_path: Path, allowed_hosts: str, kept: list[str]
) -> None:
    """Per entry form, the hosts kept are the ones _assert_url_allowed lets a page visit (a leading-dot
    or bracketed entry lets none), plus a domain cookie's parents of those hosts."""
    async with cookie_gateway(tmp_path, allowed_hosts) as gateway:
        assert await returned_cookies(gateway) == kept


async def test_control_a_parent_domain_cookie_an_allowed_host_receives_is_returned(tmp_path: Path) -> None:
    """Domain=example.com (single sign-on across subdomains) is sent to app.example.com, so it is the allowed
    site's own cookie, with or without the allowed URL named."""
    async with cookie_gateway(tmp_path, "app.example.com") as gateway:
        assert "parent" in await returned_cookies(gateway)
        assert await returned_cookies(gateway, ["https://app.example.com/"]) == ["app", "parent"]


async def test_get_cookies_answers_nothing_for_a_url_the_list_refuses(tmp_path: Path) -> None:
    """A refused URL is dropped from `urls` before the browser is asked, so naming one returns nothing for it,
    not even the parent-domain cookie the allowed host also receives, and naming only refused URLs is not
    read as naming none (which returns every cookie)."""
    async with cookie_gateway(tmp_path, "app.example.com") as gateway:
        assert await returned_cookies(gateway, ["https://evil.example.com/"]) == []
        assert await returned_cookies(gateway, ["https://app.example.com/", "https://evil.example.com/"]) == [
            "app",
            "parent",
        ]


# ── Windows a refused page opens (RD-01) ─────────────────────────────────────

# The refused page opens a blank window, which is on about:blank, same-origin with it and
# scriptable by it, and has it log, fetch, fail a request, title itself and download a file.
OPENS_A_WINDOW = (
    "const s = document.body.innerText;"
    "const w = window.open();"
    "w.document.write('<title>' + s + '</title><p>' + s + '</p>');"
    "w.console.log(s);"
    "w.fetch('/x?q=' + s);"
    "w.fetch('http://localhost:1/fail?q=' + s).catch(() => {});"
    "const a = w.document.createElement('a');"
    "a.href = w.URL.createObjectURL(new w.Blob([s]));"
    "a.download = 'x.txt'; w.document.body.append(a); a.click();"
    "const d = w.document.createElement('a');"
    "d.href = 'data:text/plain,' + s; d.download = 'd.txt'; w.document.body.append(d); d.click();"
)


async def test_a_window_a_refused_page_opens_is_refused_with_it(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id = await network_env_session(env)
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)
        # What the window did, seen by the browser context, so the absence checks below wait until it happened.
        seen: dict[str, list[str]] = {"console": [], "requestfailed": [], "download": []}
        session.context.on("console", lambda message: seen["console"].append(message.text))
        session.context.on("requestfailed", lambda request: seen["requestfailed"].append(request.url))
        session.context.on("page", lambda page: page.on("download", lambda d: seen["download"].append(d.url)))

        popup = await refused_popup(env, session_id, OPENS_A_WINDOW, monkeypatch)
        (window,) = [page for page in session.context.pages if page not in (session.page, popup)]

        def window_done() -> bool:
            return (
                len(nav.blocked_events(spy)) >= 2
                and any(SECRET in text for text in seen["console"])
                and any(url.startswith("http://localhost:1/") for url in seen["requestfailed"])
                and len(seen["download"]) >= 2
            )

        assert await settle(window_done, timeout=10.0), seen

        log = await env.manager.get_network_log(session_id, limit=100)
        tabs = await env.manager.list_tabs(session_id)
        observed = await env.manager.observe(session_id, preset="text")
        with pytest.raises(PermissionError):
            await env.manager.activate_tab(session_id, session.context.pages.index(window))

        assert SECRET not in json.dumps(log["entries"]), log["entries"]
        assert SECRET not in str(session.console_messages) + str(session.page_errors)
        assert SECRET not in str(session.request_failures), session.request_failures
        assert all(record["status"] == "blocked" for record in session.downloads), session.downloads
        assert not list((session.artifact_dir / "downloads").glob("*")), "the window's download was saved"
        assert SECRET not in str(tabs), tabs
        assert SECRET not in str(observed)
        assert len(nav.blocked_events(spy)) == 2, "the popup and the window it opened are both sent to about:blank"
        assert SECRET not in await window.content()


# ── When the taint clears (Net F3) ───────────────────────────────────────────


class _BounceHandler(nav._Handler):
    """The harness's server, plus /bounce on the disallowed host: a 302 to an allowed URL carrying its text."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host != nav.EVIL_HOST or not self.path.startswith("/bounce"):
            super().do_GET()
            return
        env = self.server.env  # type: ignore[attr-defined]
        self.send_response(302)
        self.send_header("Location", f"{env.allowed}/landing?d={SECRET}")
        self.send_header("Content-Length", "0")
        self.end_headers()


async def test_a_refused_page_that_navigates_itself_to_an_allowed_url_stays_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """It chose that URL (the fragment is not even sent to the server); clearing its taint showed the URL whole."""
    script = "location.href = '{ALLOWED}/landing#' + encodeURIComponent(document.body.innerText);"
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        popup = await refused_popup(env, session_id, script, monkeypatch)
        await settle(lambda: popup.url == "about:blank")

        tabs = await env.manager.list_tabs(session_id)

        assert SECRET not in json.dumps(tabs), tabs
        assert env.manager._page_refused(popup)


async def test_a_page_a_refused_host_redirects_to_an_allowed_url_is_refused_until_the_agent_navigates(
    tmp_path: Path, monkeypatch
) -> None:
    """A refused host's 302 to an allowed URL that carries its content: the page commits an allowed URL."""
    monkeypatch.setattr(nav, "_Handler", _BounceHandler)
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        async with session.lock:  # as an action in flight holds it
            await session.page.evaluate("(url) => { location.href = url }", f"{env.evil}/bounce")
            await session.page.wait_for_url("**/landing**")
        await settle(lambda: session.page.url == "about:blank")

        tabs = await env.manager.list_tabs(session_id)
        assert SECRET not in json.dumps(tabs), tabs
        with pytest.raises(PermissionError):
            await env.manager.observe(session_id)

        # Control: the agent's own navigation to an allowed URL clears it.
        await env.manager.navigate(session_id, f"{env.allowed}/")
        observed = await env.manager.observe(session_id)
        assert observed["url"] == f"{env.allowed}/"


async def test_a_popup_whose_first_navigation_a_refused_host_redirected_is_refused(tmp_path: Path, monkeypatch) -> None:
    """An allowed page opens a window onto a refused URL that 302s to an allowed URL carrying its text. The
    popup's own listeners attach only when the context reports it, after that first navigation (A2 item 6)."""
    monkeypatch.setattr(nav, "_Handler", _BounceHandler)
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        async with session.lock:  # as an action in flight holds it
            popup = await open_popup(env, session_id, f"{env.evil}/bounce")
            assert "/landing" in popup.url
            assert env.manager._page_refused(popup)
        await settle(lambda: popup.url == "about:blank")

        tabs = await env.manager.list_tabs(session_id)
        assert SECRET not in json.dumps(tabs), tabs


# ── A refused page that blanks another tab and writes into it (A2 item 7) ────

# The refused popup navigates its opener to about:blank, which then has the popup's origin, and once
# it can reach that document it writes, titles and logs what it read there.
WRITES_ITS_OPENER = (
    "const s = document.body.innerText;"
    "const w = window.opener;"
    "w.location.href = 'about:blank';"
    "for (let i = 0; i < 100; i++) {"
    "  await new Promise((r) => setTimeout(r, 50));"
    "  try { if (w.location.href === 'about:blank' && w.document.body !== undefined) break; } catch (e) {}"
    "}"
    "w.document.write('<title>' + s + '</title><p>' + s + '</p>');"
    "w.console.log(s);"
)


async def test_a_tab_a_refused_page_blanks_and_writes_into_is_refused(tmp_path: Path, monkeypatch) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        opener = session.page
        # The opener's console.log(SECRET), seen by a spy on the whole context: until it is, the absence
        # below proves nothing (F8).
        logged = asyncio.Event()
        session.context.on("console", lambda message: logged.set() if SECRET in message.text else None)

        await refused_popup(env, session_id, WRITES_ITS_OPENER, monkeypatch)
        await asyncio.wait_for(logged.wait(), timeout=SCRIPT_TIMEOUT_SECONDS)
        await settle(lambda: opener.url == "about:blank")

        tabs = await env.manager.list_tabs(session_id)
        assert SECRET not in str(session.console_messages), session.console_messages
        assert SECRET not in json.dumps(tabs), tabs
        with pytest.raises(PermissionError):
            await env.manager.observe(session_id, preset="text")


async def test_control_an_allowed_page_that_blanks_itself_is_still_read(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        async with session.lock:
            await session.page.evaluate("() => { location.href = 'about:blank' }")
            await session.page.wait_for_url("about:blank")
        await settle(lambda: True)

        observed = await env.manager.observe(session_id, preset="text")

        assert observed["url"] == "about:blank"


async def test_control_a_blank_popup_an_allowed_page_opens_while_another_tab_is_refused_is_read_once_navigated(
    tmp_path: Path, monkeypatch
) -> None:
    """Its first commit is judged by its allowed opener, not by the refused tab (A2 item 7)."""
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        await refused_popup(env, session_id, "", monkeypatch)
        popup = await open_popup(env, session_id, "")
        await settle(lambda: True)

        await env.manager.activate_tab(session_id, session.context.pages.index(popup))
        await env.manager.navigate(session_id, f"{env.allowed}/")
        observed = await env.manager.observe(session_id, preset="text")

        assert observed["url"] == f"{env.allowed}/"


# ── A refused page that navigates another tab to an allowed URL (round 4c, item 3) ──

# The refused popup sends its opener, which opened it without noopener, to an allowed URL whose query it chose.
CHOSEN_MARKER = "REFUSED-POPUP-CHOSE-THIS"
NAVIGATES_ITS_OPENER = f"window.opener.location.href = '{{ALLOWED}}/search?q={CHOSEN_MARKER}';"


async def test_a_tab_a_refused_page_navigates_to_an_allowed_url_is_refused(tmp_path: Path, monkeypatch) -> None:
    """The Location setter is open across origins (reverse tabnabbing): the opener committed the URL the
    refused page chose, untainted, and observe, the tab list and the session summary showed it whole."""
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        opener = session.page
        committed: list[str] = []
        opener.on("framenavigated", lambda frame: committed.append(frame.url) if frame == opener.main_frame else None)

        await refused_popup(env, session_id, NAVIGATES_ITS_OPENER, monkeypatch)
        assert await settle(lambda: any(CHOSEN_MARKER in url for url in committed), timeout=10.0), committed

        tabs = await env.manager.list_tabs(session_id)
        summary = await env.manager.get_session_summary(session_id)
        assert CHOSEN_MARKER not in json.dumps(tabs), tabs
        assert CHOSEN_MARKER not in json.dumps(summary, default=str), summary
        with pytest.raises(PermissionError, match="another tab of this session"):
            await env.manager.observe(session_id, preset="text")


class _SlowSearchHandler(nav._Handler):
    """The harness's server, with /search answered after a pause: the refused popup has closed by its commit."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if self.path.startswith("/search"):
            time.sleep(SLOW_SEARCH_SECONDS)
        super().do_GET()


# Long enough for the popup's close to be delivered first, well inside the grace after it.
SLOW_SEARCH_SECONDS = 0.5


async def test_a_tab_a_refused_page_navigates_and_then_closes_on_is_refused(tmp_path: Path, monkeypatch) -> None:
    """PROV-1: the refused popup set window.opener.location to a URL it chose and called window.close(). Its
    close was delivered before the opener's commit, so the session had no refused tab by then, and the opener
    was read on the URL the popup chose."""
    monkeypatch.setattr(nav, "_Handler", _SlowSearchHandler)
    script = f"(() => {{ {NAVIGATES_ITS_OPENER} window.close(); }})()"
    monkeypatch.setattr(nav, "EVIL_PAGE", nav.EVIL_PAGE + f"<script>{script}</script>")
    async with browser_env(tmp_path, {}) as env:
        session_id = await env.create()
        session = env.manager.sessions[session_id]
        opener = session.page
        committed: list[str] = []
        opener.on("framenavigated", lambda frame: committed.append(frame.url) if frame == opener.main_frame else None)
        closed = asyncio.Event()
        session.context.on("page", lambda page: page.on("close", lambda _page: closed.set()))

        await opener.evaluate("(url) => window.open(url)", f"{env.evil}/")
        await asyncio.wait_for(closed.wait(), timeout=SCRIPT_TIMEOUT_SECONDS)
        assert await settle(lambda: any(CHOSEN_MARKER in url for url in committed), timeout=10.0), committed

        tabs = await env.manager.list_tabs(session_id)
        assert CHOSEN_MARKER not in json.dumps(tabs), tabs
        with pytest.raises(PermissionError, match="another tab of this session"):
            await env.manager.observe(session_id, preset="text")


# ── Navigations a refused page chooses, in the network log (A2 item 8) ───────


class _NoContentHandler(nav._Handler):
    """The harness's server, plus /no-content on either host: a 204, so a window opened onto it commits nothing."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if not self.path.startswith("/no-content"):
            super().do_GET()
            return
        self.send_response(204)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        """Records what was posted, so a test knows the post was sent before it looks for it in the log."""
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        POSTED.append(self.path + " " + body.decode("utf-8", "replace"))
        self.send_response(204)
        self.end_headers()


POSTED: list[str] = []
# The refused page writes a script into the window's blank document (its own origin, since the 204 left the
# window on the blank it opened with), which posts this marker to the allowed host once the page is blanked.
PLANTED = "PLANTED-IN-THE-WINDOW"


async def test_a_window_a_refused_page_opens_onto_an_allowed_204_is_logged_by_its_origin(
    tmp_path: Path, monkeypatch
) -> None:
    """The window's first navigation carries what the refused page read in its query, and its frame is not
    available yet when it is logged; it commits nothing, so no page of its own is ever judged."""
    monkeypatch.setattr(nav, "_Handler", _NoContentHandler)
    POSTED.clear()
    script = (
        "const w = window.open('{ALLOWED}/no-content?q=' + encodeURIComponent(document.body.innerText));"
        "await new Promise((r) => setTimeout(r, 1000));"
        "const post = 'fetch(\"{ALLOWED}/collect?m=" + PLANTED + '", {method: "POST", body: \''
        " + JSON.stringify('" + PLANTED + " ' + document.body.innerText) + '})';"
        "w.document.write('<script>setTimeout(() => ' + post + ', 1000)</scr' + 'ipt>');"
        "w.document.close();"
    )
    async with browser_env(tmp_path, {}) as env:
        session_id = await network_env_session(env)
        inspector = env.manager.sessions[session_id].network_inspector

        def window_entries() -> list[dict]:
            return [
                e
                for e in inspector.entries(limit=500)
                if "/no-content" in e["url"] or (e.get("withheld") and e["resource_type"] == "document")
            ]

        await refused_popup(env, session_id, script, monkeypatch)
        await settle(lambda: any(e["url"].startswith(env.allowed) for e in window_entries()))

        entries = window_entries()
        assert SECRET not in json.dumps(inspector.entries(limit=500)), entries
        assert any(e.get("withheld") == WITHHELD and e["url"] == env.allowed for e in entries), entries

        # The script it planted posts from a page the context never reported: no frame, no worker.
        assert await settle(lambda: any(PLANTED in posted for posted in POSTED), timeout=10.0), POSTED

        # Round 4d (T6): the POST's own entry first, withheld, so the marker's absence below is not
        # just a request the inspector had not been told about yet (or never was).
        def planted_post_withheld() -> bool:
            return any(
                e.get("withheld") == WITHHELD and e["method"] == "POST" and e["url"] == env.allowed
                for e in inspector.entries(limit=500)
            )

        assert await settle(planted_post_withheld, timeout=10.0), inspector.entries(limit=500)
        logged = json.dumps(inspector.entries(limit=500))
        assert PLANTED not in logged and SECRET not in logged, inspector.entries(limit=500)

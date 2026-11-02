"""The reads that ALLOWED_HOSTS holds to the page are checked again once they have read it.

Each read checked the page's URL first, then read: a page that moved onto a refused
host in between handed over its title, its pixels or its text. The CDP routes did not
check at all, and the network inspector kept everything a refused page asked for. These
use fakes, so the page moves exactly between the check and the read on every run.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.async_api import Error as PlaywrightError

from app.browser.page_provenance import REFUSED_FAILURES_KEPT, PageProvenance
from app.browser_manager import BrowserManager
from app.cdp.passthrough import CDPPassthrough
from app.network_inspector import NetworkInspector
from app.routes._utils import OFF_LIST_PAGE_DETAIL
from app.routes.extensions import cdp as cdp_routes
from app.routes.extensions.cdp import cdp_router
from app.tool_gateway import McpToolGateway
from app.tool_inputs import GetCookiesInput
from tests._background import pending
from tests.test_browser_manager_create_session import _settings
from tests.test_network_inspector import FakeRequest, FakeResponse

ALLOWED = "https://allowed.example/page"
REFUSED = "https://refused.example/page"
WITHHELD = "page off ALLOWED_HOSTS"


def manager_for(tmp_path: Path) -> BrowserManager:
    settings = _settings(tmp_path)
    settings.allowed_hosts = "allowed.example"
    return BrowserManager(settings)


class MovingPage:
    """A page whose URL becomes `then` as soon as its title is read."""

    def __init__(self, url: str, then: str, title: str = "REFUSED-TITLE") -> None:
        self.url = url
        self._then = then
        self._title = title

    async def title(self) -> str:
        self.url = self._then
        return self._title

    def is_closed(self) -> bool:
        return False


def tab_session(page: MovingPage) -> SimpleNamespace:
    return SimpleNamespace(id="s1", page=page, context=SimpleNamespace(pages=[page]), attached_pages=set())


# ── Network inspector: which requests are refused ───────────────────────────


class FakeFrame:
    def __init__(self, page_url: str, *, main: bool = True) -> None:
        self.page = SimpleNamespace(url=page_url)
        self.parent_frame = None if main else object()


class FramedRequest(FakeRequest):
    def __init__(self, *, frame: FakeFrame | None, navigation: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self._frame = frame
        self._navigation = navigation

    @property
    def frame(self):
        if self._frame is None:
            raise PlaywrightError("Frame for this navigation request is not available")
        return self._frame

    def is_navigation_request(self) -> bool:
        return self._navigation


def goto_response(*urls: str) -> SimpleNamespace:
    """What page.goto() returns: a response whose request leads back through redirected_from to urls[0]."""
    request = None
    for url in urls:
        request = SimpleNamespace(url=url, redirected_from=request)
    return SimpleNamespace(url=urls[-1], request=request)


def inspector() -> NetworkInspector:
    return NetworkInspector("s1", url_refused=lambda url: urlsplit(url).hostname == "refused.example")


async def drain(timeout: float = 5.0) -> None:
    """Wait for the background tasks this test started; fail rather than hang if one never finishes."""
    deadline = time.monotonic() + timeout
    while pending():
        if time.monotonic() > deadline:
            raise AssertionError(f"{len(pending())} background tasks still pending after {timeout}s")
        await asyncio.sleep(0.01)


async def record(network: NetworkInspector, request: FramedRequest, body: bytes = b"REFUSED-BODY") -> dict:
    network._on_request(request)
    await drain()
    network._on_response(FakeResponse(request, headers={"content-type": "text/html"}, body=body))
    await drain()
    network._on_request_finished(request)
    await drain()
    (entry,) = network.entries()
    return entry


async def test_a_popups_first_navigation_is_withheld_though_its_frame_is_not_available_yet() -> None:
    request = FramedRequest(
        frame=None, navigation=True, resource_type="document", url="https://refused.example:8443/p?q=secret"
    )

    entry = await record(inspector(), request)

    assert entry["withheld"] == WITHHELD
    assert entry["url"] == "https://refused.example:8443"
    assert (entry["response_body"], entry["response_headers"], entry["request_headers"]) == (None, None, {})
    assert entry["status"] == 200 and entry["resource_type"] == "document"


async def test_a_request_from_a_frame_of_a_refused_page_is_withheld() -> None:
    request = FramedRequest(frame=FakeFrame(REFUSED, main=False), url="https://cdn.example/lib.js")

    entry = await record(inspector(), request)

    assert entry["withheld"] == WITHHELD and entry["url"] == "https://cdn.example"


async def test_control_an_allowed_pages_cross_host_iframe_is_kept_whole() -> None:
    request = FramedRequest(
        frame=FakeFrame(ALLOWED, main=False), navigation=True, resource_type="document", url=REFUSED
    )

    entry = await record(inspector(), request, body=b"<p>framed</p>")

    assert "withheld" not in entry
    assert entry["url"] == REFUSED and entry["response_body"] == "<p>framed</p>"


class FramelessRequest(FramedRequest):
    """A request with no frame (Playwright raises for its frame): a service worker's, when `worker_url` is set."""

    def __init__(self, *, worker_url: str | None, **kwargs) -> None:
        super().__init__(frame=None, **kwargs)
        self.service_worker = None if worker_url is None else SimpleNamespace(url=worker_url)


@pytest.mark.parametrize(
    ("worker_url", "url", "withheld"),
    [
        ("https://refused.example/sw.js", "https://allowed.example/collect", True),
        # An allowed page's worker fetching from another host: kept, as the page's own subresources are.
        ("https://allowed.example/sw.js", "https://refused.example/api", False),
        (None, "https://refused.example/api", True),
        # A page the context has not reported yet (round 4c, item 4): a refused page's popup that never
        # commits (a 204) stays unreported, and the refused page can script its blank document.
        (None, "https://allowed.example/api", True),
    ],
    ids=["refused-worker", "allowed-worker", "no-worker-refused-url", "no-worker-allowed-url"],
)
async def test_a_request_with_no_frame_is_judged_by_its_service_worker_or_else_withheld(
    worker_url: str | None, url: str, withheld: bool
) -> None:
    """NET-SW-01: such a request had no page to be judged by, so it was never withheld. Without a worker
    it comes from a page the context has not reported, which cannot be judged, so it is withheld."""
    request = FramelessRequest(worker_url=worker_url, method="POST", resource_type="fetch", url=url)

    entry = await record(inspector(), request)

    assert ("withheld" in entry) is withheld, entry


async def test_a_request_the_inspector_cannot_attribute_is_withheld() -> None:
    class Unattributable(FramedRequest):
        def is_navigation_request(self) -> bool:
            raise RuntimeError("Target page, context or browser has been closed")

    entry = await record(inspector(), Unattributable(frame=FakeFrame(ALLOWED), url=ALLOWED))

    assert entry["withheld"] == WITHHELD


@pytest.mark.parametrize("method, logged", [("POST", "POST"), ("options", "OPTIONS"), ("IGNORE-THE-RULES", "OTHER")])
async def test_a_withheld_entry_records_only_a_standard_method(method: str, logged: str) -> None:
    """The page chooses the method token, so a withheld entry keeps it only when it is a standard one."""
    request = FramedRequest(frame=FakeFrame(REFUSED), url=REFUSED, method=method, post_data="REFUSED-BODY")

    entry = await record(inspector(), request)

    assert (entry["withheld"], entry["method"], entry["request_body"]) == (WITHHELD, logged, None)


async def test_hooks_see_an_entry_that_stopped_being_tracked_because_too_many_requests_were_in_flight() -> None:
    network = NetworkInspector("s1", max_entries=1)
    seen: list[str] = []

    async def hook(entry: dict) -> None:
        seen.append(entry["url"])

    network.register_hook("*", hook)
    network._on_request(FakeRequest(url="https://allowed.example/first"))
    await drain()
    network._on_request(FakeRequest(url="https://allowed.example/second"))
    await drain()

    assert seen == ["https://allowed.example/first"]


# ── Failed requests: judged when they start ──────────────────────────────────


class FiringPage:
    """A page whose listeners the test fires by hand."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.main_frame = SimpleNamespace(parent_frame=None, url=url)
        self.handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event: str, argument: object) -> None:
        for handler in self.handlers.get(event, []):
            handler(argument)

    def commit(self, manager: BrowserManager, url: str) -> None:
        self.url = self.main_frame.url = url
        manager.page_provenance.committed(self, url, url_refused=manager._url_off_list(url))


@pytest.mark.parametrize(
    ("started_on", "failed_on", "listed"),
    [(REFUSED, ALLOWED, False), (ALLOWED, REFUSED, False), (ALLOWED, ALLOWED, True)],
    ids=["started-refused-failed-allowed", "started-allowed-failed-refused", "control-allowed"],
)
async def test_a_failed_request_is_judged_by_the_page_it_started_on(
    tmp_path: Path, started_on: str, failed_on: str, listed: bool
) -> None:
    """Judged when the failure was delivered, a refused document's request that failed after the
    page had moved on to an allowed URL was listed, path included."""
    manager = manager_for(tmp_path)
    page = FiringPage(started_on)
    session = SimpleNamespace(id="s1", attached_pages=weakref.WeakSet(), request_failures=[], lock=asyncio.Lock())
    manager.diagnostics.attach_page_listeners(page, session)
    request = FakeRequest(url="https://refused.example/x?q=REFUSED-TEXT")

    page.fire("request", request)
    page.commit(manager, failed_on)
    page.fire("requestfailed", request)
    await drain()

    assert bool(session.request_failures) is listed, session.request_failures


# ── Provenance: a popup is judged by its opener ─────────────────────────────


class EventPage:
    """A page that keeps its handlers, so a test delivers Playwright's events in Playwright's order."""

    def __init__(self, url: str, opener: "EventPage | None" = None) -> None:
        self.url = url
        self.main_frame = SimpleNamespace(parent_frame=None, url=url)
        self.closed = False
        self._opener = opener
        self.handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event: str, arg) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(arg)

    def commit(self, manager: BrowserManager, url: str) -> None:
        self.url = self.main_frame.url = url
        self.fire("framenavigated", self.main_frame)

    async def goto(self, url: str, **_kwargs) -> None:
        """The page guard's blank (diagnostics._return_to_blank) commits as Playwright's does."""
        self.commit(None, url)

    def close(self) -> None:
        self.closed = True
        self.fire("close", self)

    def is_closed(self) -> bool:
        return self.closed

    async def title(self) -> str:
        return "page"

    async def opener(self):
        # Playwright's Page.opener(): None once the opener has closed.
        return None if self._opener is None or self._opener.is_closed() else self._opener


def popup_session(*pages: EventPage) -> SimpleNamespace:
    return SimpleNamespace(
        id="s1",
        page=pages[0] if pages else None,
        context=SimpleNamespace(pages=list(pages)),
        attached_pages=weakref.WeakSet(),
        console_messages=[],
        page_errors=[],
        request_failures=[],
        lock=asyncio.Lock(),
        unframed_hops=[],
        refused_failures=deque(maxlen=REFUSED_FAILURES_KEPT),
        # A session the pre-request gate does not own (BrowserSession's defaults: no gate in these fakes).
        browser=None,
        gate_context_id=None,
    )


def report(manager: BrowserManager, session: SimpleNamespace, popup: EventPage) -> None:
    """What Playwright's BrowserContext._on_page does: the context's "page" event, then the opener's
    "popup" event, synchronously, and only while the opener is open (_browser_context.py:240-244)."""
    session.context.pages.append(popup)
    manager._attach_popup(popup, session)
    if popup._opener is not None and not popup._opener.is_closed():
        popup._opener.fire("popup", popup)


def opener_on(
    manager: BrowserManager, session: SimpleNamespace, url: str, *, refused: bool, blanked: bool = False
) -> EventPage:
    opener = EventPage(ALLOWED)
    session.page = opener
    session.context.pages.append(opener)
    manager._attach_page_listeners(opener, session)
    if refused:
        opener.commit(manager, url)
    if blanked:
        # The page guard's blank: the opener is on about:blank, still tainted.
        manager.page_provenance.blank_started(opener)
        opener.commit(manager, "about:blank")
    return opener


@pytest.mark.parametrize(
    ("popup_url", "opener_refused", "blanked", "refused"),
    [
        ("about:blank", False, False, False),
        ("about:blank", True, False, True),
        ("about:blank", True, True, True),
        # On an allowed host too: the refused opener chose its path and query, which can carry what it read.
        (ALLOWED, True, False, True),
    ],
    ids=["allowed-opener", "refused-opener", "tainted-blank-opener", "allowed-url-refused-opener"],
)
async def test_a_popup_is_judged_by_its_opener_as_it_opens(
    tmp_path: Path, popup_url: str, opener_refused: bool, blanked: bool, refused: bool
) -> None:
    manager = manager_for(tmp_path)
    session = popup_session()
    opener = opener_on(manager, session, REFUSED, refused=opener_refused, blanked=blanked)
    popup = EventPage(popup_url, opener)

    report(manager, session, popup)
    await drain()

    assert manager._page_refused(popup) is refused


async def test_a_popup_keeps_the_verdict_its_opener_had_when_it_opened(tmp_path: Path) -> None:
    """Codex F1, BP-2: judged when a lookup resolved, a refused opener that cleared its taint first (or
    whose allowed commit arrived before the popup was reported) left its blank popup untainted. The opener
    is cleared as the agent clears it, by a navigation: since round 4 part 2 its own allowed commit leaves
    it refused, so a verdict taken late saw it refused too and the test could not tell (round 4d mutant
    4a-a1-verdict_taken_later)."""
    manager = manager_for(tmp_path)
    session = popup_session()
    opener = opener_on(manager, session, REFUSED, refused=True)
    popup = EventPage("about:blank", opener)

    report(manager, session, popup)
    with manager.page_provenance.controller_navigation(opener) as navigation:
        opener.commit(manager, ALLOWED)
        navigation.response = goto_response(ALLOWED)
    assert not manager._page_refused(opener)
    await drain()

    assert manager._page_refused(popup)


async def test_a_popup_whose_opener_closed_before_it_was_reported_stays_refused(tmp_path: Path) -> None:
    """Codex F1: a refused page writes a blank window and closes. Playwright then reports no popup
    event and Page.opener() is None, which read as no opener, so the window was not refused."""
    manager = manager_for(tmp_path)
    session = popup_session()
    opener = opener_on(manager, session, REFUSED, refused=True)
    popup = EventPage("about:blank", opener)
    session.page = popup
    opener.close()

    report(manager, session, popup)
    await drain()

    assert manager._page_refused(popup)


async def test_a_page_with_no_verdict_is_read_again_once_the_agent_navigates_it(tmp_path: Path) -> None:
    """Refused for good otherwise: no verdict comes, and its own allowed commits do not clear it."""
    manager = manager_for(tmp_path)
    session = popup_session()
    opener = opener_on(manager, session, REFUSED, refused=True)
    popup = EventPage("about:blank", opener)
    session.page = popup
    opener.close()
    report(manager, session, popup)

    popup.commit(manager, ALLOWED)  # an allowed URL it loaded itself
    assert manager._page_refused(popup)
    with manager.page_provenance.controller_navigation(popup) as navigation:
        popup.commit(manager, ALLOWED)  # the agent's navigate
        navigation.response = goto_response(ALLOWED)
    await drain()

    assert not manager._page_refused(popup)


async def test_control_a_tab_the_controller_opens_is_not_refused(tmp_path: Path) -> None:
    """The controller's own tabs also come through the context's "page" event, with no opener."""
    manager = manager_for(tmp_path)
    session = popup_session(EventPage(ALLOWED))
    manager._persist_session = AsyncMock()
    manager._session_summary = AsyncMock(return_value={})

    async def new_page() -> EventPage:
        page = EventPage("about:blank")
        report(manager, session, page)  # Playwright reports the page before new_page() returns it
        return page

    session.context.new_page = new_page
    manager.get_session = AsyncMock(return_value=session)

    opened = await manager.tabs.open("s1", None, activate=False)
    await drain()

    assert not manager._page_refused(session.context.pages[opened["index"]])


def test_a_page_stays_tainted_on_the_controllers_blank_and_is_cleared_by_the_agents_navigation() -> None:
    provenance = PageProvenance(url_refused=lambda url: urlsplit(url).hostname == "refused.example")
    page = SimpleNamespace(url="about:blank")

    assert provenance.committed(page, REFUSED, url_refused=True), "a refused commit is sent back"
    provenance.blank_started(page)
    assert not provenance.committed(page, "about:blank", url_refused=False), "the controller's own blank"
    assert provenance.refused(page) and not provenance.needs_blank(page)
    # The refused opener navigates its window to about:blank again, to write into it.
    assert provenance.committed(page, "about:blank", url_refused=False), "a later blank of a tainted page"
    assert provenance.needs_blank(page)
    with provenance.controller_navigation(page) as navigation:
        page.url = ALLOWED
        provenance.committed(page, ALLOWED, url_refused=False)
        navigation.response = goto_response(ALLOWED)
    assert not provenance.refused(page)


def test_a_tainted_pages_own_allowed_commit_leaves_it_tainted() -> None:
    """Net F3: the refused page chose the URL (location = 'https://allowed/#' + what it read), so the
    allowed commit it made itself does not clear the taint: its URL is shown by origin, it is not read."""
    provenance = PageProvenance()
    page = SimpleNamespace(url="about:blank")
    provenance.committed(page, REFUSED, url_refused=True)

    assert provenance.committed(page, f"{ALLOWED}#REFUSED-CONTENT", url_refused=False), "sent back too"
    assert provenance.refused(page)


def test_a_commit_after_a_refused_hop_is_tainted_whatever_its_url() -> None:
    """Net F3: a refused host's 302 to an allowed URL carrying its content commits an allowed URL."""
    provenance = PageProvenance()
    page = SimpleNamespace(url=ALLOWED)

    provenance.refused_hop_started(page, SimpleNamespace(url=REFUSED, redirected_from=None))
    assert provenance.refused(page), "refused while the navigation runs through the refused host"
    assert provenance.committed(page, f"{ALLOWED}?d=REFUSED-CONTENT", url_refused=False)
    assert provenance.refused(page)


def test_control_a_refused_hop_whose_navigation_failed_leaves_no_mark() -> None:
    provenance = PageProvenance()
    page = SimpleNamespace(url=ALLOWED)
    hop = SimpleNamespace(url=REFUSED, redirected_from=None)

    provenance.refused_hop_started(page, hop)
    provenance.navigation_failed(page, hop)

    assert not provenance.refused(page)
    assert not provenance.committed(page, ALLOWED, url_refused=False)


async def test_requests_on_a_page_navigating_through_a_refused_hop_are_withheld(tmp_path: Path) -> None:
    """Net F7: what the browser fetches before the commit (early hints) is the refused host's choice."""
    manager = manager_for(tmp_path)
    page = FiringPage(ALLOWED)
    session = SimpleNamespace(
        id="s1",
        page=page,
        context=SimpleNamespace(pages=[page]),
        attached_pages=weakref.WeakSet(),
        request_failures=[],
        lock=asyncio.Lock(),
    )
    manager.diagnostics.attach_page_listeners(page, session)
    network = NetworkInspector("s1", url_refused=manager._url_off_list, page_refused=manager._page_refused)
    hop = FramedRequest(
        frame=SimpleNamespace(page=page, parent_frame=None), navigation=True, resource_type="document", url=REFUSED
    )
    page.fire("request", hop)
    preload = FramedRequest(
        frame=SimpleNamespace(page=page, parent_frame=None), resource_type="script", url="https://cdn.example/x?d=1"
    )

    entry = await record(network, preload)

    assert entry.get("withheld") == WITHHELD, entry
    page.fire("requestfailed", hop)
    assert not manager._page_refused(page), "a failed navigation leaves no mark"


class TaintMovingPage:
    """A page on about:blank whose provenance changes while its title is read.

    "taints": clean before the read, tainted during it (its opener judged refused, say).
    "clears": tainted before the read, and an allowed commit clears it during the read.
    "pends": refused during the read while its opener is looked up, with no commit counted.
    Either way the title read may be the document a refused page wrote.
    """

    def __init__(self, provenance: PageProvenance, change: str) -> None:
        self.url = "about:blank"
        self._provenance = provenance
        self._change = change
        if change == "clears":
            provenance.committed(self, REFUSED, url_refused=True)

    async def title(self) -> str:
        if self._change == "taints":
            self._provenance.committed(self, REFUSED, url_refused=True)
        elif self._change == "pends":
            self._provenance.opener_lookup_started(self)
        else:
            self.url = ALLOWED
            self._provenance.committed(self, ALLOWED, url_refused=False)
        return "WRITTEN-BY-A-REFUSED-PAGE"

    def is_closed(self) -> bool:
        return False


@pytest.mark.parametrize("change", ["taints", "clears", "pends"])
async def test_a_tab_tainted_before_or_after_its_title_is_read_has_no_title(tmp_path: Path, change: str) -> None:
    manager = manager_for(tmp_path)
    session = tab_session(TaintMovingPage(manager.page_provenance, change))

    (tab,) = await manager.tabs.summaries(session)

    assert tab["title"] == ""


@pytest.mark.parametrize("change", ["taints", "clears", "pends"])
async def test_a_session_summary_has_no_title_when_its_page_is_tainted_before_or_after_the_read(
    tmp_path: Path, change: str
) -> None:
    manager = manager_for(tmp_path)
    session = tab_session(TaintMovingPage(manager.page_provenance, change))

    _, title, live = await manager.session_lifecycle._page_snapshot(session)

    assert (title, live) == ("", True)


# ── Titles ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("start, then", [(ALLOWED, REFUSED), (REFUSED, ALLOWED)], ids=["moves_off", "moves_back"])
async def test_a_tab_whose_page_is_off_the_list_before_or_after_its_title_is_read_has_no_title(
    tmp_path: Path, start: str, then: str
) -> None:
    manager = manager_for(tmp_path)
    session = tab_session(MovingPage(start, then))

    (tab,) = await manager.tabs.summaries(session)

    assert tab["title"] == ""


async def test_control_a_tab_on_an_allowed_page_keeps_its_title(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)

    (tab,) = await manager.tabs.summaries(tab_session(MovingPage(ALLOWED, ALLOWED, title="kept")))

    assert tab["title"] == "kept"


async def test_a_session_summary_has_no_title_when_the_page_moves_off_the_list_while_it_is_read(
    tmp_path: Path,
) -> None:
    manager = manager_for(tmp_path)
    session = tab_session(MovingPage(ALLOWED, REFUSED))

    _, title, live = await manager.session_lifecycle._page_snapshot(session)

    assert (title, live) == ("", True)


# ── Screenshots and observations ─────────────────────────────────────────────


def moving_capture(session: SimpleNamespace, tmp_path: Path) -> AsyncMock:
    """A capture that writes its file while the page moves off the list."""

    async def capture(_session, label: str, **_kwargs) -> dict:
        path = tmp_path / f"{label}.png"
        path.write_bytes(b"REFUSED-PIXELS")
        session.page.url = REFUSED
        return {"path": str(path), "url": f"/artifacts/s1/{label}.png"}

    return AsyncMock(side_effect=capture)


async def test_a_screenshot_is_refused_and_deleted_when_the_page_moved_off_the_list_while_it_was_taken(
    tmp_path: Path,
) -> None:
    manager = manager_for(tmp_path)
    session = tab_session(MovingPage(ALLOWED, ALLOWED))
    session.artifact_dir = tmp_path  # as every BrowserSession has
    manager.artifacts.capture_screenshot = moving_capture(session, tmp_path)

    with pytest.raises(PermissionError, match="ALLOWED_HOSTS"):
        await manager.observation.capture_session_screenshot(session, "shot")

    assert not (tmp_path / "shot.png").exists()


def moving_summary(session: SimpleNamespace) -> AsyncMock:
    async def summary(_page, text_limit: int = 2000) -> dict:
        session.page.url = REFUSED
        return {
            "title": "REFUSED-TITLE",
            "active_element": None,
            "text_excerpt": "REFUSED-TEXT",
            "dom_outline": {},
            "accessibility_outline": {},
        }

    return AsyncMock(side_effect=summary)


@pytest.mark.parametrize("preset", ["text", "normal"])
async def test_an_observation_is_refused_when_the_page_moved_off_the_list_while_it_was_read(
    tmp_path: Path, preset: str
) -> None:
    manager = manager_for(tmp_path)
    session = tab_session(MovingPage(ALLOWED, ALLOWED))
    session.console_messages, session.page_errors, session.request_failures, session.downloads = [], [], [], []
    manager.element_ids.evaluate = AsyncMock(return_value=[])
    manager.observation.page_summary = moving_summary(session)
    shot = tmp_path / "observe.png"
    shot.write_bytes(b"PIXELS")
    manager._capture_screenshot = AsyncMock(return_value={"path": str(shot), "url": "/artifacts/s1/observe.png"})
    manager.observation._extract_ocr_if_needed = AsyncMock(return_value=None)
    manager._session_summary = AsyncMock(return_value={"id": "s1"})
    manager.tabs.summaries = AsyncMock(return_value=[])
    manager._current_takeover_url = lambda _session: None
    manager.remote_access = SimpleNamespace(session_info=lambda _session: {})

    with pytest.raises(PermissionError, match="ALLOWED_HOSTS"):
        await manager.observation.observation_payload(session, preset=preset)

    assert not shot.exists() or preset == "text"


async def test_an_action_snapshot_is_refused_when_the_page_moved_off_the_list_while_it_was_read(
    tmp_path: Path,
) -> None:
    manager = manager_for(tmp_path)
    manager.settings.pii_scrub_action_screenshots = False
    session = tab_session(MovingPage(ALLOWED, ALLOWED))
    manager.observation.page_summary = moving_summary(session)
    shot = tmp_path / "action.png"
    shot.write_bytes(b"PIXELS")
    manager._capture_screenshot = AsyncMock(return_value={"path": str(shot), "url": "/artifacts/s1/action.png"})

    with pytest.raises(PermissionError, match="ALLOWED_HOSTS"):
        await manager.observation.light_snapshot(session, label="action")

    assert not shot.exists()


# ── REST CDP routes ──────────────────────────────────────────────────────────


class RecordingLock:
    def __init__(self) -> None:
        self.held = False

    async def acquire(self) -> bool:
        self.held = True
        return True

    def release(self) -> None:
        self.held = False

    async def __aenter__(self) -> None:
        self.held = True

    async def __aexit__(self, *exc: object) -> None:
        self.held = False


class StuckLock(RecordingLock):
    """A session another call holds for HANG_SECONDS, longer than the route waits."""

    async def acquire(self) -> bool:
        await asyncio.sleep(HANG_SECONDS)
        return await super().acquire()


class LockCheckingCdp:
    def __init__(self, session: SimpleNamespace) -> None:
        self.session = session
        self.page = session.page
        self.calls: list[tuple[str, bool]] = []

    async def get_element_intelligence(self, selector: str) -> dict:
        self.calls.append(("element", self.session.lock.held))
        return {"selector": selector}

    async def raw_cdp_command(self, method: str, params: dict) -> dict:
        self.calls.append((method, self.session.lock.held))
        return {"method": method}


class HangingCdp(LockCheckingCdp):
    """A renderer that does not answer (a script in an endless loop): each call waits for HANG_SECONDS."""

    async def get_element_intelligence(self, selector: str) -> dict:
        await asyncio.sleep(HANG_SECONDS)
        return await super().get_element_intelligence(selector)

    async def raw_cdp_command(self, method: str, params: dict) -> dict:
        await asyncio.sleep(HANG_SECONDS)
        return await super().raw_cdp_command(method, params)


def cdp_client(
    tmp_path: Path, url: str, *, lock: RecordingLock | None = None, cdp_class: type = LockCheckingCdp
) -> tuple[TestClient, LockCheckingCdp]:
    manager = manager_for(tmp_path)
    session = SimpleNamespace(id="s1", page=SimpleNamespace(url=url), lock=lock or RecordingLock())
    manager.sessions["s1"] = session
    cdp = cdp_class(session)
    app = FastAPI()
    app.state.browser_manager = manager
    app.state.cdp_sessions = {"s1": cdp}
    app.include_router(cdp_router)
    return TestClient(app), cdp


def cdp_calls(client: TestClient):
    return [
        client.get("/sessions/s1/cdp/element?selector=p"),
        client.post("/sessions/s1/cdp/raw", json={"method": "DOM.getDocument", "params": {}}),
    ]


def test_the_cdp_routes_refuse_while_the_active_page_is_off_the_list(tmp_path: Path) -> None:
    client, cdp = cdp_client(tmp_path, REFUSED)

    responses = cdp_calls(client)

    assert [(r.status_code, r.json().get("detail")) for r in responses] == [(403, OFF_LIST_PAGE_DETAIL)] * 2
    assert cdp.calls == []


def test_control_the_cdp_routes_read_an_allowed_page_under_the_session_lock(tmp_path: Path) -> None:
    client, cdp = cdp_client(tmp_path, ALLOWED)

    responses = cdp_calls(client)

    assert [r.status_code for r in responses] == [200, 200]
    assert cdp.calls == [("element", True), ("DOM.getDocument", True)]


HANG_SECONDS = 3.0
QUICK_TIMEOUT = 0.2


def test_a_cdp_route_answers_409_when_another_call_keeps_the_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cdp_routes, "CDP_TIMEOUT_SECONDS", QUICK_TIMEOUT, raising=False)
    client, cdp = cdp_client(tmp_path, ALLOWED, lock=StuckLock())

    responses = cdp_calls(client)

    assert [r.status_code for r in responses] == [409, 409]
    assert cdp.calls == []


def test_a_cdp_call_that_never_answers_is_504_and_lets_go_of_the_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cdp_routes, "CDP_TIMEOUT_SECONDS", QUICK_TIMEOUT, raising=False)
    client, cdp = cdp_client(tmp_path, ALLOWED, cdp_class=HangingCdp)

    responses = cdp_calls(client)

    assert [r.status_code for r in responses] == [504, 504]
    assert cdp.session.lock.held is False


def test_a_cdp_route_answers_503_rather_than_read_the_old_tab_when_it_cannot_attach(
    tmp_path: Path, monkeypatch
) -> None:
    client, cdp = cdp_client(tmp_path, ALLOWED)
    cdp.page = SimpleNamespace(url=REFUSED)  # still attached to the tab the session left
    monkeypatch.setattr(CDPPassthrough, "from_page", AsyncMock(side_effect=PlaywrightError("could not attach")))

    responses = cdp_calls(client)

    assert [r.status_code for r in responses] == [503, 503]
    assert cdp.calls == []


# ── Cookies partitioned under a refused top-level site (Net F5) ──────────────


async def test_get_cookies_drops_a_cookie_partitioned_under_a_refused_top_level_site(tmp_path: Path) -> None:
    """A partitioned (CHIPS) cookie is only ever sent under its top-level site: one keyed to a refused
    site was set by a frame on the refused page, and is never sent to an allowed top-level page."""
    manager = manager_for(tmp_path)
    jar = [
        {"name": "under-refused", "domain": "allowed.example", "partitionKey": "https://refused.example"},
        {"name": "under-allowed", "domain": "allowed.example", "partitionKey": "https://allowed.example"},
        {"name": "unreadable-key", "domain": "allowed.example", "partitionKey": "not a site"},
        {"name": "unpartitioned", "domain": "allowed.example"},
        {"name": "no-key", "domain": "allowed.example", "partitionKey": None},
    ]
    context = SimpleNamespace(cookies=AsyncMock(return_value=jar))
    manager.get_session = AsyncMock(return_value=SimpleNamespace(context=context))
    gateway = McpToolGateway(manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())

    result = await gateway._get_cookies(GetCookiesInput(session_id="s1"))

    assert sorted(c["name"] for c in result["cookies"]) == ["no-key", "under-allowed", "unpartitioned"]


async def test_control_a_cookie_partitioned_under_the_site_of_a_listed_host_is_returned(tmp_path: Path) -> None:
    """The key is a site: a top-level page on a listed app.allowed.example is keyed https://allowed.example."""
    manager = manager_for(tmp_path)
    manager.settings.allowed_hosts = "app.allowed.example"
    jar = [{"name": "sso", "domain": ".allowed.example", "partitionKey": "https://allowed.example"}]
    context = SimpleNamespace(cookies=AsyncMock(return_value=jar))
    manager.get_session = AsyncMock(return_value=SimpleNamespace(context=context))
    gateway = McpToolGateway(manager=manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())

    result = await gateway._get_cookies(GetCookiesInput(session_id="s1"))

    assert [c["name"] for c in result["cookies"]] == ["sso"]


# ── A blob download is judged by the page that reports it (R3-RDT-05) ────────


@pytest.mark.parametrize(("tainted", "captured"), [(False, True), (True, False)], ids=["allowed-page", "tainted-page"])
async def test_a_blob_download_is_judged_by_the_page_that_reports_it(
    tmp_path: Path, tainted: bool, captured: bool
) -> None:
    """A widget iframe from another host builds a file on an allowed page: its blob URL names the
    widget's origin. Iframes on an allowed page are not filtered, so neither is the file it makes;
    a refused or tainted page's file is refused by the page's own verdict."""
    manager = manager_for(tmp_path)
    page = SimpleNamespace(url=ALLOWED)
    if tainted:
        manager.page_provenance.committed(page, REFUSED, url_refused=True)
    session = SimpleNamespace(id="s1", downloads=[], unframed_hops=[], refused_failures=deque())
    record = {"status": "completed", "filename": "report.csv", "url": "/artifacts/s1/report.csv", "failure": None}
    manager.diagnostics.download_capture = SimpleNamespace(
        capture=AsyncMock(return_value=record),
        block=AsyncMock(return_value={"source_url": "https://widget.vendor.example"}),
    )
    manager.audit.append = AsyncMock()
    download = SimpleNamespace(url="blob:https://widget.vendor.example/6f1c", suggested_filename="report.csv")

    manager.diagnostics._on_download(session, page, download)
    await drain()

    assert manager.diagnostics.download_capture.capture.await_count == int(captured)
    assert manager.diagnostics.download_capture.block.await_count == int(not captured)

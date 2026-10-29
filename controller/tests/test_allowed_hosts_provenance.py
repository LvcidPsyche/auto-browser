"""When a refused page's taint clears under ALLOWED_HOSTS, and what keeps it set (app/browser/page_provenance.py).

A refused page is read again only after a navigation the controller started ends on the list. These
use fakes that deliver Playwright's events (request, requestfailed, a commit) in the order each case
needs, so every run sees the same order.
"""

from __future__ import annotations

import asyncio
import weakref
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError
from app.actions.pipeline import BrowserActionPipeline
from app.browser_manager import BrowserManager, BrowserSession
from app.network_inspector import WITHHELD, NetworkInspector
from app.utils import UTC
from tests.test_allowed_hosts_rechecks import (
    ALLOWED,
    REFUSED,
    EventPage,
    FiringPage,
    FramedRequest,
    FramelessRequest,
    drain,
    goto_response,
    manager_for,
    opener_on,
    popup_session,
    record,
    report,
)
from tests.test_network_inspector import FakeRequest


class NavigationRequest(FramedRequest):
    """A top-level document request of `page`, as Playwright reports a navigation's."""

    def __init__(self, page: FiringPage, url: str, *, redirected_from: "NavigationRequest | None" = None) -> None:
        super().__init__(
            frame=SimpleNamespace(page=page, parent_frame=None), navigation=True, resource_type="document", url=url
        )
        self.redirected_from = redirected_from


def listened_page(manager: BrowserManager, url: str = ALLOWED) -> FiringPage:
    """A page with the diagnostics listeners on it, which mark refused hops and failed navigations."""
    page = FiringPage(url)
    session = SimpleNamespace(
        id="s1",
        attached_pages=weakref.WeakSet(),
        request_failures=[],
        lock=asyncio.Lock(),
        unframed_hops=[],
        refused_failures=deque(),
    )
    manager.diagnostics.attach_page_listeners(page, session)
    return page


def tainted_page(manager: BrowserManager) -> FiringPage:
    page = listened_page(manager)
    page.commit(manager, REFUSED)
    return page


# ── A controller navigation clears the taint when it ends on the list (round 4, A2 item 1) ──


def test_an_allowed_goto_with_no_redirects_clears_the_taint(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    page = tainted_page(manager)

    with manager.page_provenance.controller_navigation(page) as navigation:
        page.commit(manager, ALLOWED)
        navigation.response = goto_response(ALLOWED)

    assert not manager._page_refused(page)


def test_a_goto_whose_redirect_chain_passed_a_refused_url_leaves_the_taint(tmp_path: Path) -> None:
    """Judged by the response: the hop's request event is not what decides, the redirect chain is."""
    manager = manager_for(tmp_path)
    page = tainted_page(manager)

    with manager.page_provenance.controller_navigation(page) as navigation:
        page.commit(manager, f"{ALLOWED}?d=REFUSED-CONTENT")
        navigation.response = goto_response(f"{ALLOWED}/go", REFUSED, f"{ALLOWED}?d=REFUSED-CONTENT")

    assert manager._page_refused(page)


def test_a_same_document_commit_after_a_refused_hop_during_the_goto_leaves_the_taint(tmp_path: Path) -> None:
    """The hop taints the commit after it, and a same-document commit after that kept the document."""
    manager = manager_for(tmp_path)
    page = tainted_page(manager)

    with manager.page_provenance.controller_navigation(page) as navigation:
        page.fire("request", NavigationRequest(page, REFUSED))
        page.commit(manager, ALLOWED)
        page.commit(manager, f"{ALLOWED}#same-document")
        navigation.response = goto_response(ALLOWED)

    assert manager._page_refused(page)


def test_a_refused_hop_from_before_the_goto_that_commits_during_it_leaves_the_taint(tmp_path: Path) -> None:
    """The page's own navigation through a refused host was in flight when the goto started, and won."""
    manager = manager_for(tmp_path)
    page = listened_page(manager)
    page.fire("request", NavigationRequest(page, REFUSED))

    with manager.page_provenance.controller_navigation(page) as navigation:
        page.commit(manager, f"{ALLOWED}?d=REFUSED-CONTENT")
        navigation.response = goto_response(ALLOWED)

    assert manager._page_refused(page)


def test_control_a_refused_hop_that_failed_before_the_goto_committed_does_not_keep_the_taint(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    page = tainted_page(manager)
    hop = NavigationRequest(page, REFUSED)
    page.fire("request", hop)

    with manager.page_provenance.controller_navigation(page) as navigation:
        page.fire("requestfailed", hop)  # the goto aborted it
        page.commit(manager, ALLOWED)
        navigation.response = goto_response(ALLOWED)

    assert not manager._page_refused(page)


def test_a_goto_that_ends_with_the_page_off_the_list_leaves_the_taint(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    page = tainted_page(manager)

    with manager.page_provenance.controller_navigation(page) as navigation:
        page.commit(manager, ALLOWED)
        navigation.response = goto_response(ALLOWED)
        page.url = REFUSED  # where the page is when the goto returns

    assert manager.page_provenance.refused(page)


def test_a_goto_that_returned_no_response_leaves_the_taint(tmp_path: Path) -> None:
    """F1: a same-document goto (onto #top) returns None and keeps the document the refused page wrote, though
    the page's URL is allowed."""
    manager = manager_for(tmp_path)
    page = tainted_page(manager)
    page.commit(manager, ALLOWED)

    with manager.page_provenance.controller_navigation(page) as navigation:
        page.commit(manager, f"{ALLOWED}#top")
        navigation.response = None

    assert manager._page_refused(page)


@pytest.mark.parametrize("then", ["about:blank", "blob:null"])
def test_a_goto_whose_page_left_http_before_it_returned_leaves_the_taint(tmp_path: Path, then: str) -> None:
    """F1: the response was allowed, but the page committed a document a refused page can write into before
    the goto returned."""
    manager = manager_for(tmp_path)
    page = tainted_page(manager)

    with manager.page_provenance.controller_navigation(page) as navigation:
        page.commit(manager, ALLOWED)
        navigation.response = goto_response(ALLOWED)
        page.commit(manager, then)

    assert manager._page_refused(page)


def test_a_refused_hop_that_starts_and_fails_during_the_goto_does_not_keep_the_taint(tmp_path: Path) -> None:
    """PROV-2: a refused request the page sent during the goto that commits nothing (a 204 ping) brought
    no document from the refused host, and its mark kept the page refused after every navigate."""
    manager = manager_for(tmp_path)
    page = tainted_page(manager)

    with manager.page_provenance.controller_navigation(page) as navigation:
        hop = NavigationRequest(page, REFUSED)
        page.fire("request", hop)
        page.fire("requestfailed", hop)
        page.commit(manager, ALLOWED)
        navigation.response = goto_response(ALLOWED)

    assert not manager._page_refused(page)


def test_a_refused_hop_that_starts_and_commits_during_the_goto_keeps_the_taint(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    page = tainted_page(manager)

    with manager.page_provenance.controller_navigation(page) as navigation:
        page.fire("request", NavigationRequest(page, REFUSED))
        page.commit(manager, f"{ALLOWED}?d=REFUSED-CONTENT")  # where the refused host's redirect sent it
        navigation.response = goto_response(ALLOWED)

    assert manager._page_refused(page)


def test_a_refused_hop_still_in_flight_when_the_goto_returns_keeps_the_page_refused(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    page = tainted_page(manager)

    hop = NavigationRequest(page, REFUSED)
    with manager.page_provenance.controller_navigation(page) as navigation:
        page.commit(manager, ALLOWED)
        page.fire("request", hop)
        navigation.response = goto_response(ALLOWED)
    page.fire("requestfailed", hop)  # after the goto: the page was not cleared, so it stays refused

    assert manager._page_refused(page)


# ── The refused-hop mark is cleared only by its own navigation (A2 item 2) ──


def test_a_refused_hop_is_not_cleared_by_another_navigations_failure(tmp_path: Path) -> None:
    """Request 1's failure arrived after request 2 had marked the page. It cleared request 2's
    mark, and the commit that request 2's redirect chose was not tainted."""
    manager = manager_for(tmp_path)
    page = listened_page(manager)
    first, hop = NavigationRequest(page, f"{ALLOWED}/one"), NavigationRequest(page, REFUSED)

    page.fire("request", first)
    page.fire("request", hop)
    page.fire("requestfailed", first)
    assert manager._page_refused(page), "the hop's mark is kept"
    page.commit(manager, f"{ALLOWED}?d=REFUSED-CONTENT")

    assert manager._page_refused(page)


def test_control_a_failure_further_along_the_hops_redirect_chain_clears_its_mark(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    page = listened_page(manager)
    hop = NavigationRequest(page, REFUSED)
    redirected = NavigationRequest(page, f"{ALLOWED}/no-content", redirected_from=hop)

    page.fire("request", hop)
    page.fire("request", redirected)
    page.fire("requestfailed", redirected)  # a 204 at the end of the chain: no commit

    assert not manager._page_refused(page)


# ── A refused opener taints its popup whatever the popup's URL (A2 item 3) ──


async def test_a_popup_on_an_allowed_url_whose_opener_is_refused_is_refused_and_shown_by_its_origin(
    tmp_path: Path,
) -> None:
    """The refused opener chose the popup's path and query (window.open), which can carry what it read."""
    manager = manager_for(tmp_path)
    session = popup_session()
    opener = opener_on(manager, session, REFUSED, refused=True)
    popup = EventPage(f"{ALLOWED}?d=REFUSED-CONTENT", opener)

    report(manager, session, popup)
    await drain()

    assert manager._page_refused(popup)
    assert manager._shown_url(popup) == "https://allowed.example"


# ── The controller's go_back off a blocked action's page restores the page it left (A2 item 4) ──


class HistoryPage(FiringPage):
    """A page whose go_back lands on `back_to`; `on_back` runs first, for what the way back passes."""

    def __init__(self, manager: BrowserManager, url: str, back_to: str, on_back=None) -> None:
        super().__init__(url)
        self.manager, self.back_to, self.on_back = manager, back_to, on_back

    async def go_back(self, **_kwargs) -> None:
        if self.on_back is not None:
            self.on_back(self)
        self.commit(self.manager, self.back_to)

    async def goto(self, url: str, **_kwargs) -> None:
        self.commit(self.manager, url)

    def is_closed(self) -> bool:
        return False


def action_session(manager: BrowserManager, page: HistoryPage, tmp_path: Path) -> BrowserSession:
    """A session the real action pipeline runs on, with its records and the page reads stubbed."""
    session = BrowserSession(
        id="s1",
        name="s1",
        created_at=datetime.now(UTC),
        context=SimpleNamespace(pages=[page]),
        page=page,
        artifact_dir=tmp_path,
        auth_dir=tmp_path,
        upload_dir=tmp_path,
        takeover_url="",
        trace_path=tmp_path / "trace.zip",
    )
    manager.diagnostics.attach_page_listeners(page, session)
    manager._light_snapshot = AsyncMock(side_effect=lambda s, label: {"url": s.page.url})
    manager._append_jsonl = AsyncMock()
    manager.audit.append = AsyncMock()
    manager._record_witness_receipt = AsyncMock()
    manager._settle = AsyncMock()
    return session


async def blocked_action(manager: BrowserManager, session: BrowserSession, action: str = "click") -> None:
    """An action that takes the page to a refused host: the policy blocks it, and its rollback goes back."""

    async def operation() -> None:
        session.page.commit(manager, REFUSED)

    with pytest.raises(BrowserActionError) as blocked:
        await manager._run_action(session, action, {}, operation)
    assert blocked.value.code == "browser_action_blocked"


async def test_the_escape_back_to_the_page_read_before_the_action_makes_it_readable(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    page = HistoryPage(manager, ALLOWED, back_to=ALLOWED)

    await blocked_action(manager, action_session(manager, page, tmp_path))

    assert page.url == ALLOWED
    assert not manager._page_refused(page)


async def test_an_escape_that_lands_on_another_url_leaves_the_page_refused(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    page = HistoryPage(manager, ALLOWED, back_to=f"{ALLOWED}/elsewhere")

    await blocked_action(manager, action_session(manager, page, tmp_path))

    assert manager._page_refused(page)


async def test_an_escape_back_through_a_refused_hop_leaves_the_page_refused(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    page = HistoryPage(
        manager, ALLOWED, back_to=ALLOWED, on_back=lambda p: p.fire("request", NavigationRequest(p, REFUSED))
    )

    await blocked_action(manager, action_session(manager, page, tmp_path))

    assert manager._page_refused(page)


async def test_an_escape_from_a_page_refused_before_the_action_leaves_it_refused(tmp_path: Path) -> None:
    """Recorded for a refused page is its origin, or about:blank for the guard's blank: going back to
    that blank matches it, and the refused page may have written into the window."""
    manager = manager_for(tmp_path)
    page = HistoryPage(manager, ALLOWED, back_to="about:blank")
    session = action_session(manager, page, tmp_path)
    page.commit(manager, REFUSED)
    manager.page_provenance.blank_started(page)
    page.commit(manager, "about:blank")

    await blocked_action(manager, session, action="navigate")

    assert page.url == "about:blank"
    assert manager._page_refused(page)


# ── A service worker's request while a page of the session is refused (A2 item 5) ──


def two_tab_network(manager: BrowserManager, *, tainted: bool) -> tuple[list[FiringPage], NetworkInspector]:
    """A session of two tabs on an allowed URL, the first tainted when `tainted`, and its network inspector."""
    pages = [FiringPage(ALLOWED), FiringPage(ALLOWED)]
    session = SimpleNamespace(page=pages[1], context=SimpleNamespace(pages=pages))
    if tainted:
        manager.page_provenance.committed(pages[0], REFUSED, url_refused=True)
    network = NetworkInspector(
        "s1",
        url_refused=manager._url_off_list,
        page_refused=manager._page_refused,
        session_refused=lambda: manager._session_has_refused_page(session),
    )
    return pages, network


@pytest.mark.parametrize(("tainted", "withheld"), [(True, True), (False, False)], ids=["one-page-refused", "control"])
async def test_an_allowed_service_workers_request_is_withheld_while_a_page_of_the_session_is_refused(
    tmp_path: Path, tainted: bool, withheld: bool
) -> None:
    """The worker serves every page of its origin, a refused one among them, and has no frame to judge it by."""
    manager = manager_for(tmp_path)
    _, network = two_tab_network(manager, tainted=tainted)
    request = FramelessRequest(
        worker_url="https://allowed.example/sw.js",
        method="POST",
        resource_type="fetch",
        url="https://allowed.example/collect?d=REFUSED-CONTENT",
    )

    entry = await record(network, request)

    assert ("withheld" in entry) is withheld, entry


# ── A popup whose first navigation passed a refused redirect (A2 item 6) ──


class UnframedRequest(FramedRequest):
    """A popup's first navigation request: Playwright cannot name its frame until the context reports the page."""

    service_worker = None

    def __init__(self, url: str, *, redirected_from: "UnframedRequest | None" = None) -> None:
        super().__init__(frame=None, navigation=True, resource_type="document", url=url)
        self.redirected_from = redirected_from

    def reported(self, page: EventPage) -> None:
        """The page exists now, and the request's frame is its main frame."""
        self._frame = SimpleNamespace(page=page, parent_frame=None)


async def test_a_popup_whose_first_navigation_passed_a_refused_redirect_is_refused(tmp_path: Path) -> None:
    """Its own listeners attach only when the context reports it, after that navigation: the context alone
    saw the hop, and the popup committed the allowed URL the refused host's 302 chose."""
    manager = manager_for(tmp_path)
    session = popup_session()
    opener = opener_on(manager, session, ALLOWED, refused=False)
    popup = EventPage(f"{ALLOWED}?d=REFUSED-CONTENT", opener)
    hop = UnframedRequest(REFUSED)
    manager.diagnostics.on_context_request(session, hop)
    hop.reported(popup)

    report(manager, session, popup)
    await drain()

    assert manager._page_refused(popup)
    assert manager._shown_url(popup) == "https://allowed.example"


async def test_control_hops_of_other_pages_do_not_taint_the_popup(tmp_path: Path) -> None:
    """One resolves to a page attached already and is dropped; one whose page is not reported yet is kept."""
    manager = manager_for(tmp_path)
    session = popup_session()
    opener = opener_on(manager, session, ALLOWED, refused=False)
    popup = EventPage(ALLOWED, opener)
    elsewhere, pending = UnframedRequest(REFUSED), UnframedRequest(f"{REFUSED}?2")
    manager.diagnostics.on_context_request(session, elsewhere)
    manager.diagnostics.on_context_request(session, pending)
    elsewhere.reported(opener)

    report(manager, session, popup)
    await drain()

    assert not manager._page_refused(popup)
    assert session.unframed_hops == [pending]


def test_a_kept_hop_whose_navigation_failed_is_dropped(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    session = popup_session()
    hop = UnframedRequest(REFUSED)
    landing = UnframedRequest(f"{ALLOWED}/no-content", redirected_from=hop)
    manager.diagnostics.on_context_request(session, hop)
    manager.diagnostics.on_context_request(session, landing)  # allowed, but redirected from the kept hop: kept

    manager.diagnostics.on_context_request_failed(session, landing)  # a 204 at the end of the chain

    assert session.unframed_hops == []


# ── The network log for navigations a refused page may have chosen (A2 item 8) ──


async def test_a_popups_first_navigation_is_withheld_whatever_its_url(tmp_path: Path) -> None:
    """Its frame is not available yet, so the page that chose it cannot be judged: a refused opener's choice."""
    manager = manager_for(tmp_path)
    _, network = two_tab_network(manager, tainted=False)

    entry = await record(network, UnframedRequest(f"{ALLOWED}/no-content?q=REFUSED-CONTENT"))

    assert (entry.get("withheld"), entry["url"]) == (WITHHELD, "https://allowed.example"), entry


@pytest.mark.parametrize(
    ("tainted", "withheld"), [(True, True), (False, False)], ids=["while-a-tab-is-refused", "control-allowed-page"]
)
async def test_a_navigation_that_starts_while_a_page_of_the_session_is_refused_is_withheld(
    tmp_path: Path, tainted: bool, withheld: bool
) -> None:
    manager = manager_for(tmp_path)
    pages, network = two_tab_network(manager, tainted=tainted)

    entry = await record(network, NavigationRequest(pages[1], f"{ALLOWED}/next?q=REFUSED-CONTENT"))

    assert ("withheld" in entry) is withheld, entry


# ── Request failures (A2 item 9) ──


async def test_a_failed_top_level_navigation_to_a_refused_url_is_not_listed(tmp_path: Path) -> None:
    """Its start was not seen, and the allowed page it would have left is not refused: its URL is."""
    manager = manager_for(tmp_path)
    page = FiringPage(ALLOWED)
    session = SimpleNamespace(id="s1", attached_pages=weakref.WeakSet(), request_failures=[], lock=asyncio.Lock())
    manager.diagnostics.attach_page_listeners(page, session)

    page.fire("requestfailed", NavigationRequest(page, f"{REFUSED}?q=REFUSED-CONTENT"))
    await drain()

    assert session.request_failures == []


async def test_a_refused_pages_request_that_fails_after_the_agent_navigated_the_page_is_not_listed(
    tmp_path: Path,
) -> None:
    """The agent's navigation aborts what the refused page had in flight; those failures arrive after it."""
    manager = manager_for(tmp_path)
    page = FiringPage(ALLOWED)
    session = SimpleNamespace(id="s1", attached_pages=weakref.WeakSet(), request_failures=[], lock=asyncio.Lock())
    manager.diagnostics.attach_page_listeners(page, session)
    page.commit(manager, REFUSED)
    request = FramedRequest(frame=SimpleNamespace(page=page, parent_frame=None), url="https://cdn.example/x?q=REFUSED")
    page.fire("request", request)

    with manager.page_provenance.controller_navigation(page) as navigation:
        page.commit(manager, ALLOWED)
        navigation.response = goto_response(ALLOWED)
    assert not manager._page_refused(page), "the agent's navigation cleared the taint"
    page.fire("requestfailed", request)
    await drain()

    assert session.request_failures == []


# ── Edges of the guard and the pipeline on a tainted page (round 4, D1) ──


class RecordingPage(HistoryPage):
    """A HistoryPage that records its gotos and go_backs; with fail_goto, a goto anywhere but about:blank fails."""

    def __init__(self, manager: BrowserManager, url: str, *, fail_goto: bool = False) -> None:
        super().__init__(manager, url, back_to=url)
        self.fail_goto = fail_goto
        self.calls: list[str] = []

    async def goto(self, url: str, **_kwargs) -> None:
        self.calls.append(f"goto {url}")
        if self.fail_goto and url != "about:blank":
            raise PlaywrightError("net::ERR_NAME_NOT_RESOLVED")
        self.commit(self.manager, url)

    async def go_back(self, **kwargs) -> None:
        self.calls.append("go_back")
        await super().go_back(**kwargs)


def on_the_guards_blank(manager: BrowserManager, page: FiringPage) -> None:
    """The page committed a refused URL, and the guard has sent it to about:blank: tainted, and blanked."""
    page.commit(manager, REFUSED)
    manager.page_provenance.blank_started(page)
    page.commit(manager, "about:blank")


def guard_blanks(manager: BrowserManager) -> list:
    return [c for c in manager.audit.append.await_args_list if c.kwargs.get("event_type") == "navigation_blocked"]


async def test_after_a_failed_goto_a_later_tainted_blank_is_still_sent_back(tmp_path: Path) -> None:
    """The agent's goto off the guard's blank fails (a DNS error): the taint stays, so when the page commits
    about:blank on its own afterwards (its refused opener writing into it), the guard sends it back."""
    manager = manager_for(tmp_path)
    page = RecordingPage(manager, ALLOWED, fail_goto=True)
    session = action_session(manager, page, tmp_path)
    manager.sessions[session.id] = session
    on_the_guards_blank(manager, page)

    with pytest.raises(BrowserActionError) as failed:
        await manager.navigate(session.id, f"{ALLOWED}/next")
    assert failed.value.code == "browser_action_failed"
    page.url = page.main_frame.url = "about:blank"
    page.fire("framenavigated", page.main_frame)
    await drain()

    assert page.calls == [f"goto {ALLOWED}/next", "goto about:blank"]
    assert len(guard_blanks(manager)) == 1
    assert manager._page_refused(page)


async def test_a_requestfailed_with_no_request_event_on_a_refused_page_is_not_listed(tmp_path: Path) -> None:
    """Its start was never seen, so no start verdict was kept: the page is judged as the failure arrives."""
    manager = manager_for(tmp_path)
    page = FiringPage(ALLOWED)
    session = SimpleNamespace(id="s1", attached_pages=weakref.WeakSet(), request_failures=[], lock=asyncio.Lock())
    manager.diagnostics.attach_page_listeners(page, session)
    on_the_guards_blank(manager, page)

    page.fire("requestfailed", FakeRequest(url=f"{ALLOWED}/x?q=REFUSED-TEXT"))
    await drain()

    assert session.request_failures == []


async def test_control_a_requestfailed_with_no_request_event_on_an_allowed_page_is_listed(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    page = FiringPage(ALLOWED)
    session = SimpleNamespace(id="s1", attached_pages=weakref.WeakSet(), request_failures=[], lock=asyncio.Lock())
    manager.diagnostics.attach_page_listeners(page, session)

    page.fire("requestfailed", FakeRequest(url=f"{ALLOWED}/x"))
    await drain()

    assert [failure["url"] for failure in session.request_failures] == [f"{ALLOWED}/x"]


def test_a_closed_page_leaves_no_provenance_behind(tmp_path: Path) -> None:
    """Its state is keyed by id(page), which a later page can be given once this one is collected."""
    manager = manager_for(tmp_path)
    page = RecordingPage(manager, ALLOWED)
    action_session(manager, page, tmp_path)
    on_the_guards_blank(manager, page)
    assert manager.page_provenance.refused(page) and manager.page_provenance.refused_commits(page) == 1

    page.fire("close", page)

    assert not manager.page_provenance.refused(page)
    assert manager.page_provenance.refused_commits(page) == 0


async def test_a_policy_blocked_action_on_a_tainted_blank_moves_the_page_nowhere(tmp_path: Path) -> None:
    """The page is on the guard's blank already: no go_back and no second goto to about:blank."""
    manager = manager_for(tmp_path)
    page = RecordingPage(manager, ALLOWED)
    session = action_session(manager, page, tmp_path)
    on_the_guards_blank(manager, page)
    ran: list[int] = []

    async def operation() -> None:
        ran.append(1)

    with pytest.raises(BrowserActionError) as blocked:
        await manager._run_action(session, "click", {}, operation)

    assert (blocked.value.code, ran) == ("browser_action_blocked", [])
    assert page.calls == []
    assert page.url == "about:blank"


class GuardedRecordingPage(RecordingPage):
    """A RecordingPage whose commits fire framenavigated, as Playwright's do, so the real page guard judges
    them (RecordingPage records them in provenance directly, and the guard never runs). Its first
    ``blank_failures`` gotos to about:blank fail."""

    def __init__(self, manager: BrowserManager, url: str, *, blank_failures: int = 0) -> None:
        super().__init__(manager, url)
        self.blank_failures = blank_failures

    def commit(self, manager: BrowserManager, url: str) -> None:
        self.url = self.main_frame.url = url
        self.fire("framenavigated", self.main_frame)

    async def goto(self, url: str, **kwargs) -> None:
        if url == "about:blank" and self.blank_failures:
            self.blank_failures -= 1
            self.calls.append(f"goto {url}")
            raise PlaywrightError("net::ERR_ABORTED")
        await super().goto(url, **kwargs)


async def blocked_off_a_page_with_no_way_back(manager: BrowserManager, page: GuardedRecordingPage, tmp_path: Path):
    """A click that takes the page to a refused host after location.replace(): go_back stays put, so the
    pipeline sends the page to about:blank itself."""
    page.back_to = REFUSED
    session = action_session(manager, page, tmp_path)
    manager.sessions[session.id] = session

    async def operation() -> None:
        page.commit(manager, REFUSED)

    with pytest.raises(BrowserActionError) as blocked:
        await manager._run_action(session, "click", {}, operation)
    await drain()
    assert blocked.value.code == "browser_action_blocked"


async def test_the_pipelines_blank_off_a_refused_page_is_the_controllers_own(tmp_path: Path) -> None:
    """PROV-4: the blank a blocked action's page is sent to was not marked as the controller's, so the guard
    took it for a refused commit and blanked the page a second time, with a second audit event. Round 4d
    (T1): on a page whose commits reach the guard; RecordingPage's never did, so this passed unfixed."""
    manager = manager_for(tmp_path)
    page = GuardedRecordingPage(manager, ALLOWED)

    await blocked_off_a_page_with_no_way_back(manager, page, tmp_path)

    assert page.calls == ["go_back", "goto about:blank"]
    assert guard_blanks(manager) == []
    assert manager.page_provenance.refused_commits(page) == 2  # the click's commit and go_back's
    assert manager._page_refused(page)


async def test_a_pipeline_blank_that_fails_is_not_left_counted_as_the_controllers_own(tmp_path: Path) -> None:
    """T1, the blank_failed branch: the pipeline's blank fails, and the guard moves the page instead. Left
    counted, the failed blank exempted the page's next about:blank commit of its own (a refused opener
    writing into it), which the guard then never sent back."""
    manager = manager_for(tmp_path)
    page = GuardedRecordingPage(manager, ALLOWED, blank_failures=1)
    await blocked_off_a_page_with_no_way_back(manager, page, tmp_path)
    assert len(guard_blanks(manager)) == 1  # the guard's, for the refused page the pipeline could not move

    page.commit(manager, "about:blank")
    await drain()

    assert len(guard_blanks(manager)) == 2
    assert page.calls == ["go_back", "goto about:blank", "goto about:blank", "goto about:blank"]


# ── A blank that never committed (round 4d, R4C-L1-4) ──


async def cancelled_goto(*_args, **_kwargs) -> None:
    raise asyncio.CancelledError


def test_a_controller_navigation_drops_a_blank_that_never_committed(tmp_path: Path) -> None:
    """A blank whose goto was cancelled before its commit stayed counted. After the agent's navigate, an
    about:blank commit the page made itself while another tab was refused was taken for the controller's
    blank, so it was not tainted and what the refused tab wrote there was read."""
    manager = manager_for(tmp_path)
    provenance = manager.page_provenance
    page = FiringPage(ALLOWED)
    page.commit(manager, REFUSED)
    provenance.blank_started(page)  # its goto cancelled before the blank committed

    with provenance.controller_navigation(page) as navigation:
        page.commit(manager, ALLOWED)
        navigation.response = goto_response(ALLOWED)
    assert not provenance.refused(page)

    assert provenance.committed(page, "about:blank", url_refused=False, by_another_page=True)
    assert provenance.refused(page)


@pytest.mark.parametrize("blank", ["pipeline", "guard"])
async def test_a_cancelled_blank_is_not_left_counted_as_the_controllers_own(tmp_path: Path, blank: str) -> None:
    """Cancelled before its commit, the blank stayed counted: the page's next about:blank commit, which a
    refused opener can make, was taken for it and not counted as a refused commit."""
    manager = manager_for(tmp_path)
    page = RecordingPage(manager, ALLOWED)
    session = action_session(manager, page, tmp_path)
    manager.sessions[session.id] = session
    page.commit(manager, REFUSED)
    page.goto = cancelled_goto

    with pytest.raises(asyncio.CancelledError):
        if blank == "pipeline":
            await BrowserActionPipeline._leave_disallowed_page(SimpleNamespace(manager=manager, session=session))
        else:
            await manager.diagnostics._return_to_blank(session, page)

    assert manager.page_provenance.committed(page, "about:blank", url_refused=False)

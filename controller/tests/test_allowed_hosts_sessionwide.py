"""What a refused page reaches beyond itself (round 4c): another tab it navigates, requests from a page
the browser context has not reported yet, and a download its redirect chose.

Fakes deliver Playwright's events in the order each case needs, so every run sees the same order.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError
from app.browser import page_provenance
from app.downloads import DownloadCaptureService
from app.models import BrowserActionDecision
from app.network_inspector import url_origin
from tests.test_allowed_hosts_provenance import NavigationRequest, UnframedRequest, action_session, listened_page
from tests.test_allowed_hosts_rechecks import (
    ALLOWED,
    REFUSED,
    EventPage,
    drain,
    goto_response,
    manager_for,
    popup_session,
    report,
)

# An allowed URL whose query a refused page chose: what it read can travel in it.
CHOSEN = "https://allowed.example/search?q=REFUSED-PAGE-TEXT"
# A grace these tests can wait past (page_provenance.CLOSED_PAGE_GRACE_SECONDS).
SHORT_GRACE_SECONDS = 0.05
# A grace a navigate can wait out quickly (item 30), and the page it goes to.
TEST_GRACE_SECONDS = 0.3
NEXT = f"{ALLOWED}/next"


async def past_the_grace(monkeypatch) -> None:
    """The refused tab closed long enough ago that it no longer counts for what the other tabs do."""
    # raising=False: before the grace existed this sets nothing the code reads.
    monkeypatch.setattr(page_provenance, "CLOSED_PAGE_GRACE_SECONDS", SHORT_GRACE_SECONDS, raising=False)
    await asyncio.sleep(SHORT_GRACE_SECONDS * 2)


# ── A tab a refused tab navigates (item 3, PROV-1) ──


def opener_and_refused_popup(manager):
    """A clean opener and the popup it opened without noopener, which then went off the list."""
    opener = EventPage(ALLOWED)
    popup = EventPage(ALLOWED, opener=opener)
    session = popup_session(opener)
    manager._attach_page_listeners(opener, session)
    report(manager, session, popup)  # judged by its clean opener: clean
    popup.commit(manager, REFUSED)
    assert manager._page_refused(popup)
    return session, opener, popup


async def test_a_tab_a_refused_tab_navigates_to_an_allowed_url_is_refused_and_told_why(tmp_path: Path) -> None:
    """window.opener.location is writable across origins: the refused popup sent its opener to an allowed URL
    whose query it chose, and the opener committed it untainted and was read with that URL."""
    manager = manager_for(tmp_path)
    _session, opener, _popup = opener_and_refused_popup(manager)

    opener.commit(manager, CHOSEN)
    await drain()

    assert manager._page_refused(opener)
    assert manager._shown_url(opener) == url_origin(CHOSEN)
    with pytest.raises(PermissionError, match="another tab of this session"):
        manager._assert_page_object_allowed(opener)


async def test_the_agents_navigate_clears_a_tab_another_tab_made_refused(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    _session, opener, _popup = opener_and_refused_popup(manager)
    opener.commit(manager, CHOSEN)

    with manager.page_provenance.controller_navigation(opener) as navigation:
        opener.commit(manager, ALLOWED)
        navigation.response = goto_response(ALLOWED)
    await drain()

    assert not manager._page_refused(opener)


async def test_control_a_tabs_own_navigation_is_read_while_no_tab_of_the_session_is_refused(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    opener = EventPage(ALLOWED)
    popup = EventPage(ALLOWED, opener=opener)
    session = popup_session(opener)
    manager._attach_page_listeners(opener, session)
    report(manager, session, popup)

    opener.commit(manager, CHOSEN)
    await drain()

    assert not manager._page_refused(opener)
    assert manager._shown_url(opener) == CHOSEN


# ── Which tabs count as refused for the others (round 4d, item 2: R4C-T3, R4C-PROV-2) ──


def tabs_session(manager, *tabs: EventPage) -> SimpleNamespace:
    manager.audit.append = AsyncMock()
    session = popup_session(*tabs)
    manager.sessions[session.id] = session
    for tab in tabs:
        manager._attach_page_listeners(tab, session)
    return session


async def test_a_tab_refused_only_by_another_tab_does_not_keep_the_others_refused(tmp_path: Path, monkeypatch) -> None:
    """A tab refused only because another tab was refused counted as refused itself, so the refusal
    cascaded. With the off-list tab closed, a third tab's click was refused because of the second. Then,
    after the agent navigated the second as the refusal told it, the second's next click was refused
    because of the third. These tabs' documents come from an allowed origin, so what their scripts do is
    the accepted limit: an allowed host's own scripts."""
    manager = manager_for(tmp_path)
    tab_b, tab_c = EventPage(ALLOWED), EventPage(ALLOWED)
    session = tabs_session(manager, tab_b, tab_c)
    popup_a = EventPage(ALLOWED, opener=tab_b)
    report(manager, session, popup_a)
    popup_a.commit(manager, REFUSED)
    await drain()
    assert popup_a.url == "about:blank"  # on the guard's blank, still refused: it still counts
    tab_b.commit(manager, f"{ALLOWED}/b-link")  # the agent clicks a link in B while A is refused
    await drain()
    assert manager.page_provenance.refused_by_another_page(tab_b)

    popup_a.close()
    session.context.pages.remove(popup_a)
    await past_the_grace(monkeypatch)
    tab_c.commit(manager, f"{ALLOWED}/c-link")
    await drain()

    assert not manager._page_refused(tab_c)
    assert tab_c.url == f"{ALLOWED}/c-link"
    assert manager._page_refused(tab_b)  # B itself stays refused until the agent navigates it

    with manager.page_provenance.controller_navigation(tab_b) as navigation:
        tab_b.commit(manager, f"{ALLOWED}/b-fresh")
        navigation.response = goto_response(f"{ALLOWED}/b-fresh")
    await drain()
    tab_b.commit(manager, f"{ALLOWED}/b-link-2")
    await drain()

    assert not manager._page_refused(tab_b)
    assert tab_b.url == f"{ALLOWED}/b-link-2"
    manager._assert_page_object_allowed(tab_b)


async def test_a_tab_still_waiting_for_its_openers_verdict_does_not_refuse_the_others(tmp_path: Path) -> None:
    """A popup whose opener closed before Playwright's popup event never gets a verdict, so it stays
    refused for the session's life. Counted for the session, it refused every other tab's own
    navigation, the agent's clicks included, until the agent found and closed it."""
    manager = manager_for(tmp_path)
    tab = EventPage(ALLOWED)
    session = tabs_session(manager, tab)
    gone = EventPage(ALLOWED)
    gone.close()
    phantom = EventPage(f"{ALLOWED}/sso", opener=gone)
    report(manager, session, phantom)
    assert manager._page_refused(phantom)  # refused for its own reads, as before

    tab.commit(manager, f"{ALLOWED}/next")
    await drain()

    assert not manager._page_refused(tab)
    assert manager._shown_url(tab) == f"{ALLOWED}/next"


async def test_a_tab_with_a_refused_hop_in_flight_still_refuses_the_others(tmp_path: Path) -> None:
    """Its next document is the refused host's, so its scripts can be too."""
    manager = manager_for(tmp_path)
    tab, other = EventPage(ALLOWED), EventPage(ALLOWED)
    tabs_session(manager, tab, other)
    manager.page_provenance.refused_hop_started(other, NavigationRequest(other, REFUSED))

    tab.commit(manager, CHOSEN)
    await drain()

    assert manager.page_provenance.refused_by_another_page(tab)


# ── A blank, blob: or data: document has the origin of the page that made it (round 4e, item 1) ──

# An allowed URL a script a refused tab wrote chose: what it read can travel in its path and query.
SECRET_URL = f"{ALLOWED}/REFUSED-SECRET?private=VALUE"


@pytest.mark.parametrize(
    "made_there", ["about:blank", "data:text/html,x", "blob:null/1f2e"], ids=["blank", "data", "opaque-blob"]
)
async def test_a_tab_a_refused_tab_blanked_and_wrote_into_still_refuses_the_others_after_it_closes(
    tmp_path: Path, made_there: str, monkeypatch
) -> None:
    """Round 4e (Codex 1): the refused tab sent another tab to a document with the refused tab's origin and
    wrote what it read there. Round 4d took that tab for one refused only because of another tab, whose
    document comes from an allowed origin, so once the refused tab closed it no longer counted, and a third
    tab its script then sent to an allowed URL that the script chose was read with that URL."""
    manager = manager_for(tmp_path)
    written, third = EventPage(ALLOWED), EventPage(ALLOWED)
    session = tabs_session(manager, written, third)
    refused = EventPage(REFUSED)
    session.context.pages.append(refused)
    manager._attach_page_listeners(refused, session)
    written.commit(manager, made_there)  # the refused tab sends it there and writes into it
    await drain()
    refused.close()
    session.context.pages.remove(refused)
    await past_the_grace(monkeypatch)

    third.commit(manager, SECRET_URL)  # the script written into the blank sends the third tab there

    assert manager._page_refused(third)
    assert manager._shown_url(third) == url_origin(SECRET_URL)
    await drain()


async def test_a_blank_tab_still_waiting_for_its_openers_verdict_refuses_the_others(tmp_path: Path) -> None:
    """Round 4e (Codex 1): a blank window a refused page opened, which closed before Playwright's popup
    event, has that page's origin and never gets a verdict. Round 4d did not count a tab still waiting for its
    verdict, so the script the refused page wrote into the window could send another tab to an allowed URL it
    chose. The same tab on an allowed http(s) URL still does not count (the 4d test above)."""
    manager = manager_for(tmp_path)
    other = EventPage(ALLOWED)
    session = tabs_session(manager, other)
    gone = EventPage(REFUSED)
    gone.close()
    report(manager, session, EventPage("about:blank", opener=gone))

    other.commit(manager, SECRET_URL)

    assert manager._page_refused(other)
    assert manager._shown_url(other) == url_origin(SECRET_URL)
    await drain()


async def test_control_a_blank_popup_its_allowed_opener_judged_does_not_refuse_the_others(tmp_path: Path) -> None:
    """A sign-in popup still on its first blank, judged by an allowed opener, holds nothing a refused page made."""
    manager = manager_for(tmp_path)
    opener, other = EventPage(ALLOWED), EventPage(ALLOWED)
    session = tabs_session(manager, opener, other)
    report(manager, session, EventPage("about:blank", opener=opener))

    other.commit(manager, f"{ALLOWED}/next")
    await drain()

    assert not manager._page_refused(other)
    assert other.url == f"{ALLOWED}/next"


# ── A refused tab that closes before what it did is delivered (final review, PROV-1) ──


async def opener_and_refused_tab(manager) -> tuple[SimpleNamespace, EventPage, EventPage]:
    opener = EventPage(ALLOWED)
    session = tabs_session(manager, opener)
    refused = EventPage(REFUSED)
    session.context.pages.append(refused)
    manager._attach_page_listeners(refused, session)
    await drain()  # the guard's blank: the tab stays tainted, so it still counts
    return session, opener, refused


def closed(session: SimpleNamespace, tab: EventPage) -> None:
    """window.close(): the close event, and the context drops the page."""
    tab.close()
    session.context.pages.remove(tab)


async def test_a_navigation_a_refused_tab_started_is_refused_though_the_tab_closed_before_its_commit(
    tmp_path: Path, monkeypatch
) -> None:
    """PROV-1: the refused popup set window.opener.location to a URL it chose, then called window.close().
    Its close was delivered before the opener's commit, so the session had no refused tab by then, and the
    opener was read with that URL. Past the grace, so what refuses it is the mark its request got."""
    manager = manager_for(tmp_path)
    session, opener, refused = await opener_and_refused_tab(manager)
    opener.fire("request", NavigationRequest(opener, CHOSEN))
    closed(session, refused)
    await past_the_grace(monkeypatch)

    opener.commit(manager, CHOSEN)

    assert manager._page_refused(opener)
    assert manager._shown_url(opener) == url_origin(CHOSEN)
    await drain()


async def test_a_navigation_that_starts_just_after_a_refused_tab_closed_is_refused(tmp_path: Path, monkeypatch) -> None:
    """PROV-1, the other order: the close is delivered before the opener's navigation request."""
    manager = manager_for(tmp_path)
    session, opener, refused = await opener_and_refused_tab(manager)
    closed(session, refused)
    opener.fire("request", NavigationRequest(opener, CHOSEN))
    await past_the_grace(monkeypatch)

    opener.commit(manager, CHOSEN)

    assert manager._page_refused(opener)
    assert manager._shown_url(opener) == url_origin(CHOSEN)
    await drain()


class AgentClosedPage(EventPage):
    """A tab the agent closes through the tab service: Playwright's Page.close() is a coroutine, and the
    context drops the page."""

    def __init__(self, url: str, pages: list) -> None:
        super().__init__(url)
        self._pages = pages

    async def close(self) -> None:
        super().close()
        self._pages.remove(self)


async def test_a_navigation_in_flight_when_the_agent_closes_the_refused_tab_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """PROV-1: the agent closed the refused tab (browser.close_tab) while the navigation it caused was in flight."""
    manager = manager_for(tmp_path)
    opener = EventPage(ALLOWED)
    session = tabs_session(manager, opener)
    refused = AgentClosedPage(REFUSED, session.context.pages)
    session.context.pages.append(refused)
    manager._attach_page_listeners(refused, session)
    await drain()
    manager.get_session = AsyncMock(return_value=session)
    manager._persist_session = AsyncMock()
    manager._session_summary = AsyncMock(return_value={})
    opener.fire("request", NavigationRequest(opener, CHOSEN))
    await manager.tabs.close(session.id, 1)
    await past_the_grace(monkeypatch)

    opener.commit(manager, CHOSEN)

    assert manager._page_refused(opener)
    await drain()


class LoadingPage(EventPage):
    """A page whose goto commits the URL and then, while the page loads, a #fragment it sets (same document)."""

    def __init__(self, manager, url: str) -> None:
        super().__init__(url)
        self.manager, self.goto_at = manager, None

    async def goto(self, url: str, **kwargs):
        if url == "about:blank":  # the page guard's blank
            return await super().goto(url, **kwargs)
        self.goto_at = time.monotonic()
        self.fire("request", NavigationRequest(self, url))
        self.commit(self.manager, url)
        self.commit(self.manager, f"{url}#top")
        return goto_response(url)

    def is_closed(self) -> bool:
        return self.closed


async def session_with_a_refused_tab(manager, tmp_path: Path, monkeypatch):
    """A session on an allowed tab with another tab, refused, open: (session, the tab, the refused tab)."""
    monkeypatch.setattr(page_provenance, "CLOSED_PAGE_GRACE_SECONDS", TEST_GRACE_SECONDS)
    tab = LoadingPage(manager, ALLOWED)
    session = action_session(manager, tab, tmp_path)
    manager.sessions[session.id] = session
    manager._check_bot_challenge = AsyncMock(return_value=None)
    manager._observation_payload = AsyncMock(side_effect=lambda s, **_kwargs: {"url": s.page.url})
    refused = EventPage(REFUSED)
    session.context.pages.append(refused)
    manager._attach_page_listeners(refused, session)
    await drain()  # the guard's blank: the refused tab still counts
    return session, tab, refused


async def refused_tab_just_closed(manager, tmp_path: Path, monkeypatch):
    """A session on an allowed tab whose other tab, refused, has just closed: (session, the tab, when it closed)."""
    session, tab, refused = await session_with_a_refused_tab(manager, tmp_path, monkeypatch)
    closed(session, refused)
    return session, tab, manager.page_provenance._counting_closed_at[session.id]


async def test_the_grace_wait_ends_at_its_bound_however_many_closes_follow(monkeypatch) -> None:
    """Item 49 (Fable E-NAV-1): each close of a page that counted restarted the wait, so a refused tab closing
    popups faster than the grace kept the agent's navigate waiting for good, holding the session lock."""
    monkeypatch.setattr(page_provenance, "CLOSED_PAGE_GRACE_SECONDS", TEST_GRACE_SECONDS)
    provenance = page_provenance.PageProvenance()
    provenance.closed_while_counting("s1")

    async def keep_closing() -> None:
        while True:
            await asyncio.sleep(TEST_GRACE_SECONDS / 3)
            provenance.closed_while_counting("s1")

    closer = asyncio.create_task(keep_closing())
    started = time.monotonic()
    try:
        await asyncio.wait_for(provenance.after_the_closed_page_grace("s1"), TEST_GRACE_SECONDS * 5)
    finally:
        closer.cancel()
    waited = time.monotonic() - started

    assert TEST_GRACE_SECONDS * 0.9 <= waited < TEST_GRACE_SECONDS * 2


async def test_no_action_waits_out_the_grace_while_a_page_that_counts_is_still_open(
    tmp_path: Path, monkeypatch
) -> None:
    """Item 49: while a refused tab is open, the tab's commits are judged as its choice whether the action waits
    or not, and the advice is to close it first; the wait only held the session lock for nothing."""
    manager = manager_for(tmp_path)
    session, tab, _still_open = await session_with_a_refused_tab(manager, tmp_path, monkeypatch)
    another = EventPage(REFUSED)
    session.context.pages.append(another)
    manager._attach_page_listeners(another, session)
    await drain()
    closed(session, another)
    closed_at = manager.page_provenance._counting_closed_at[session.id]

    with contextlib.suppress(BrowserActionError):
        await manager.navigate(session.id, NEXT)
    await drain()

    assert tab.goto_at - closed_at < TEST_GRACE_SECONDS / 2


async def test_the_agents_click_right_after_a_refused_tab_closed_waits_out_the_grace(
    tmp_path: Path, monkeypatch
) -> None:
    """Item 50 (Fable E-NAV-2/3): only navigate and open_tab waited, so a click on a clean tab's link within
    the grace after the refused tab closed had the new page refused and blanked; every action now waits, in
    the pipeline, before its snapshot and before its effect is marked."""
    manager = manager_for(tmp_path)
    session, tab, closed_at = await refused_tab_just_closed(manager, tmp_path, monkeypatch)
    clicked_at: list[float] = []

    async def click() -> None:  # a link on the tab's own page
        clicked_at.append(time.monotonic())
        tab.fire("request", NavigationRequest(tab, NEXT))
        tab.commit(manager, NEXT)

    await manager._run_action(session, "click", {"selector": "a"}, click)
    await drain()

    assert not manager._page_refused(tab)
    assert tab.url == NEXT
    assert clicked_at[0] - closed_at >= TEST_GRACE_SECONDS


async def test_an_approved_navigate_cancelled_while_it_waits_out_the_grace_gives_its_approval_back(
    tmp_path: Path, monkeypatch
) -> None:
    """Item 40 (Codex E 4): the wait ran inside the operation, after the pipeline marked the effect started, so
    a cancellation while it waited left the approval spent with nothing loaded."""
    manager = manager_for(tmp_path)
    session, tab, refused = await session_with_a_refused_tab(manager, tmp_path, monkeypatch)
    decision = BrowserActionDecision(action="navigate", reason="pay", url=NEXT, risk_category="payment")
    with pytest.raises(ApprovalRequiredError) as asked:
        await manager.execute_decision(session.id, decision)
    approval_id = asked.value.approval.id
    await manager.approve(approval_id, comment="ok")
    closed(session, refused)

    run = asyncio.create_task(manager.execute_decision(session.id, decision, approval_id=approval_id))
    await asyncio.sleep(TEST_GRACE_SECONDS / 3)  # inside the wait
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    approval = await manager.approvals.get(approval_id)
    assert (tab.goto_at, approval.status, approval.executed_at) == (None, "approved", None)
    given_back = [
        call for call in manager.audit.append.await_args_list if call.kwargs.get("event_type") == "approval_given_back"
    ]
    assert [call.kwargs["approval_id"] for call in given_back] == [approval_id]


async def test_the_agents_navigate_right_after_a_refused_tab_closed_waits_out_the_grace(
    tmp_path: Path, monkeypatch
) -> None:
    """Item 30 (Opus C): the refusal's advice is to close the refused tab and navigate this one. Done at once,
    the new page's own commit while it loaded (a #fragment, replaceState, a client redirect) came within
    CLOSED_PAGE_GRACE_SECONDS of the close and counted as another page's choice: the tab was blanked and stayed
    refused. The agent's navigate now starts once the grace has passed."""
    manager = manager_for(tmp_path)
    session, tab, closed_at = await refused_tab_just_closed(manager, tmp_path, monkeypatch)

    await manager.navigate(session.id, NEXT)
    await drain()

    assert not manager._page_refused(tab)
    assert tab.url == f"{NEXT}#top"
    assert tab.goto_at - closed_at >= TEST_GRACE_SECONDS


async def test_a_tab_the_agent_opens_right_after_a_refused_tab_closed_waits_out_the_grace(
    tmp_path: Path, monkeypatch
) -> None:
    """Item 30, open_tab with a URL: its new page's own commit while it loads is judged the same way."""
    manager = manager_for(tmp_path)
    session, _tab, closed_at = await refused_tab_just_closed(manager, tmp_path, monkeypatch)
    opened: list[LoadingPage] = []

    async def new_page() -> LoadingPage:
        page = LoadingPage(manager, "about:blank")
        session.context.pages.append(page)
        opened.append(page)
        return page

    session.context.new_page = new_page
    manager._persist_session = AsyncMock()
    manager._session_summary = AsyncMock(return_value={})
    manager.tabs.summaries = AsyncMock(return_value=[])

    await manager.open_tab(session.id, NEXT, activate=False)
    await drain()

    assert not manager._page_refused(opened[0])
    assert opened[0].goto_at - closed_at >= TEST_GRACE_SECONDS


async def test_a_blank_a_refused_tab_navigated_just_before_it_closed_is_refused(tmp_path: Path) -> None:
    """PROV-1: a blank, blob: or data: navigation has no request event to mark, and the refused tab that sent
    the opener there (and writes into it) can close first. Within the grace its commit is a refused one."""
    manager = manager_for(tmp_path)
    session, opener, refused = await opener_and_refused_tab(manager)
    closed(session, refused)

    opener.commit(manager, "about:blank")

    assert manager._page_refused(opener)
    assert manager.page_provenance.counts_for_its_session(opener)
    await drain()


async def test_control_a_refused_tab_closed_long_before_no_longer_refuses_the_others(
    tmp_path: Path, monkeypatch
) -> None:
    manager = manager_for(tmp_path)
    session, opener, refused = await opener_and_refused_tab(manager)
    closed(session, refused)
    await past_the_grace(monkeypatch)

    opener.fire("request", NavigationRequest(opener, f"{ALLOWED}/next"))
    opener.commit(manager, f"{ALLOWED}/next")
    await drain()

    assert not manager._page_refused(opener)
    assert opener.url == f"{ALLOWED}/next"


# ── What clears a tab's refusal while another tab is refused (final review, PROV-2, PROV-4, PROV-6) ──


async def test_a_commit_a_refused_tab_made_after_the_agents_navigate_committed_keeps_the_tab_refused(
    tmp_path: Path,
) -> None:
    """PROV-2: the refused tab sent its opener to a URL it chose right after the agent's navigate committed,
    before the goto returned. The goto's response was the agent's URL's and the page was on an allowed URL, so
    the navigate cleared the tab, which was then read on the URL the refused tab chose."""
    manager = manager_for(tmp_path)
    _session, opener, _popup = opener_and_refused_popup(manager)

    with manager.page_provenance.controller_navigation(opener) as navigation:
        opener.commit(manager, ALLOWED)  # the agent's navigate
        opener.commit(manager, CHOSEN)  # the refused tab's, before the goto returned
        navigation.response = goto_response(ALLOWED)
    await drain()

    assert manager._page_refused(opener)
    assert manager._shown_url(opener) == url_origin(CHOSEN)


async def test_control_the_agents_navigate_clears_a_tab_whose_new_page_moved_itself_while_it_loaded(
    tmp_path: Path,
) -> None:
    """The tab was refused for its own URL, so it counted for its session while the goto ran; the second commit
    is the new page's own (a client redirect, a history.replaceState), and no other tab was refused."""
    manager = manager_for(tmp_path)
    tab = EventPage(ALLOWED)
    tabs_session(manager, tab)
    tab.commit(manager, REFUSED)
    await drain()  # the guard's blank

    with manager.page_provenance.controller_navigation(tab) as navigation:
        tab.commit(manager, ALLOWED)
        tab.commit(manager, f"{ALLOWED}/home")
        navigation.response = goto_response(ALLOWED)
    await drain()

    assert not manager._page_refused(tab)
    assert tab.url == f"{ALLOWED}/home"


async def test_the_escape_back_does_not_clear_a_tab_while_another_tab_is_refused(tmp_path: Path) -> None:
    """PROV-6: a blocked action's go_back landed on the URL the agent read before it, while a refused tab that
    holds this one was open. The refused tab can win that race with its own navigation to the same URL (a POST
    through a named target, say), so the commit can be its choice; the escape cleared the tab by URL alone."""
    manager = manager_for(tmp_path)
    _session, opener, _popup = opener_and_refused_popup(manager)
    opener.commit(manager, REFUSED)  # where the blocked action took it

    with manager.page_provenance.escape(opener, ALLOWED):
        opener.commit(manager, ALLOWED)
    await drain()

    assert manager._page_refused(opener)


async def test_control_the_escape_back_clears_a_tab_only_its_own_action_made_refused(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    tab = EventPage(ALLOWED)
    tabs_session(manager, tab)
    tab.commit(manager, REFUSED)  # where the blocked action took it

    with manager.page_provenance.escape(tab, ALLOWED):
        tab.commit(manager, ALLOWED)
    await drain()

    assert not manager._page_refused(tab)


class SupersededBlankPage(EventPage):
    """A tab whose guard's blank loses to the refused tab's own about:blank navigation of it: Playwright fails
    the guard's goto once the other navigation has committed."""

    async def goto(self, url: str, **_kwargs) -> None:
        self.commit(None, "about:blank")  # the refused tab's blank, which it writes into
        raise PlaywrightError("Navigation to about:blank is interrupted by another navigation to about:blank")


async def test_a_refused_tabs_blank_that_beat_the_guards_blank_still_refuses_the_others(
    tmp_path: Path, monkeypatch
) -> None:
    """PROV-4: any about:blank commit while the guard's blank was pending counted as the guard's, so a blank the
    refused tab committed and wrote into was taken for the controller's (opaque-origin) blank. Once the refused
    tab closed, it no longer counted, and a third tab its script sent to a URL it chose was read with that URL."""
    manager = manager_for(tmp_path)
    written, third = SupersededBlankPage(ALLOWED), EventPage(ALLOWED)
    session = tabs_session(manager, written, third)
    refused = EventPage(REFUSED)
    session.context.pages.append(refused)
    manager._attach_page_listeners(refused, session)
    manager.sessions.pop(session.id)  # the guard this commit starts finds no session: the test runs it below
    written.commit(manager, CHOSEN)  # the refused tab sends it there: refused, so the guard blanks it
    await drain()
    manager.sessions[session.id] = session
    with pytest.raises(PlaywrightError):
        await manager.diagnostics._return_to_blank(session, written)
    assert written.url == "about:blank"
    closed(session, refused)
    await past_the_grace(monkeypatch)

    third.commit(manager, SECRET_URL)  # the script written into the blank sends the third tab there

    assert manager._page_refused(third)
    assert manager._shown_url(third) == url_origin(SECRET_URL)
    await drain()


# ── A download a refused redirect chose (item 8, CR-2) ──

# The allowed URL a refused host's redirect sent the page to, which answers as an attachment.
CHOSEN_FILE = "https://allowed.example/report.csv?d=REFUSED-PAGE-TEXT"


def stub_download_capture(manager) -> SimpleNamespace:
    capture = SimpleNamespace(
        capture=AsyncMock(
            return_value={"status": "completed", "filename": "r.csv", "url": "/a/r.csv", "failure": None}
        ),
        block=AsyncMock(return_value={"source_url": "https://allowed.example"}),
    )
    manager.diagnostics.download_capture = capture
    manager.audit.append = AsyncMock()
    return capture


@pytest.mark.parametrize(
    ("through_refused_host", "failure_first", "captured"),
    [(True, True, False), (True, False, False), (False, True, True)],
    ids=["refused-hop-failure-first", "refused-hop-download-first", "control-allowed-redirect"],
)
async def test_a_download_reached_through_a_refused_redirect_is_refused_whatever_the_event_order(
    tmp_path: Path, through_refused_host: bool, failure_first: bool, captured: bool
) -> None:
    """The navigation that turns into a download fails without a commit, and that failure cleared the
    refused hop: delivered before the download event, the download's page was not refused, its allowed
    final URL passed, and its source_url carried the refused host's query whole."""
    manager = manager_for(tmp_path)
    page = listened_page(manager)
    first = NavigationRequest(page, REFUSED if through_refused_host else f"{ALLOWED}/go")
    landing = NavigationRequest(page, CHOSEN_FILE, redirected_from=first)
    page.fire("request", first)
    page.fire("request", landing)
    capture = stub_download_capture(manager)
    download = SimpleNamespace(url=CHOSEN_FILE, suggested_filename="report.csv")

    events = [("requestfailed", landing), ("download", download)]
    for event, argument in events if failure_first else reversed(events):
        page.fire(event, argument)
    await drain()

    assert (capture.capture.await_count, capture.block.await_count) == (int(captured), int(not captured))


async def test_a_download_after_the_agent_navigated_the_page_is_not_refused_for_an_earlier_refused_redirect(
    tmp_path: Path,
) -> None:
    """Round 4d (R4C-NET-1): the record of a failed navigation through a refused host outlived the taint it
    stands for, so a later download from the same URL, with no refused redirect, was cancelled."""
    manager = manager_for(tmp_path)
    page = listened_page(manager)
    first = NavigationRequest(page, REFUSED)
    landing = NavigationRequest(page, CHOSEN_FILE, redirected_from=first)
    for event, request in (("request", first), ("request", landing), ("requestfailed", landing)):
        page.fire(event, request)
    await drain()
    assert manager.page_provenance.download_refused(page, CHOSEN_FILE)
    with manager.page_provenance.controller_navigation(page) as navigation:
        page.commit(manager, ALLOWED)
        navigation.response = goto_response(ALLOWED)
    capture = stub_download_capture(manager)

    page.fire("download", SimpleNamespace(url=CHOSEN_FILE, suggested_filename="report.csv"))
    await drain()

    assert (capture.capture.await_count, capture.block.await_count) == (1, 0)


# ── A download of a popup the context never reported (round 4e, item 2) ──


def capturing_session(manager, page: EventPage) -> SimpleNamespace:
    """A session on `page`, with the real download capture service and its file writes faked."""
    session = popup_session(page)
    session.downloads = []
    session.artifact_dir = Path("virtual-artifacts")
    capture = DownloadCaptureService(SimpleNamespace(append_jsonl=AsyncMock()))
    capture._reserve = lambda folder, name: folder / name
    manager.diagnostics.download_capture = capture
    manager.audit.append = AsyncMock()
    manager._attach_page_listeners(page, session)
    return session


def file_download(saved: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        url=CHOSEN_FILE,
        suggested_filename="report.csv",
        save_as=AsyncMock(side_effect=saved.append),
        failure=AsyncMock(return_value=None),
        cancel=AsyncMock(),
    )


@pytest.mark.parametrize(
    ("through_refused_host", "failure_first", "saved_whole"),
    [(True, True, False), (True, False, False), (False, True, True)],
    ids=["refused-hop-failure-first", "refused-hop-download-first", "control-allowed-redirect"],
)
async def test_a_download_an_unreported_popups_refused_redirect_chose_is_refused_whatever_the_event_order(
    tmp_path: Path, through_refused_host: bool, failure_first: bool, saved_whole: bool
) -> None:
    """Round 4e (Codex 2): an allowed opener's popup went through a refused redirect onto an allowed URL
    that answered as an attachment. Playwright reports the download of a popup it has not reported on the
    popup's opener, and only the context saw the popup's navigation, whose failure dropped the kept hop and
    remembered nothing: the allowed opener's download was saved, its source_url whole."""
    manager = manager_for(tmp_path)
    opener = EventPage(ALLOWED)
    session = capturing_session(manager, opener)
    first = UnframedRequest(REFUSED if through_refused_host else f"{ALLOWED}/go")
    landing = UnframedRequest(CHOSEN_FILE, redirected_from=first)
    for request in (first, landing):  # the context's request event for each hop (coreBundle.js requestStarted)
        manager.diagnostics.on_context_request(session, request)
    saved: list[str] = []
    download = file_download(saved)

    events = [
        lambda: manager.diagnostics.on_context_request_failed(session, landing),
        lambda: opener.fire("download", download),
    ]
    for deliver in events if failure_first else reversed(events):
        deliver()
    await drain()

    [kept] = session.downloads
    if saved_whole:
        assert (kept["status"], kept["source_url"], len(saved)) == ("completed", CHOSEN_FILE, 1)
    else:
        assert (kept["status"], kept["source_url"], saved) == ("blocked", url_origin(CHOSEN_FILE), [])


async def test_control_a_refused_redirect_of_a_reported_page_is_remembered_by_that_page_alone(tmp_path: Path) -> None:
    """The context sees a reported page's requests too. Remembered for the whole session, such a page's
    failed navigation through a refused host outlived the agent's navigate of that page, which clears the
    page's own record (round 4d, R4C-NET-1), and a later download from the same URL was refused."""
    manager = manager_for(tmp_path)
    page = EventPage(ALLOWED)
    session = capturing_session(manager, page)
    first = NavigationRequest(page, REFUSED)
    landing = NavigationRequest(page, CHOSEN_FILE, redirected_from=first)
    for request in (first, landing):
        page.fire("request", request)
        manager.diagnostics.on_context_request(session, request)
    page.fire("requestfailed", landing)
    manager.diagnostics.on_context_request_failed(session, landing)
    with manager.page_provenance.controller_navigation(page) as navigation:
        page.commit(manager, ALLOWED)
        navigation.response = goto_response(ALLOWED)
    saved: list[str] = []

    page.fire("download", file_download(saved))
    await drain()

    assert ([kept["status"] for kept in session.downloads], len(saved)) == (["completed"], 1)

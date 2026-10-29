"""What a refused page reaches beyond itself (round 4c): another tab it navigates, requests from a page
the browser context has not reported yet, and a download its redirect chose.

Fakes deliver Playwright's events in the order each case needs, so every run sees the same order.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.downloads import DownloadCaptureService
from app.network_inspector import url_origin
from tests.test_allowed_hosts_provenance import NavigationRequest, UnframedRequest, listened_page
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


async def test_a_tab_refused_only_by_another_tab_does_not_keep_the_others_refused(tmp_path: Path) -> None:
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
    tmp_path: Path, made_there: str
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

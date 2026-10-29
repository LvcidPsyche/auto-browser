"""Which pages ALLOWED_HOSTS refuses for where they came from, not only for where they are.

Judging a page by its URL alone let a refused page reach the agent through pages whose URLs
pass: a window it opened with ``window.open()`` is on about:blank and scriptable by it, and
whatever that window logs, fetches or downloads passed every URL check. So a page is also
refused while it is tainted:

- it committed a refused top-level URL, or
- it committed a top-level URL on its own while another page of its session could run a refused
  host's code (counts_for_its_session), which can have sent it there (a popup setting
  window.opener.location) with what it read in the URL, or
- it is a popup whose opener was refused or tainted when the popup opened, whatever the popup's
  URL, and a page the context reports is refused until its opener has judged it; one that gets
  no verdict (its opener closed first) and that the controller did not open stays refused.

The taint clears only when a navigation the controller started (the agent's navigate, a tab it
opens, the start URL, the shadow page) ends on the list, judged by the response its goto
returned: every hop of the response's redirect chain allowed, the page on an allowed http(s)
URL, no commit tainted by a refused hop while it ran, and no refused hop in flight; or when the
controller's go_back off a page a blocked action left lands back on the page the agent read
before the action (escape). The agent's own go_back and reload do not clear it. A commit the page
made itself does not clear it, since the refused page chose that URL and can put what it read
in it; nor does a same-document one, which keeps the document the refused page wrote; nor does
the controller's own blank: a refused opener can navigate the window to about:blank again and
write into it. A top-level request to a refused URL marks the page as navigating through a
refused hop: until the commit that follows (tainted whatever its URL, as a refused host's
redirect chose it) or that request's own navigation fails, the page is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag, urlsplit

HTTP_SCHEMES = frozenset({"http", "https"})
# Final URLs of a page's failed top-level navigations that passed a refused URL, kept for the
# download such a navigation turns into: the last few only, as a download follows its navigation.
REFUSED_FAILURES_KEPT = 8
# How long a page that counted for its session (counts_for_its_session) still counts after it closed. A
# refused popup can set window.opener.location and then call window.close(): its close can be delivered
# before the opener's navigation request, or before a blank it wrote commits (which has no request). This
# covers the gap between the refused page's last script and those events, not the navigation's response.
CLOSED_PAGE_GRACE_SECONDS = 2.0


@dataclass
class _PageState:
    page: Any
    tainted: bool = False
    opener_pending: bool = False
    # Blanks the controller started on this page whose commit has not been seen yet.
    own_blanks: int = 0
    # The page's current document is the controller's own blank.
    blanked: bool = False
    # Refused commits seen, so a read can tell the page moved while it was read (PageRead).
    refused_commits: int = 0
    # The top-level request to a refused URL whose navigation has not committed or failed yet.
    refused_hop: Any = None
    # Refused hops marked, and commits a refused hop tainted: a go_back off a blocked action during
    # which either count changed does not clear the taint (escape), nor does a controller navigation
    # during which a refused hop committed (controller_navigation).
    hops_marked: int = 0
    hop_commits: int = 0
    # Tainted only because another page of the session was refused when it committed an http(s) URL: its
    # refusal says so, since closing that page is how the agent stops it happening again. Such a page
    # counts for its session only while it is on another kind of document (counts_for_its_session).
    by_another_page: bool = False
    # Final URLs of failed top-level navigations whose redirect chain passed a refused URL
    # (refused_navigation_failed): a download from one of them is refused.
    refused_failures: deque[str] = field(default_factory=lambda: deque(maxlen=REFUSED_FAILURES_KEPT))
    # A top-level navigation request that started while the session counted as refused, until the next commit
    # or that navigation's failure: its commit is judged as if the refused page were still open, which it may
    # not be by the time the commit is delivered (navigation_started).
    started_while_refused: Any = None
    # During a controller navigation or escape: each commit's URL, and whether it may be another page's choice
    # (it was refused for its own URL or a refused hop, or another page of the session counted when it was made
    # or its navigation started). None otherwise.
    recorded_commits: list[tuple[str, bool]] | None = None


@dataclass
class ControllerNavigation:
    """A navigation the controller started: the caller sets ``response`` to what its goto returned."""

    response: Any = None


class PageProvenance:
    """Taint for the pages of every session, kept until the page closes."""

    def __init__(self, url_refused: Callable[[str], bool] | None = None) -> None:
        # By id(): Playwright pages are hashable, but the fakes tests use are not all.
        # The state holds the page, so the id is not reused while it is kept.
        self._states: dict[int, _PageState] = {}
        # Whether ALLOWED_HOSTS refuses a page at a URL (manager._url_off_list). Without it (the
        # fakes tests build) no controller navigation is judged to end on the list.
        self._url_refused = url_refused
        # Session id -> when a page of that session that counted for it closed (closed_while_counting).
        self._counting_closed_at: dict[str, float] = {}

    def _state(self, page: Any) -> _PageState | None:
        state = self._states.get(id(page))
        return state if state is not None and state.page is page else None

    def _ensure(self, page: Any) -> _PageState:
        state = self._state(page)
        if state is None:
            state = self._states[id(page)] = _PageState(page)
        return state

    def refused(self, page: Any) -> bool:
        """Whether the page is tainted, or its opener has not been judged yet."""
        state = self._state(page)
        return state is not None and (state.tainted or state.opener_pending or state.refused_hop is not None)

    def refused_by_another_page(self, page: Any) -> bool:
        """Whether the page is refused only for a commit made while another page of its session was refused."""
        state = self._state(page)
        return (
            state is not None
            and state.tainted
            and state.by_another_page
            and not state.opener_pending
            and state.refused_hop is None
        )

    def counts_for_its_session(self, page: Any) -> bool:
        """Whether the page can run a refused host's code, so another page's own commit while it is open is
        refused (by_another_page): it is tainted by a refused commit, a refused opener or a refused hop,
        or has a refused hop in flight. A page refused only because another page was, or only waiting for
        its opener's verdict (which arrives right after the page is reported unless the opener has closed
        first), counts only while it is on a blank, blob: or data: document other than the controller's
        own blank: such a document has the origin of the page that made it, which can be a refused one.
        On an http(s) URL its document came from that URL's origin, so what its scripts do is an allowed
        host's own, and the controller's blank has an opaque origin no other page shares. Counting those
        kept the other pages refused after the refused page had closed, each refusing the next, or for the
        session's life. A page's URL off the list counts too, and is the caller's to check
        (manager._session_has_refused_page).
        """
        state = self._state(page)
        if state is None:
            return False
        if (state.tainted and not state.by_another_page) or state.refused_hop is not None:
            return True
        if not (state.tainted or state.opener_pending):
            return False
        return not (state.blanked or is_http(page.url))

    def refused_commits(self, page: Any) -> int:
        """How many refused commits the page has made (a refused URL, a tainted page's document)."""
        state = self._state(page)
        return state.refused_commits if state is not None else 0

    def needs_blank(self, page: Any) -> bool:
        """Whether the guard must send a tainted page to about:blank (it is not on the controller's blank)."""
        state = self._state(page)
        return state is not None and state.tainted and not state.blanked

    def committed(
        self,
        page: Any,
        url: str,
        *,
        url_refused: bool,
        by_another_page: bool = False,
        another_page_counted: bool = False,
    ) -> bool:
        """Record a top-level commit. True when it is a refused one, which the guard sends back to about:blank.

        ``by_another_page``: the commit is refused only because another page of the session was
        refused when it was made. A tainted page's commit stays refused even during the controller's
        own navigation: that navigation clears the taint when it ends (controller_navigation), so the
        guard it starts here finds nothing to do by the time it has the session lock.
        ``another_page_counted``: during a controller navigation or escape, a page of the session other
        than this one counted for it (counts_for_its_session) when the commit was made or its navigation started.
        """
        state = self._ensure(page) if url_refused or by_another_page else self._state(page)
        if state is None:
            return False
        if state.own_blanks and url == "about:blank":
            state.own_blanks -= 1
            state.blanked = True
            return False
        state.blanked = False
        state.started_while_refused = None
        refused_hop, state.refused_hop = state.refused_hop, None
        if refused_hop is not None:
            state.hop_commits += 1
        if state.recorded_commits is not None:
            chosen = url_refused or refused_hop is not None or another_page_counted
            state.recorded_commits.append((url, chosen))
        if url_refused or refused_hop is not None:
            state.tainted = True
            state.by_another_page = False
        elif by_another_page and not state.tainted:
            state.tainted = True
            state.by_another_page = True
        if state.tainted:
            state.refused_commits += 1
        return state.tainted

    def opener_lookup_started(self, page: Any) -> None:
        """A page the context reports: refused until its opener judges it (opener_judged)."""
        self._ensure(page).opener_pending = True

    def opened_by_controller(self, page: Any) -> None:
        """A page the controller opened itself (context.new_page()): it has no opener to judge it."""
        state = self._state(page)
        if state is not None:
            state.opener_pending = False

    def opener_judged(self, page: Any, *, opener_refused: bool) -> bool:
        """Record the opener's verdict. True when it taints the page, which then goes back to about:blank.

        A refused opener taints the popup whatever its URL: on about:, blob: or data: the popup shows
        what the opener wrote, and on an allowed URL the opener chose its path and query, which can
        carry what it read.
        """
        state = self._ensure(page)
        state.opener_pending = False
        if not opener_refused:
            return False
        state.tainted = True
        state.by_another_page = False
        state.blanked = False
        state.refused_commits += 1
        return True

    @contextlib.contextmanager
    def controller_navigation(self, page: Any) -> Iterator[ControllerNavigation]:
        """Around a navigation the controller starts (page.goto under the session lock).

        On the way out the page's taint, and a missing opener verdict, clear when the navigation
        ended on the list (_ended_on_list), no commit was tainted by a refused hop while it ran (a
        navigation through a refused host that the page started earlier can still commit during the
        goto), no refused hop is still in flight, and no commit after the navigation's own may be another
        page's choice (_chosen_after_its_own): a refused tab that holds this one can navigate it again
        between the goto's commit and its return. A refused hop that started and failed while it
        ran (a 204 ping) does not keep the taint: it brought no document from the refused host. Not
        when the goto raised.

        Its start also drops a blank of the controller's (blank_started) still counted though its
        commit never came (its goto was cancelled, say): kept, the count exempted the page's own next
        about:blank commit.
        """
        state = self._ensure(page)
        state.own_blanks = 0
        hop_commits = state.hop_commits
        state.recorded_commits = commits = []
        navigation = ControllerNavigation()
        try:
            yield navigation
        finally:
            state.recorded_commits = None
        if (
            state.hop_commits == hop_commits
            and state.refused_hop is None
            and self._ended_on_list(page, navigation.response)
            and not _chosen_after_its_own(commits, navigation.response.url)
        ):
            _clear(state)

    @contextlib.contextmanager
    def escape(self, page: Any, url: str | None) -> Iterator[None]:
        """Around the controller's go_back off a page a blocked action left (actions/pipeline.py).

        The taint clears when the page is back on ``url``, the URL of the page the agent read before
        the action, with no refused hop marked, nor a commit tainted by one, on the way back, and no commit
        on the way that may be another page's choice: a refused tab that holds this one can win the race
        with its own navigation to that same URL, which the URL alone does not tell apart. None
        (the page was refused, or moved while it was read, before the action) clears nothing: what is
        recorded for a refused page is its origin or the controller's blank, which a go_back can reach
        with what the refused page wrote still there.
        """
        state = self._ensure(page)
        hops = _hop_count(state)
        state.recorded_commits = commits = []
        try:
            yield
        finally:
            state.recorded_commits = None
        if (
            url is not None
            and page.url == url
            and _hop_count(state) == hops
            and not any(chosen for _url, chosen in commits)
        ):
            _clear(state)

    def _ended_on_list(self, page: Any, response: Any) -> bool:
        """Whether a goto returned a response every hop of whose redirect chain is allowed, and the page is on the list.

        None (a same-document navigation, which keeps the document) is not.
        """
        if response is None or self._url_refused is None:
            return False
        if any(self._url_refused(request.url) for request in redirect_chain(response.request)):
            return False
        return not self._url_refused(page.url) and is_http(page.url)

    def refused_hop_started(self, page: Any, request: Any) -> None:
        """A top-level request of this page to a refused URL: refused until its navigation ends."""
        state = self._ensure(page)
        state.refused_hop = request
        state.hops_marked += 1

    def navigation_failed(self, page: Any, request: Any) -> None:
        """A top-level navigation request of the page failed or was aborted without a commit.

        It clears the refused hop only when it is the hop's own request or a later one of its redirect
        chain: another navigation's failure (one the hop superseded, say) says nothing about where the
        page goes next.
        """
        state = self._state(page)
        if state is None:
            return
        if any(hop is state.refused_hop for hop in redirect_chain(request)):
            state.refused_hop = None
        if state.started_while_refused is not None and any(
            hop is state.started_while_refused for hop in redirect_chain(request)
        ):
            state.started_while_refused = None

    def refused_navigation_failed(self, page: Any, url: str) -> None:
        """A top-level navigation whose redirect chain passed a refused URL failed at ``url`` without a commit.

        A navigation that turns into a download fails so, and its failure clears the refused hop
        (navigation_failed), which can be delivered before the download event: so the download
        is judged by this record too (download_refused).
        """
        self._ensure(page).refused_failures.append(url)

    def download_refused(self, page: Any, url: str) -> bool:
        """Whether a download at ``url`` comes from a failed navigation of the page that passed a refused URL."""
        state = self._state(page)
        return state is not None and url in state.refused_failures

    def blank_started(self, page: Any) -> None:
        self._ensure(page).own_blanks += 1

    def blank_failed(self, page: Any) -> None:
        """The controller's blank failed (or was cancelled) on the page.

        When its count was already used up, the about:blank commit taken for it was another navigation's
        (a refused page's own, which superseded the goto and can write into its document): that document is
        not the controller's blank. A goto that fails after its own commit is taken the same way: the safe
        direction, as the page then counts for its session (counts_for_its_session) until it moves or closes.
        """
        state = self._state(page)
        if state is None:
            return
        if state.own_blanks:
            state.own_blanks -= 1
        else:
            state.blanked = False

    def navigation_started(self, page: Any, request: Any, *, session_refused: bool) -> None:
        """A top-level navigation request of the page started: marked when the session counted as refused then.

        A refused popup can send its opener somewhere and close before the opener's commit is delivered, and the
        agent can close a refused tab while another tab's navigation is in flight: the commit is judged by the
        mark too (started_while_refused). A later navigation that starts while nothing counts leaves the mark,
        so a stale one can refuse a later clean commit of the page: the safe direction.
        """
        if session_refused:
            self._ensure(page).started_while_refused = request

    def recording_commits(self, page: Any) -> bool:
        """Whether the page is in a controller navigation or escape, whose commits are recorded (committed())."""
        state = self._state(page)
        return state is not None and state.recorded_commits is not None

    def started_while_refused(self, page: Any) -> bool:
        """Whether the page's navigation now in flight started while the session counted as refused."""
        state = self._state(page)
        return state is not None and state.started_while_refused is not None

    def closed_while_counting(self, session_key: str) -> None:
        """A page that counted for its session closed: the session counts as refused a little longer."""
        self._counting_closed_at[session_key] = time.monotonic()

    def counting_page_closed_recently(self, session_key: str) -> bool:
        """Whether a page that counted for the session closed less than CLOSED_PAGE_GRACE_SECONDS ago."""
        closed_at = self._counting_closed_at.get(session_key)
        if closed_at is None:
            return False
        if time.monotonic() - closed_at < CLOSED_PAGE_GRACE_SECONDS:
            return True
        del self._counting_closed_at[session_key]  # past it: one entry per session is kept no longer
        return False

    async def after_the_closed_page_grace(self, session_key: str) -> None:
        """Return once no page that counted for the session closed less than CLOSED_PAGE_GRACE_SECONDS ago,
        or once CLOSED_PAGE_GRACE_SECONDS have passed since the call, whichever comes first.

        For the agent's own action on a tab (manager._wait_out_closed_page_grace): the refusal's advice is to
        close the refused tab and act, and an action started at once had the page's own commits counted as
        another page's choice. Bounded from the call: each later close restarted the wait, so a refused tab
        closing popups faster than the grace held the session lock for good. Past the bound the action goes
        on, and a close's grace still running then refuses its commits, the safe side. A loop, as a timer can
        fire a little early.
        """
        deadline = time.monotonic() + CLOSED_PAGE_GRACE_SECONDS
        while self.counting_page_closed_recently(session_key):
            now = time.monotonic()
            if now >= deadline:
                return
            closed_at = self._counting_closed_at[session_key]
            await asyncio.sleep(min(deadline - now, max(0.0, CLOSED_PAGE_GRACE_SECONDS - (now - closed_at))))

    def forget(self, page: Any) -> None:
        state = self._state(page)
        if state is not None:
            del self._states[id(page)]


def is_http(url: str) -> bool:
    """Whether a page at ``url`` holds a document from that URL's own origin; a blank, blob: or data: one
    has the origin of the page that made it."""
    return urlsplit(url).scheme.lower() in HTTP_SCHEMES


def _chosen_after_its_own(commits: list[tuple[str, bool]], url: str) -> bool:
    """Whether a recorded commit that may be another page's choice came after the navigation's own commit.

    Its own is the first at its response's URL (fragment aside); the commits before it were replaced by it.
    With none at that URL, whether any recorded commit may be. The page's own taint does not count: once
    the navigation's own commit is in, its document is the agent's URL's, and a second commit of a page
    whose document is from an allowed origin (a client redirect, a replaceState) is that origin's own.
    """
    own = urldefrag(url).url
    for index, (committed_url, _chosen) in enumerate(commits):
        if urldefrag(committed_url).url == own:
            return any(chosen for _url, chosen in commits[index + 1 :])
    return any(chosen for _url, chosen in commits)


def redirect_chain(request: Any) -> Iterator[Any]:
    """A request, then each request it was redirected from, back to the first of its navigation."""
    while request is not None:
        yield request
        request = request.redirected_from


def _hop_count(state: _PageState) -> tuple[int, int]:
    return state.hops_marked, state.hop_commits


def _clear(state: _PageState) -> None:
    """The page holds nothing a refused page put there: its taint, a missing opener verdict, and the
    record of its failed navigations through a refused host (a download is judged by it) go."""
    state.tainted = False
    state.by_another_page = False
    state.opener_pending = False
    state.refused_failures.clear()

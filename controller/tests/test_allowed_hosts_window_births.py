"""The window-birth watcher judges a page target at its CDP birth (app/browser/window_births.py).

These drive the watcher with hand-made CDP events, so every run sees the same order: a page target born
with an opener is judged by that opener (refused, a born-refused window itself, or unmatched and so by the
session); one with no opener (the controller's own new_page) never is; iframes and workers make no entry; a
born-refused window Playwright never reports is closed once nothing justifies it, and one it reports never is;
state is one entry per live target, dropped on targetDestroyed; and every path that cannot classify a window
fails closed (the attach, the context id, the targetId read, a birth that never came, a lost watcher).

No real browser: the fakes stand in for the browser-level CDP session and the targetId lookup.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.browser import window_births as wb
from app.browser.window_births import (
    BirthVerdict,
    WindowBirths,
    WindowBirthWatcher,
    WindowBirthWatcherUnavailable,
)

CTX = "CTX-1"
OTHER_CTX = "CTX-2"


class FakeCdp:
    """A browser-level CDP session: records handlers and answers the sends the watcher makes."""

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.sent: list[tuple[str, Any]] = []
        self.closed_targets: list[str] = []
        self.detached = False
        self.fail_close = False
        self.fail_discover = False

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params))
        if method == "Target.setDiscoverTargets" and self.fail_discover:
            raise RuntimeError("discover failed")
        if method == "Target.closeTarget":
            if self.fail_close:
                raise RuntimeError("no such target")
            self.closed_targets.append(params["targetId"])
            return {"success": True}
        return {}

    async def detach(self) -> None:
        self.detached = True

    def fire(self, event: str, payload: dict) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(payload)


class FakeBrowser:
    def __init__(self, cdp: FakeCdp | None = None) -> None:
        self.cdps: list[FakeCdp] = [cdp] if cdp is not None else []
        self.fail_attach = False
        self.fail_discover = False

    async def new_browser_cdp_session(self) -> FakeCdp:
        if self.fail_attach:
            raise RuntimeError("no browser target")
        if len(self.cdps) == 1 and not self.cdps[0].handlers:
            return self.cdps[0]
        cdp = FakeCdp()
        cdp.fail_discover = self.fail_discover
        self.cdps.append(cdp)
        return cdp

    @property
    def cdp(self) -> FakeCdp:
        return self.cdps[-1]


class FakeSession:
    def __init__(self, browser: FakeBrowser, session_id: str = "s1") -> None:
        self.browser = browser
        self.id = session_id


class FakePage:
    """A stand-in for a Playwright Page; unlike object(), it can be weakly referenced."""

    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed


def created(target_id: str, context_id: str | None, *, opener_id: str | None = None, type: str = "page") -> dict:
    return {
        "targetInfo": {
            "targetId": target_id,
            "type": type,
            "browserContextId": context_id,
            "openerId": opener_id,
        }
    }


class Harness:
    """A watcher wired to test doubles: a table of pages by targetId (the manager's cache), a set of refused
    pages, and a mutable 'counts' flag for the session-wide fallback."""

    def __init__(self) -> None:
        self.cdp = FakeCdp()
        self.browser = FakeBrowser(self.cdp)
        self.session = FakeSession(self.browser)
        self.counts = False
        self.grace = False
        # The manager's cache: a page by its targetId, and the targetId a page resolves to.
        self.pages: dict[str, Any] = {}
        self.target_ids: dict[Any, str] = {}
        self.refused: set[Any] = set()
        self.enabled = True
        self.seeded: dict[Any, str] = {}
        self.births = WindowBirths(
            enabled=lambda: self.enabled,
            opener_refused=lambda _session, page: page in self.refused or self.grace,
            session_counts_as_refused=lambda _session: self.counts,
            page_by_target_id=lambda _session, tid: self._open(self.pages.get(tid)),
            session_pages=lambda _session: [page for page in self.pages.values() if not page.closed],
            resolve_target_id=self._resolve,
            known_target_id=lambda page: self.target_ids.get(page),
            seed_target_id=lambda page, tid: self.seeded.__setitem__(page, tid),
        )

    @staticmethod
    def _open(page: Any) -> Any:
        return page if page is not None and not page.closed else None

    async def _resolve(self, page: Any) -> str | None:
        return self.target_ids.get(page)

    def known(self, target_id: str, *, refused: bool = False) -> FakePage:
        """A page the manager knows by its targetId (the first page, an open-tab tab, a reported popup)."""
        page = FakePage()
        self.pages[target_id] = page
        self.target_ids[page] = target_id
        if refused:
            self.refused.add(page)
        return page

    async def watch(self) -> WindowBirthWatcher:
        await self.births.watch(self.session, context=object(), page=object(), context_id=CTX)
        return self.births._get(self.browser)

    @property
    def watcher(self) -> WindowBirthWatcher:
        return self.births._get(self.browser)

    async def report(self, target_id: str) -> BirthVerdict:
        page = FakePage()
        self.target_ids[page] = target_id
        return await self.births.verdict(self.session, page)

    async def sweep(self) -> list[str]:
        await self.births.close_unreported(self.session)
        return self.cdp.closed_targets


@pytest.fixture
async def h() -> Harness:
    harness = Harness()
    await harness.watch()
    yield harness
    await harness.births.release_all()


# ── the birth judgment: keyed on the opener ──────────────────────────────────


async def test_a_window_whose_opener_is_refused_is_born_refused(h: Harness) -> None:
    h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))

    assert await h.report("T1") is BirthVerdict.REFUSED


async def test_a_window_whose_opener_is_clean_is_not_born_refused_though_another_tab_is(h: Harness) -> None:
    """Keyed on the opener, not the session: a clean tab's popup is readable while an unrelated tab is refused."""
    h.known("OP")
    h.known("OTHER", refused=True)
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))

    assert await h.report("T1") is BirthVerdict.CLEAN


async def test_a_window_born_within_the_grace_after_a_counting_page_closed_is_born_refused(h: Harness) -> None:
    h.known("OP")
    h.grace = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))

    assert await h.report("T1") is BirthVerdict.REFUSED


async def test_a_window_with_no_opener_is_never_born_refused(h: Harness) -> None:
    """The controller's own context.new_page(): a session's first page, or an open-tab tool tab."""
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id=None))

    assert await h.report("T1") is BirthVerdict.CLEAN


async def test_an_unmatched_opener_falls_back_to_the_session_and_says_so(h: Harness) -> None:
    """The opener's targetId is not cached (and it has no birth): the session decides, and the verdict is
    marked as the session's, so the refusal names another tab as the cause."""
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="UNKNOWN"))
    assert await h.report("T1") is BirthVerdict.REFUSED_BY_SESSION

    h.counts = False
    h.cdp.fire("Target.targetCreated", created("T2", CTX, opener_id="UNKNOWN"))
    assert await h.report("T2") is BirthVerdict.CLEAN


async def test_an_opener_is_resolved_from_the_watchers_own_registry_before_the_session(h: Harness) -> None:
    """A reported popup's page is kept on its birth: a window it opens is judged by it, not by the session,
    even when the manager's cache does not hold its targetId."""
    h.known("OP")
    h.cdp.fire("Target.targetCreated", created("P", CTX, opener_id="OP"))
    assert await h.report("P") is BirthVerdict.CLEAN
    assert "P" not in h.pages  # the manager's cache does not know P; the watcher's birth does
    h.counts = True  # another tab is refused

    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="P"))

    assert await h.report("T1") is BirthVerdict.CLEAN


async def test_a_reported_popup_refused_later_refuses_its_windows_through_the_registry(h: Harness) -> None:
    """The same resolution when it matters: the reported popup is refused by the time it opens a window (a
    taint, say), its targetId still not in the manager's cache. The watcher's own page reference finds it
    refused; without that reference the window would be judged by the unmatched-clean-opener rule and let
    through."""
    h.known("OP")
    h.cdp.fire("Target.targetCreated", created("P", CTX, opener_id="OP"))
    assert await h.report("P") is BirthVerdict.CLEAN
    popup = h.watcher._births["P"].live_page()
    assert popup is not None and "P" not in h.pages
    h.refused.add(popup)

    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="P"))

    assert await h.report("T1") is BirthVerdict.REFUSED


# ── propagation: a born-refused window's windows ──────────────────────────────


async def test_a_window_opened_by_an_unreported_born_refused_window_is_born_refused(h: Harness) -> None:
    """The opener is a window Playwright never reported (no page to match), opened after the agent's navigate
    cleared everything: still the refused page's, so born refused."""
    h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("W1", CTX, opener_id="OP"))
    h.refused.clear()  # the agent's navigate cleared the opener
    h.counts = False

    h.cdp.fire("Target.targetCreated", created("C", CTX, opener_id="W1"))

    assert h.watcher._births["C"].born_refused is True
    assert await h.report("C") is BirthVerdict.REFUSED


async def test_a_window_opened_by_a_reported_born_refused_window_is_born_refused(h: Harness) -> None:
    h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("W1", CTX, opener_id="OP"))
    assert await h.report("W1") is BirthVerdict.REFUSED
    h.refused.clear()
    h.counts = False

    h.cdp.fire("Target.targetCreated", created("C", CTX, opener_id="W1"))

    assert await h.report("C") is BirthVerdict.REFUSED


async def test_propagation_reaches_a_grandchild(h: Harness) -> None:
    h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("W1", CTX, opener_id="OP"))
    h.refused.clear()
    h.cdp.fire("Target.targetCreated", created("C", CTX, opener_id="W1"))
    h.cdp.fire("Target.targetCreated", created("G", CTX, opener_id="C"))

    assert h.watcher._births["G"].born_refused is True


# ── the page-type filter ───────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["iframe", "worker", "service_worker", "other"])
async def test_only_page_targets_make_an_entry(h: Harness, kind: str) -> None:
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("F1", CTX, opener_id="OP", type=kind))

    assert h.watcher._births == {}


async def test_a_target_in_an_unknown_context_makes_no_entry(h: Harness) -> None:
    """A page target in a context the controller did not create (an operator's own tab under attach mode)."""
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", OTHER_CTX, opener_id="OP"))

    assert h.watcher._births == {}


# ── closing an unreported born-refused window: keyed on its opener ─────────────


async def test_an_unreported_born_refused_window_is_closed_once_its_opener_is_clean(h: Harness) -> None:
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))

    # While the opener is still refused, the window is still justified: nothing is closed.
    assert await h.sweep() == []

    # The agent's navigate (or a rollback) cleared the opener.
    h.refused.discard(opener)
    assert await h.sweep() == ["T1"]


async def test_a_sibling_refused_tab_does_not_keep_the_window_alive(h: Harness) -> None:
    """Reviewer's P5e: a tainted popup keeps the session counting, but the window's own opener is clean, so the
    window is closed all the same."""
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    h.known("SIBLING", refused=True)
    h.counts = True

    h.refused.discard(opener)

    assert await h.sweep() == ["T1"]


async def test_a_window_whose_opener_is_gone_is_closed_once_the_session_stops_counting(h: Harness) -> None:
    """The opener closed: the grace keeps the session counting a little; past it nothing justifies the window."""
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    opener.closed = True
    h.counts = True  # the grace after a counting page closed
    assert await h.sweep() == []

    h.counts = False
    assert await h.sweep() == ["T1"]


async def test_a_window_of_an_unreported_born_refused_window_is_closed_with_it(h: Harness) -> None:
    """The nested window's opener is itself an unreported born-refused window: justified while that one is."""
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("W1", CTX, opener_id="OP"))
    h.cdp.fire("Target.targetCreated", created("C", CTX, opener_id="W1"))
    assert await h.sweep() == []

    h.refused.discard(opener)
    assert sorted(await h.sweep()) == ["C", "W1"]


async def test_a_session_fallback_window_is_closed_once_the_session_stops_counting(h: Harness) -> None:
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="UNKNOWN"))
    assert await h.sweep() == []

    h.counts = False
    assert await h.sweep() == ["T1"]


async def test_a_reported_born_refused_window_is_not_closed(h: Harness) -> None:
    """Reviewer's P1: a reported page is the agent's to navigate or close, never the sweep's."""
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    assert await h.report("T1") is BirthVerdict.REFUSED  # Playwright reported it: refused, not closed
    h.refused.discard(opener)

    assert await h.sweep() == []


async def test_the_sweep_skips_a_reported_page_whose_birth_was_not_marked_yet(h: Harness) -> None:
    """The report's targetId read is still in flight when the sweep runs: the session's pages are resolved
    first, and a page among them is never closed."""
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    reported = h.known("T1")  # in the session's pages, its birth not marked reported
    h.refused.discard(opener)

    assert await h.sweep() == []
    assert h.watcher._births["T1"].reported is True
    assert reported is h.pages["T1"]


async def test_a_window_with_no_opener_is_not_closed(h: Harness) -> None:
    """The open-tab tool's tab: born with no opener, so never closed, however the session counts."""
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id=None))

    h.counts = False
    assert await h.sweep() == []


async def test_the_sweep_never_reads_an_uncached_pages_target_id(h: Harness, monkeypatch) -> None:
    """A refused page can flood the session with windows that commit: each is reported, and sits in the
    session's pages until its report's targetId read lands. The sweep must not open a CDP session of its own to
    identify each one, which — kept for the page's life — a refused page could turn into resource exhaustion (it
    hung the controller before this). While any reported page's id is unknown it closes nothing it cannot
    match, since any of it could be that page; once every id is known, the unmatched window goes."""
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))  # born refused, unreported
    flood = [FakePage() for _ in range(5)]
    for i, page in enumerate(flood):
        h.pages[f"FLOOD{i}"] = page  # in the session's pages, its targetId not read yet

    resolved: list[Any] = []
    original = h.births._resolve_target_id

    async def spy(page: Any) -> str | None:
        resolved.append(page)
        return await original(page)

    monkeypatch.setattr(h.births, "_resolve_target_id", spy)
    h.refused.discard(opener)

    assert await h.sweep() == [], "a window was closed while a reported page's id was unknown: it could be that page"
    assert resolved == [], "the sweep read a page's targetId instead of using only cached ids"

    for i, page in enumerate(flood):
        h.target_ids[page] = f"FLOOD{i}"  # their reads landed
    assert await h.sweep() == ["T1"]
    assert resolved == []


async def test_close_target_failure_leaves_the_window_claimable_again(h: Harness) -> None:
    h.known("OP")  # clean: nothing justifies the window
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="UNKNOWN"))
    h.watcher._births["T1"].born_refused = True
    h.cdp.fail_close = True

    await h.sweep()  # the send raises; must not crash

    h.cdp.fail_close = False
    assert await h.sweep() == ["T1"], "a failed close did not mark the window closed, so a retry closes it"


async def test_the_recheck_closes_a_window_whatever_ended_the_refusal(h: Harness, monkeypatch) -> None:
    """No navigate, rollback or close ran the sweep: the opener was cleared another way (a page leaving its
    blank). The watcher's own re-check closes the window within RECHECK_INTERVAL_SECONDS."""
    monkeypatch.setattr(wb, "RECHECK_INTERVAL_SECONDS", 0.05)
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    await asyncio.sleep(0.15)
    assert h.cdp.closed_targets == []

    h.refused.discard(opener)
    await asyncio.sleep(0.3)

    assert h.cdp.closed_targets == ["T1"]


# ── state is bounded: one entry per live target, dropped on destroy ────────────


async def test_state_is_one_entry_per_live_target_and_dropped_on_destroy(h: Harness) -> None:
    h.counts = True
    for index in range(500):
        target_id = f"T{index}"
        h.cdp.fire("Target.targetCreated", created(target_id, CTX, opener_id="OP"))
        h.cdp.fire("Target.targetDestroyed", {"targetId": target_id})

    assert h.watcher._births == {}, "a flood of births and destroys left state behind"


async def test_repeated_info_changes_do_not_multiply_or_downgrade_the_entry(h: Harness) -> None:
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    h.counts = False  # a later info change must not flip the birth verdict to clean
    h.cdp.fire("Target.targetInfoChanged", created("T1", CTX, opener_id="OP"))

    assert len(h.watcher._births) == 1
    assert h.watcher._births["T1"].born_refused is True


# ── fail closed ────────────────────────────────────────────────────────────────


async def test_a_report_with_no_birth_is_refused_and_its_late_birth_is_marked_reported(h: Harness) -> None:
    """A report the watcher cannot explain (no birth came for it) is refused as unjudged, whether or not the
    session counts; the birth, when it lands, is a reported page's, so the sweep never closes it."""
    h.counts = False
    page = FakePage()
    h.target_ids[page] = "T1"
    assert await h.births.verdict(h.session, page) is BirthVerdict.REFUSED_UNJUDGED

    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="UNKNOWN"))
    assert h.watcher._births["T1"].reported is True
    h.counts = False
    assert await h.sweep() == []


async def test_a_report_whose_target_id_cannot_be_read_is_refused(h: Harness, monkeypatch) -> None:
    """Read TARGET_ID_READ_ATTEMPTS times, then refused as unjudged: its birth cannot be told."""
    monkeypatch.setattr(wb, "TARGET_ID_RETRY_SECONDS", 0.0)
    h.counts = False
    page = FakePage()  # not in target_ids: resolve returns None
    reads: list[Any] = []
    original = h.births._resolve_target_id

    async def counting(page: Any) -> str | None:
        reads.append(page)
        return await original(page)

    monkeypatch.setattr(h.births, "_resolve_target_id", counting)

    assert await h.births.verdict(h.session, page) is BirthVerdict.REFUSED_UNJUDGED
    assert len(reads) == wb.TARGET_ID_READ_ATTEMPTS


async def test_a_failed_attach_refuses_the_session_as_the_gate_does() -> None:
    harness = Harness()
    harness.browser.fail_attach = True

    with pytest.raises(WindowBirthWatcherUnavailable) as raised:
        await harness.watch()

    assert raised.value.status_code == 503
    assert raised.value.retryable is True
    assert raised.value.code == "window_birth_watcher_unavailable"
    assert harness.births.active_for(harness.session) is False
    assert harness.births.status()["state"] == "attach_failed"


async def test_an_unreadable_context_id_refuses_the_session() -> None:
    class Context:
        async def new_cdp_session(self, _page):
            raise asyncio.TimeoutError()

    harness = Harness()
    with pytest.raises(WindowBirthWatcherUnavailable):
        await harness.births.watch(harness.session, context=Context(), page=object(), context_id=None)


async def test_a_failed_discover_detaches_the_browser_session_and_backs_off(monkeypatch) -> None:
    """Reviewer's P8: setDiscoverTargets failing left the browser session attached, with its handlers, once per
    retry. Now it is detached, and the next attach on that browser waits out a backoff."""
    harness = Harness()
    harness.browser.fail_discover = True
    harness.cdp.fail_discover = True

    with pytest.raises(WindowBirthWatcherUnavailable):
        await harness.watch()
    assert harness.cdp.detached is True

    # Within the backoff: no new browser session is opened, the session is still refused.
    with pytest.raises(WindowBirthWatcherUnavailable):
        await harness.watch()
    assert len(harness.browser.cdps) == 1

    # Past it: a new attach is tried, and succeeds once discovery works.
    monkeypatch.setattr(wb, "ATTACH_RETRY_BACKOFF_SECONDS", 0.0)
    harness.browser.fail_discover = False
    await harness.watch()
    assert harness.births.active_for(harness.session) is True


async def test_a_lost_watcher_holds_popups_refused_and_a_replacement_re_registers_every_session(h: Harness) -> None:
    other = FakeSession(h.browser, "s2")
    await h.births.watch(other, context=object(), page=object(), context_id=OTHER_CTX)
    old = h.watcher

    h.cdp.fire("close", {})  # the browser session went down on its own

    assert old.live is False
    assert h.births.active_for(h.session) is False
    assert h.births.holds_refused(h.session) is True
    assert h.births.holds_refused(other) is True
    assert h.births.status()["state"] == "lost"

    await asyncio.sleep(0.05)  # the replacement attaches in the background
    new = h.watcher
    assert new is not None and new is not old
    assert new._sessions == {CTX: h.session, OTHER_CTX: other}
    assert h.births.holds_refused(h.session) is False


# ── the registry: no-gate attach, active_for, forget, release ──────────────────


async def test_watch_attaches_a_watcher_with_no_gate_and_active_for_reports_it() -> None:
    harness = Harness()
    assert harness.births.active_for(harness.session) is False

    await harness.watch()

    assert harness.births.active_for(harness.session) is True
    assert ("Target.setDiscoverTargets", {"discover": True}) in harness.cdp.sent
    assert harness.births.status() == {
        "state": "attached",
        "watchers": [{"state": "attached", "sessions": 1, "born_refused_unreported": 0}],
    }
    await harness.births.release_all()


async def test_no_watcher_attaches_when_allowed_hosts_refuses_nothing() -> None:
    """When every host is allowed, nothing is ever born refused, so the watcher attaches to nothing."""
    harness = Harness()
    harness.enabled = False
    await harness.births.watch(harness.session, context=object(), page=object(), context_id=CTX)

    assert harness.births.active_for(harness.session) is False
    assert harness.births.holds_refused(harness.session) is False
    assert await harness.births.verdict(harness.session, FakePage()) is BirthVerdict.CLEAN
    assert harness.births.status()["state"] == "off"


async def test_a_session_without_a_browser_is_not_watched() -> None:
    harness = Harness()
    harness.session.browser = None
    await harness.births.watch(harness.session, context=object(), page=object(), context_id=CTX)

    assert harness.births.active_for(harness.session) is False
    assert harness.births.holds_refused(harness.session) is False


async def test_forget_session_drops_the_context_mapping(h: Harness) -> None:
    h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))

    h.births.forget_session(h.session)

    # With the context no longer mapped, close_unreported cannot find the session's windows.
    h.refused.clear()
    assert await h.sweep() == []
    assert h.births.watching(h.session) is False


async def test_release_detaches_the_watcher(h: Harness) -> None:
    await h.births.release(h.browser)

    assert h.cdp.detached is True
    assert h.births.active_for(h.session) is False


async def test_a_dead_watcher_closes_nothing(h: Harness) -> None:
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    h.cdp.fire("close", {})  # the browser session went down with the browser
    h.refused.discard(opener)

    await h.births.close_unreported(h.session)
    assert h.cdp.closed_targets == []


# ── round 3: an unmatched opener born clean ────────────────────────────────────


async def test_a_window_of_a_clean_popup_whose_read_is_in_flight_is_born_clean(h: Harness) -> None:
    """Reviewer's R5: a fresh clean popup opens a window from its inline script before its own targetId read
    lands, while another tab is refused. The opener is unmatched, but it was born clean and every open page
    whose id is unknown is clean: the window is born clean, not refused by the session."""
    h.known("OP")
    h.known("OTHER", refused=True)
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("P", CTX, opener_id="OP"))
    h.pages["P-unread"] = FakePage()  # reported, in the session's pages, its targetId not cached yet

    h.cdp.fire("Target.targetCreated", created("C", CTX, opener_id="P"))

    assert h.watcher._births["C"].born_refused is False
    assert await h.report("C") is BirthVerdict.CLEAN


async def test_a_window_of_an_unmatched_clean_opener_falls_back_when_an_unread_page_is_refused(h: Harness) -> None:
    """The opener was born clean but a refused page's id is unknown too: the window could be that page's, so the
    session decides (fail safe), and the refusal names another tab."""
    h.known("OP")
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("P", CTX, opener_id="OP"))
    h.pages["P-unread"] = FakePage()
    refused_unread = FakePage()
    h.pages["Q-unread"] = refused_unread
    h.refused.add(refused_unread)

    h.cdp.fire("Target.targetCreated", created("C", CTX, opener_id="P"))

    assert h.watcher._births["C"].born_refused is True
    assert await h.report("C") is BirthVerdict.REFUSED_BY_SESSION


# ── round 3: the replacement watcher ──────────────────────────────────────────


class ReplayingCdp(FakeCdp):
    """A browser session whose discovery replays the targets that exist, before it answers, as Chromium's does."""

    def __init__(self, replay: list[dict] | None = None) -> None:
        super().__init__()
        self.replay = replay or []
        self.sessions_at_discovery: int | None = None

    async def send(self, method: str, params: dict | None = None) -> dict:
        if method == "Target.setDiscoverTargets":
            # The handlers are the watcher's bound methods: how many sessions it knows as discovery starts.
            watcher = self.handlers["Target.targetCreated"][0].__self__
            self.sessions_at_discovery = watcher.session_count
            for event in self.replay:
                self.fire("Target.targetCreated", event)
        return await super().send(method, params)


async def replace_watcher(h: Harness, replay: list[dict] | None = None) -> ReplayingCdp:
    """The watcher's browser session dies on its own; the replacement attaches on ``replay``'s targets."""
    replacement = ReplayingCdp(replay)

    async def next_browser_session() -> FakeCdp:
        h.browser.cdps.append(replacement)
        return replacement

    h.browser.new_browser_cdp_session = next_browser_session  # type: ignore[method-assign]
    h.cdp.fire("close", {})
    await asyncio.sleep(0.05)
    assert h.watcher is not None and h.watcher._cdp is replacement
    return replacement


async def test_a_replacement_registers_the_sessions_before_it_discovers_the_targets(h: Harness) -> None:
    """Chromium replays targetCreated for every existing target before setDiscoverTargets returns; a watcher
    that registered its sessions only after that dropped the replay and knew nothing of the windows born under
    the lost watcher (the orchestrator's O3 leak)."""
    other = FakeSession(h.browser, "s2")
    await h.births.watch(other, context=object(), page=object(), context_id=OTHER_CTX)
    replacement = await replace_watcher(h)

    assert replacement.sessions_at_discovery == 2
    assert h.watcher._sessions == {CTX: h.session, OTHER_CTX: other}


async def test_targets_a_replacement_first_sees_in_the_replay_fail_closed_unless_known(h: Harness) -> None:
    """The replay: a known page (a cached targetId) is reported and clean; a window with an opener is unjudged
    (refused, counted by readiness, swept once the session stops counting, and its report says so); one with
    no opener is clean."""
    first = h.known("FIRST")
    h.counts = True
    replacement = await replace_watcher(
        h,
        [
            created("FIRST", CTX, opener_id=None),
            created("W", CTX, opener_id="FIRST"),
            created("N", CTX, opener_id=None),
        ],
    )
    new = h.watcher

    assert new._births["FIRST"].reported is True and new._births["FIRST"].born_refused is False
    assert new._births["FIRST"].live_page() is first
    assert new._births["W"].born_refused is True and new._births["W"].unjudged is True
    assert new._births["N"].born_refused is False
    assert h.births.status()["watchers"][0]["born_refused_unreported"] == 1

    # Reported: refused as unjudged. Unreported: swept once the session stops counting.
    page = FakePage()
    h.target_ids[page] = "W"
    assert await h.births.verdict(h.session, page) is BirthVerdict.REFUSED_UNJUDGED
    new._births["W"].reported = False
    assert await h.sweep() == []
    h.counts = False
    await h.births.close_unreported(h.session)
    assert replacement.closed_targets == ["W"]


async def test_an_info_change_for_an_unknown_target_is_a_late_sight_too(h: Harness) -> None:
    """A targetInfoChanged for a target the watcher never saw born (it was born while the watcher was lost and
    moves now): judged as the replay's are, not by its opener's current, cleared state."""
    h.known("OP")  # clean now; it was refused when the window was born, under a lost watcher
    h.counts = False
    h.cdp.fire("Target.targetInfoChanged", created("W", CTX, opener_id="OP"))

    assert h.watcher._births["W"].born_refused is True and h.watcher._births["W"].unjudged is True
    assert await h.report("W") is BirthVerdict.REFUSED_UNJUDGED


async def test_a_known_pages_popup_after_a_replacement_is_judged_by_that_page(h: Harness) -> None:
    """A reported clean popup from before the loss (its id cached) opens a window after the replacement: the
    window is judged by it, clean, not refused for the replacement."""
    popup = h.known("P")
    replacement = await replace_watcher(h, [created("P", CTX, opener_id="OP")])
    assert h.watcher._births["P"].live_page() is popup
    h.counts = True

    replacement.fire("Target.targetCreated", created("C", CTX, opener_id="P"))

    assert await h.report("C") is BirthVerdict.CLEAN


async def test_the_recheck_starts_for_the_replays_unjudged_windows(h: Harness, monkeypatch) -> None:
    monkeypatch.setattr(wb, "RECHECK_INTERVAL_SECONDS", 0.05)
    h.counts = False
    replacement = await replace_watcher(h, [created("W", CTX, opener_id="GONE")])
    await asyncio.sleep(0.3)

    assert replacement.closed_targets == ["W"], "the replay's unjudged window was not swept by the recheck"


async def test_a_replacement_sweeps_the_replays_unjudged_windows_at_once(h: Harness) -> None:
    """The orchestrator's O3 probe, released right after the replacement attached: the window had a whole recheck
    interval to act before the first sweep. A replacement sweeps the replay's windows as soon as it is live."""
    h.counts = False
    replacement = await replace_watcher(h, [created("W", CTX, opener_id="GONE")])
    await asyncio.sleep(0.1)  # well inside RECHECK_INTERVAL_SECONDS (1 s)

    assert replacement.closed_targets == ["W"], "the replacement waited a recheck interval before its first sweep"


# ── round 3: the verdict in the loss gap, and no retention ─────────────────────


async def test_a_verdict_read_in_the_loss_gap_is_not_clean(h: Harness, monkeypatch) -> None:
    """Reviewer's finding: a report started under a watcher since lost was mapped to CLEAN. It is awaited all
    the same; and a page with no report at all is refused as unjudged while its session's watcher is lost."""
    h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    page = FakePage()
    h.target_ids[page] = "T1"
    h.births.note_reported(h.session, page)  # attach_popup: the report task starts while the watcher is live
    # No replacement, so the gap stays open: the watcher holds the bound on_lost, so it is the one to silence.
    monkeypatch.setattr(h.watcher, "_on_lost", None)
    h.cdp.fire("close", {})
    assert h.births.holds_refused(h.session)

    assert await h.births.verdict(h.session, page) is BirthVerdict.REFUSED
    other = FakePage()
    h.target_ids[other] = "T1"
    assert await h.births.verdict(h.session, other) is BirthVerdict.REFUSED_UNJUDGED


async def test_a_resolved_report_keeps_the_verdict_not_the_task(h: Harness) -> None:
    h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    page = FakePage()
    h.target_ids[page] = "T1"

    assert await h.births.verdict(h.session, page) is BirthVerdict.REFUSED
    await asyncio.sleep(0)

    assert h.births._reports[page] is BirthVerdict.REFUSED
    assert await h.births.verdict(h.session, page) is BirthVerdict.REFUSED  # served from the kept verdict


# ── round 3: the sweep and a reported page it cannot match ─────────────────────


async def test_the_sweep_spares_the_unmatched_while_a_reported_pages_id_is_unknown(h: Harness, monkeypatch) -> None:
    """Reviewer's D5: a reported page whose targetId read failed has no cached id and its birth is not marked.
    The sweep cannot tell it from an unreported window, so it closes nothing it cannot match while that page is
    open; once the page closes, the unmatched window goes."""
    monkeypatch.setattr(wb, "TARGET_ID_RETRY_SECONDS", 0.0)
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    reported = FakePage()  # the popup T1, reported; its read fails (not in target_ids)
    h.pages["T1?"] = reported
    assert await h.births.verdict(h.session, reported) is BirthVerdict.REFUSED_UNJUDGED
    h.refused.discard(opener)

    assert await h.sweep() == []
    reported.closed = True
    assert await h.sweep() == ["T1"]


async def test_a_window_reported_during_the_sweeps_wait_is_not_closed(h: Harness) -> None:
    """Reviewer's 7c: the sweep waits for the reads in flight, then closes; a report that lands during that wait,
    or during an earlier close's round trip, marks the birth reported, and the close is checked again first."""
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    h.cdp.fire("Target.targetCreated", created("T2", CTX, opener_id="OP"))
    h.refused.discard(opener)

    async def reported_ids(session: Any) -> tuple[set[str], bool]:
        h.watcher.mark_reported(session, "T2", FakePage())  # lands while the sweep waits
        return set(), True

    await h.watcher.close_unreported(h.session, reported_ids)

    assert h.cdp.closed_targets == ["T1"]


async def test_a_window_reported_during_an_earlier_closes_round_trip_is_not_closed(h: Harness) -> None:
    """The sweep's reads are done and the first close is on the wire when Playwright reports the second window:
    the close of the second is checked again right before it is sent, and skipped."""
    opener = h.known("OP", refused=True)
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    h.cdp.fire("Target.targetCreated", created("T2", CTX, opener_id="OP"))
    h.refused.discard(opener)
    real_send = h.cdp.send

    async def send(method: str, params: dict | None = None) -> dict:
        if method == "Target.closeTarget" and params and params["targetId"] == "T1":
            h.watcher.mark_reported(h.session, "T2", FakePage())  # lands during T1's round trip
        return await real_send(method, params)

    h.cdp.send = send  # type: ignore[method-assign]

    async def reported_ids(_session: Any) -> tuple[set[str], bool]:
        return set(), True

    await h.watcher.close_unreported(h.session, reported_ids)

    assert h.cdp.closed_targets == ["T1"]


async def test_the_backoff_message_says_within_the_last_seconds() -> None:
    harness = Harness()
    harness.browser.fail_attach = True
    with pytest.raises(WindowBirthWatcherUnavailable):
        await harness.watch()
    harness.browser.fail_attach = False

    with pytest.raises(WindowBirthWatcherUnavailable, match="within the last 2 s"):
        await harness.watch()

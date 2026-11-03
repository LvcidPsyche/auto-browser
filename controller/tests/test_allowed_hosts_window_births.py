"""The window-birth watcher judges a page target at its CDP birth (app/browser/window_births.py).

These drive the watcher with hand-made CDP events, so every run sees the same order: a page target born
with an opener while its session counts as refused is born refused whatever its first navigation does; one
with no opener (the controller's own new_page) never is; iframes and workers make no entry; a born-refused
window Playwright never reports is closed once the session stops counting, and one it reports is not; state
is one entry per live target, dropped on targetDestroyed; and the report-before-birth race taints the window.

No real browser: the fakes stand in for the browser-level CDP session and the targetId lookup.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.browser.window_births import WindowBirths, WindowBirthWatcher

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

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params))
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
    def __init__(self, cdp: FakeCdp) -> None:
        self._cdp = cdp

    async def new_browser_cdp_session(self) -> FakeCdp:
        return self._cdp


class FakeSession:
    def __init__(self, browser: FakeBrowser, session_id: str = "s1") -> None:
        self.browser = browser
        self.id = session_id


class FakePage:
    """A stand-in for a Playwright Page; unlike object(), it can be weakly referenced."""


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
    """A watcher wired to test doubles, with a mutable 'counts' flag and a targetId lookup table."""

    def __init__(self) -> None:
        self.cdp = FakeCdp()
        self.browser = FakeBrowser(self.cdp)
        self.session = FakeSession(self.browser)
        self.counts = False
        self.target_ids: dict[Any, str] = {}
        self.late_refused: list[tuple[Any, Any]] = []
        self.enabled = True
        self.births = WindowBirths(
            enabled=lambda: self.enabled,
            session_counts_as_refused=lambda _session: self.counts,
            resolve_target_id=self._resolve,
            on_late_refused=lambda session, page: self.late_refused.append((session, page)),
        )

    async def _resolve(self, page: Any) -> str | None:
        return self.target_ids.get(page)

    async def watch(self) -> WindowBirthWatcher:
        await self.births.watch(self.session, context=object(), page=object(), context_id=CTX)
        return self.births._get(self.browser)


@pytest.fixture
async def h() -> Harness:
    harness = Harness()
    await harness.watch()
    return harness


# ── the birth judgment ───────────────────────────────────────────────────────


async def test_a_window_born_with_an_opener_while_the_session_counts_is_born_refused(h: Harness) -> None:
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    page = FakePage()
    h.target_ids[page] = "T1"

    assert await h.births.mark_reported(h.session, page) is True


async def test_a_window_with_no_opener_is_never_born_refused(h: Harness) -> None:
    """The controller's own context.new_page(): a session's first page, or an open-tab tool tab."""
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id=None))
    page = FakePage()
    h.target_ids[page] = "T1"

    assert await h.births.mark_reported(h.session, page) is False


async def test_a_window_born_while_the_session_does_not_count_is_not_born_refused(h: Harness) -> None:
    h.counts = False
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    page = FakePage()
    h.target_ids[page] = "T1"

    assert await h.births.mark_reported(h.session, page) is False


# ── the page-type filter ───────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["iframe", "worker", "service_worker", "other"])
async def test_only_page_targets_make_an_entry(h: Harness, kind: str) -> None:
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("F1", CTX, opener_id="OP", type=kind))

    watcher = h.births._get(h.browser)
    assert watcher._births == {}


async def test_a_target_in_an_unknown_context_makes_no_entry(h: Harness) -> None:
    """A page target in a context the controller did not create (an operator's own tab under attach mode)."""
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", OTHER_CTX, opener_id="OP"))

    assert h.births._get(h.browser)._births == {}


# ── closing an unreported born-refused window ──────────────────────────────────


async def test_an_unreported_born_refused_window_is_closed_once_the_session_stops_counting(h: Harness) -> None:
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))

    # While the session still counts, the window is still justified: nothing is closed.
    await h.births.close_unreported(h.session)
    assert h.cdp.closed_targets == []

    # The agent's navigate (or a rollback) cleared the last refused page.
    h.counts = False
    await h.births.close_unreported(h.session)
    assert h.cdp.closed_targets == ["T1"]


async def test_a_reported_born_refused_window_is_not_closed(h: Harness) -> None:
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    page = FakePage()
    h.target_ids[page] = "T1"
    assert await h.births.mark_reported(h.session, page) is True  # Playwright reported it; taint it, don't close it

    h.counts = False
    await h.births.close_unreported(h.session)
    assert h.cdp.closed_targets == []


async def test_a_window_with_no_opener_is_not_closed(h: Harness) -> None:
    """The open-tab tool's tab: born with no opener, so never closed, however the session counts."""
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id=None))

    h.counts = False
    await h.births.close_unreported(h.session)
    assert h.cdp.closed_targets == []


async def test_close_target_failure_leaves_the_window_claimable_again(h: Harness) -> None:
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    h.cdp.fail_close = True
    h.counts = False

    await h.births.close_unreported(h.session)  # the send raises; must not crash

    h.cdp.fail_close = False
    await h.births.close_unreported(h.session)
    assert h.cdp.closed_targets == ["T1"], "a failed close did not mark the window closed, so a retry closes it"


# ── state is bounded: one entry per live target, dropped on destroy ────────────


async def test_state_is_one_entry_per_live_target_and_dropped_on_destroy(h: Harness) -> None:
    h.counts = True
    watcher = h.births._get(h.browser)

    for index in range(500):
        target_id = f"T{index}"
        h.cdp.fire("Target.targetCreated", created(target_id, CTX, opener_id="OP"))
        h.cdp.fire("Target.targetDestroyed", {"targetId": target_id})

    assert watcher._births == {}, "a flood of births and destroys left state behind"


async def test_repeated_info_changes_do_not_multiply_or_downgrade_the_entry(h: Harness) -> None:
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    h.counts = False  # a later info change must not flip the birth verdict to clean
    h.cdp.fire("Target.targetInfoChanged", created("T1", CTX, opener_id="OP"))

    watcher = h.births._get(h.browser)
    assert len(watcher._births) == 1
    assert watcher._births["T1"].born_refused is True


# ── the event-order race ───────────────────────────────────────────────────────


async def test_report_before_birth_fails_closed_then_the_birth_taints_the_window(h: Harness) -> None:
    """Playwright's page event can arrive before Target.targetCreated: the window is refused meanwhile (fail
    closed while the session counts), and the birth, when it lands born refused, taints the reported page."""
    h.counts = True
    page = FakePage()
    h.target_ids[page] = "T1"

    # Reported first, no birth yet: fail closed (session counts) and remember the page.
    assert await h.births.mark_reported(h.session, page) is True

    # The birth lands afterwards and is born refused: the reported page is tainted now.
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    assert h.late_refused == [(h.session, page)]
    assert h.births._get(h.browser)._births["T1"].reported is True


async def test_mark_reported_fails_closed_when_the_target_id_cannot_be_read(h: Harness) -> None:
    h.counts = True
    page = FakePage()  # not in target_ids: resolve returns None

    assert await h.births.mark_reported(h.session, page) is True

    h.counts = False
    page2 = FakePage()
    assert await h.births.mark_reported(h.session, page2) is False


# ── the registry: no-gate attach, active_for, forget, release ──────────────────


async def test_watch_attaches_a_watcher_with_no_gate_and_active_for_reports_it() -> None:
    harness = Harness()
    assert harness.births.active_for(harness.session) is False

    await harness.watch()

    assert harness.births.active_for(harness.session) is True
    assert ("Target.setDiscoverTargets", {"discover": True}) in harness.cdp.sent


async def test_no_watcher_attaches_when_allowed_hosts_refuses_nothing() -> None:
    """When every host is allowed, nothing is ever born refused, so the watcher attaches to nothing."""
    harness = Harness()
    harness.enabled = False
    await harness.births.watch(harness.session, context=object(), page=object(), context_id=CTX)

    assert harness.births.active_for(harness.session) is False
    assert await harness.births.mark_reported(harness.session, FakePage()) is False


async def test_a_session_without_a_browser_is_not_watched() -> None:
    harness = Harness()
    harness.session.browser = None
    await harness.births.watch(harness.session, context=object(), page=object(), context_id=CTX)

    assert harness.births.active_for(harness.session) is False
    assert await harness.births.mark_reported(harness.session, object()) is False


async def test_forget_session_drops_the_context_mapping(h: Harness) -> None:
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))

    h.births.forget_session(h.session)

    # With the context no longer mapped, close_unreported cannot find the session's windows.
    h.counts = False
    await h.births.close_unreported(h.session)
    assert h.cdp.closed_targets == []


async def test_release_detaches_the_watcher(h: Harness) -> None:
    await h.births.release(h.browser)

    assert h.cdp.detached is True
    assert h.births.active_for(h.session) is False


async def test_a_dead_watcher_closes_nothing(h: Harness) -> None:
    h.counts = True
    h.cdp.fire("Target.targetCreated", created("T1", CTX, opener_id="OP"))
    h.cdp.fire("close", {})  # the browser session went down with the browser
    h.counts = False

    await h.births.close_unreported(h.session)
    assert h.cdp.closed_targets == []

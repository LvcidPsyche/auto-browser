"""A window-birth watcher that is lost is attached again, and an attach that fails is tried a few more times
(app/browser/window_births.py, WindowBirths._lost).

The loss starts one attach. When that one failed, the watcher stayed lost, and the sessions' popups refused, until a
new session was created on the browser. Now the attach is tried REATTACH_RETRIES more times, each a little later than
the attach backoff, and only while the browser still has the lost watcher and a watched session.

A release of the browser ends them, and forgets the browser before it waits for anything: one that is cancelled at a
wait has forgotten it all the same.

No real browser. The clock is the test's too (time.monotonic and asyncio.sleep, as window_births sees them), so a
wait ends when the test says; the last test runs on the real clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import pytest

from app.browser import window_births as wb
from tests.test_allowed_hosts_window_births import CTX, OTHER_CTX, FakeBrowser, FakeCdp, FakeSession, Harness

BACKOFF = wb.ATTACH_RETRY_BACKOFF_SECONDS
# Later than any retry's wait, and sooner than two of them.
A_RETRY_LATER = BACKOFF * 1.1
# The founder's decision for 1.18.0: three more attaches after the one the loss itself starts.
RETRIES = 3
REFUSED = "could not re-attach after a loss"
IDLE = {"state": "idle", "watchers": []}


class _ModuleWith:
    """A module as window_births sees it, with some of its names replaced."""

    def __init__(self, module: Any, **replaced: Any) -> None:
        self._module = module
        self.__dict__.update(replaced)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)


class Clock:
    """window_births' clock, moved by the test: nothing it sleeps on ends before advance() has passed its time."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Event]] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float, result: Any = None) -> Any:
        woken = asyncio.Event()
        self._sleepers.append((self.now + delay, woken))
        await woken.wait()
        return result

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        due = [woken for at, woken in self._sleepers if at <= self.now]
        self._sleepers = [(at, woken) for at, woken in self._sleepers if at > self.now]
        for woken in due:
            woken.set()
        await settle()


async def settle() -> None:
    """Let what is ready run to its next wait: every step here is a fake's, which answers within a few loop turns."""
    for _ in range(50):
        await asyncio.sleep(0)


class Attaches:
    """The attaches asked of a browser, counted; the next ``refuse`` of them fail."""

    def __init__(self, browser: FakeBrowser) -> None:
        self.asked = 0
        self.refuse = 0
        self._attach = browser.new_browser_cdp_session
        browser.new_browser_cdp_session = self._counted

    async def _counted(self) -> FakeCdp:
        self.asked += 1
        if self.refuse:
            self.refuse -= 1
            raise RuntimeError("the browser was busy")
        return await self._attach()


class Watched(Harness):
    """A session whose watcher is attached, on a browser that counts the attaches asked of it after that."""

    def __init__(self) -> None:
        super().__init__()
        self._before: set[Any] = set()

    async def start(self) -> "Watched":
        await self.watch()
        self.attaches = Attaches(self.browser)
        return self

    async def lose(self, *, refuse: int = 0) -> None:
        """The live watcher's browser session goes down on its own; the next ``refuse`` attaches fail."""
        self._before = asyncio.all_tasks()
        self.attaches.refuse = refuse
        self.browser.cdp.fire("close", {})
        await settle()

    def left_running(self) -> set[Any]:
        """The tasks started since the loss that have not ended."""
        return asyncio.all_tasks() - self._before

    def lost(self) -> bool:
        return self.births.holds_refused(self.session) and self.births.status()["state"] == "lost"

    def attached(self) -> bool:
        return self.births.active_for(self.session) and self.births.status()["state"] == "attached"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(wb, "time", _ModuleWith(time, monotonic=clock.monotonic))
    monkeypatch.setattr(wb, "asyncio", _ModuleWith(asyncio, sleep=clock.sleep))
    return clock


@pytest.fixture
async def w(clock: Clock) -> Watched:
    watched = await Watched().start()
    yield watched
    await watched.births.release_all()


def refusals(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if REFUSED in record.getMessage()]


def assert_forgotten(w: Watched) -> None:
    """Neither registry holds the browser, and /readiness is told of no watcher."""
    assert id(w.browser) not in w.births._watchers
    assert id(w.browser) not in w.births._attach_failures
    assert w.births.status() == IDLE


# ── the retry ────────────────────────────────────────────────────────────────


async def test_a_re_attach_that_failed_is_tried_again_and_attaches_without_a_new_session(
    w: Watched, clock: Clock
) -> None:
    old = w.watcher
    await w.lose(refuse=1)
    assert w.attaches.asked == 1  # the attach the loss itself starts
    assert w.lost()

    await clock.advance(A_RETRY_LATER)

    assert w.attaches.asked == 2
    assert w.attached()
    assert w.births.holds_refused(w.session) is False
    # The replacement judges the session whose creation it never saw.
    assert w.watcher is not old and w.watcher._sessions == {CTX: w.session}
    assert w.left_running() == set()


async def test_a_retry_comes_later_than_the_attach_backoff_and_reaches_the_browser(
    w: Watched, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """Sooner, it is answered from the failure _ensure keeps for the backoff, and the browser is never asked."""
    caplog.set_level(logging.WARNING, logger=wb.logger.name)
    await w.lose(refuse=99)

    await clock.advance(BACKOFF)
    assert w.attaches.asked == 1

    await clock.advance(A_RETRY_LATER - BACKOFF)
    assert w.attaches.asked == 2
    assert len(refusals(caplog)) == 2
    assert all("the browser was busy" in line for line in refusals(caplog))


async def test_the_attach_is_tried_three_more_times_and_then_the_watcher_stays_lost(w: Watched, clock: Clock) -> None:
    await w.lose(refuse=99)

    for retry in range(1, RETRIES + 1):
        await clock.advance(A_RETRY_LATER)
        assert w.attaches.asked == 1 + retry

    await clock.advance(A_RETRY_LATER * 10)
    assert w.attaches.asked == 1 + RETRIES
    assert w.lost()
    assert w.left_running() == set()
    assert wb.REATTACH_RETRIES == RETRIES


async def test_the_last_retry_can_still_attach(w: Watched, clock: Clock) -> None:
    await w.lose(refuse=RETRIES)

    for _ in range(RETRIES):
        assert w.lost()
        await clock.advance(A_RETRY_LATER)

    assert w.attaches.asked == 1 + RETRIES
    assert w.attached()
    assert w.left_running() == set()


async def test_a_new_session_still_attaches_the_watcher_once_the_retries_have_ended(w: Watched, clock: Clock) -> None:
    await w.lose(refuse=1 + RETRIES)
    for _ in range(RETRIES):
        await clock.advance(A_RETRY_LATER)
    assert w.lost() and w.attaches.asked == 1 + RETRIES
    await clock.advance(BACKOFF)  # the last failure's backoff, which a new session's attach waits out too

    newcomer = FakeSession(w.browser, "s2")
    await w.births.watch(newcomer, context=object(), page=object(), context_id=OTHER_CTX)

    assert w.attached()
    assert w.watcher._sessions == {CTX: w.session, OTHER_CTX: newcomer}


# ── where the retries stop ───────────────────────────────────────────────────


async def test_releasing_the_browser_ends_its_retry_before_release_returns(w: Watched, clock: Clock) -> None:
    await w.lose(refuse=99)
    assert len(w.left_running()) == 1  # the retry, waiting out the backoff

    await w.births.release(w.browser)

    assert w.left_running() == set()
    await clock.advance(A_RETRY_LATER * 5)
    assert w.attaches.asked == 1


async def test_releasing_the_browser_ends_a_retry_that_is_attaching(w: Watched, clock: Clock) -> None:
    await w.lose(refuse=1)
    attaching = asyncio.Event()

    async def never_answers() -> FakeCdp:
        attaching.set()
        await asyncio.Event().wait()

    w.browser.new_browser_cdp_session = never_answers
    await clock.advance(A_RETRY_LATER)
    await asyncio.wait_for(attaching.wait(), timeout=5)

    await asyncio.wait_for(w.births.release(w.browser), timeout=5)

    assert w.left_running() == set()
    assert w.births.status() == {"state": "idle", "watchers": []}


async def test_the_controllers_shutdown_leaves_no_retry_behind(w: Watched, clock: Clock) -> None:
    await w.lose(refuse=99)
    assert len(w.left_running()) == 1

    await w.births.release_all()

    assert w.left_running() == set()
    await clock.advance(A_RETRY_LATER * 5)
    assert w.attaches.asked == 1


async def test_the_retries_stop_when_the_browsers_last_watched_session_is_gone(w: Watched, clock: Clock) -> None:
    """A session on another browser does not keep them going; another on this browser does."""
    elsewhere = FakeSession(FakeBrowser(), "s2")
    await w.births.watch(elsewhere, context=object(), page=object(), context_id=OTHER_CTX)
    beside = FakeSession(w.browser, "s3")
    await w.births.watch(beside, context=object(), page=object(), context_id="CTX-3")
    await w.lose(refuse=99)

    w.births.forget_session(w.session)
    await clock.advance(A_RETRY_LATER)
    assert w.attaches.asked == 2  # ``beside`` is still watched on this browser

    w.births.forget_session(beside)
    await clock.advance(A_RETRY_LATER)
    assert w.attaches.asked == 2
    assert w.left_running() == set()
    await clock.advance(A_RETRY_LATER * 5)
    assert w.attaches.asked == 2


async def test_a_retry_whose_watcher_a_new_session_replaced_does_nothing_more(
    w: Watched, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """Not even when the replacement is lost in turn: that loss has retries of its own."""
    caplog.set_level(logging.WARNING, logger=wb.logger.name)
    await w.lose(refuse=1)
    await clock.advance(BACKOFF * 1.02)  # past the backoff, and before the retry
    newcomer = FakeSession(w.browser, "s2")
    await w.births.watch(newcomer, context=object(), page=object(), context_id=OTHER_CTX)
    assert w.attaches.asked == 2 and w.attached()
    first_retry = w.left_running()
    assert len(first_retry) == 1

    await w.lose(refuse=99)  # the replacement's own loss, and its first attach refused
    assert w.attaches.asked == 3
    caplog.clear()
    await clock.advance(BACKOFF * 0.05)  # the first watcher's retry is due

    assert all(task.done() for task in first_retry)
    assert refusals(caplog) == []
    assert w.attaches.asked == 3
    assert len(w.left_running()) == 1  # the replacement's retry

    await clock.advance(A_RETRY_LATER)
    assert w.attaches.asked == 4


# ── a release that is cancelled, or beside an attach ─────────────────────────


async def test_a_release_cancelled_while_it_waits_for_a_retry_to_end_has_forgotten_the_browser(
    w: Watched, clock: Clock
) -> None:
    """A teardown step's bound, or the caller's own cancel, which still reaches the caller."""
    await w.lose(refuse=99)
    assert w.lost() and len(w.left_running()) == 1  # the retry, waiting out the backoff

    release = asyncio.ensure_future(w.births.release(w.browser))
    await asyncio.sleep(0)  # as far as its first wait: for the retry it cancelled
    assert not release.done()
    release.cancel()
    with pytest.raises(asyncio.CancelledError):
        await release

    assert_forgotten(w)
    assert w.left_running() == set()
    await clock.advance(A_RETRY_LATER * 5)
    assert w.attaches.asked == 1


async def test_a_release_cancelled_while_it_waits_for_the_lock_has_forgotten_the_browser(w: Watched) -> None:
    """The lock is another browser's attach's, for as long as that attach takes."""
    async with w.births._lock:
        release = asyncio.ensure_future(w.births.release(w.browser))
        await settle()
        assert not release.done()
        release.cancel()
        with pytest.raises(asyncio.CancelledError):
            await release

    assert_forgotten(w)


async def test_a_release_waits_for_an_attach_in_flight_and_forgets_the_watcher_it_registers(clock: Clock) -> None:
    """A new session's own attach, which a release does not end: what it registers must not outlive the release."""
    w = Watched()
    answer = asyncio.Event()
    attach = w.browser.new_browser_cdp_session

    async def answers_late() -> FakeCdp:
        await answer.wait()
        return await attach()

    w.browser.new_browser_cdp_session = answers_late
    watching = asyncio.ensure_future(w.watch())
    await settle()
    release = asyncio.ensure_future(w.births.release(w.browser))
    await settle()
    assert not watching.done() and not release.done()

    answer.set()
    await release
    await watching

    assert_forgotten(w)
    assert w.browser.cdp.detached is True


async def test_a_release_forgets_the_watcher_of_a_retry_that_attached_although_it_was_cancelled(
    w: Watched, clock: Clock
) -> None:
    """A cancel does not always end an attach: on Python 3.11 asyncio.wait_for answers a task cancelled once the
    browser had answered with that answer, and the attach goes on. Here the browser's own call does that."""
    await w.lose(refuse=1)
    attaching = asyncio.Event()
    attach = w.browser.new_browser_cdp_session

    async def answers_all_the_same() -> FakeCdp:
        attaching.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        return await attach()

    w.browser.new_browser_cdp_session = answers_all_the_same
    await clock.advance(A_RETRY_LATER)
    await asyncio.wait_for(attaching.wait(), timeout=5)

    await asyncio.wait_for(w.births.release(w.browser), timeout=5)

    assert w.left_running() == set()
    assert_forgotten(w)


# ── a loss beside a release ──────────────────────────────────────────────────


@pytest.mark.parametrize("shutdown", [False, True], ids=["release", "release_all"])
async def test_a_loss_while_the_release_waits_for_the_lock_starts_no_attach(w: Watched, shutdown: bool) -> None:
    """Held as another browser's attach holds it. The session stays watched throughout: the release alone is why."""
    async with w.births._lock:
        release = asyncio.ensure_future(w.births.release_all() if shutdown else w.births.release(w.browser))
        await settle()
        assert not release.done()
        await w.lose()
    await release

    assert w.left_running() == set()
    await settle()
    assert w.attaches.asked == 0
    assert_forgotten(w)


# ── on the real clock ────────────────────────────────────────────────────────


async def test_on_the_real_clock_a_failed_re_attach_is_followed_by_one_that_attaches(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The wait really outlasts the backoff: one refusal is logged, the browser's, and the next attach attaches."""
    caplog.set_level(logging.WARNING, logger=wb.logger.name)
    monkeypatch.setattr(wb, "ATTACH_RETRY_BACKOFF_SECONDS", 0.5)
    w = await Watched().start()
    try:
        started = time.monotonic()
        w.attaches.refuse = 1
        w.browser.cdp.fire("close", {})

        while not w.births.active_for(w.session):
            assert time.monotonic() - started < 30, "the watcher was not attached again"
            await asyncio.sleep(0.02)

        assert w.attaches.asked == 2
        assert len(refusals(caplog)) == 1 and "the browser was busy" in refusals(caplog)[0]
    finally:
        await w.births.release_all()

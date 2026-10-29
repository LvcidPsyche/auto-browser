"""Page calls the controller makes are bounded, and a drag lets go of the button (round 4, Part B).

A renderer stuck in a script never answers, and a call made under the session lock that waits for
it stalls every action on the session. Fakes stand in for the page, so each case is the same on every run.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from app.browser.element_target import ElementTarget
from app.browser.page_read import bounded
from app.browser.services import tabs as tabs_module
from app.cdp.passthrough import CDPPassthrough
from app.routes.extensions import cdp as cdp_routes
from app.tool_gateway import drag as drag_module
from app.tool_inputs import DragDropInput, EvalJsInput
from tests.test_allowed_hosts_reads import gateway_for, manager_for, read_session
from tests.test_allowed_hosts_rechecks import ALLOWED, REFUSED, LockCheckingCdp, MovingPage, cdp_client

# A call that never answers sleeps this long; every bound under test is far shorter.
HANG_SECONDS = 30.0
# The general bound on a page call under the lock, shortened for these tests, and the most a test waits.
PAGE_BOUND_SECONDS = 1.5
TEST_BOUND_SECONDS = 5.0
# What a bounded hold may run over its bounds by: the event loop's turns and the clock's resolution.
HOLD_SLACK_SECONDS = 1.0


async def hang(*_args, **_kwargs) -> None:
    await asyncio.sleep(HANG_SECONDS)


def still_session(tmp_path: Path):
    manager = manager_for(tmp_path)
    session = read_session(manager, "returns", tmp_path)
    session.page.move = None
    return manager, session


# ── eval_js (B2) ──


async def test_eval_js_gives_up_at_its_own_timeout_and_says_the_expression_may_still_run(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", PAGE_BOUND_SECONDS)
    manager, session = still_session(tmp_path)
    session.page.evaluate = hang

    with pytest.raises(RuntimeError, match="may still be running in the page"):
        await asyncio.wait_for(
            gateway_for(manager)._eval_js(EvalJsInput(session_id="s1", expression="for(;;){}", timeout_ms=200)),
            timeout=TEST_BOUND_SECONDS,
        )

    assert not session.lock.locked()


async def test_eval_js_without_a_timeout_gives_up_at_the_default_one(tmp_path: Path, monkeypatch) -> None:
    """F9: the default bound (EVAL_JS_DEFAULT_TIMEOUT_MS) applies when timeout_ms is left out."""
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", PAGE_BOUND_SECONDS)
    monkeypatch.setattr("app.tool_gateway.gateway.EVAL_JS_DEFAULT_TIMEOUT_MS", 200)
    manager, session = still_session(tmp_path)
    session.page.evaluate = hang

    with pytest.raises(RuntimeError, match="may still be running in the page"):
        await asyncio.wait_for(
            gateway_for(manager)._eval_js(EvalJsInput(session_id="s1", expression="for(;;){}")),
            timeout=TEST_BOUND_SECONDS,
        )

    assert not session.lock.locked()


def test_eval_js_waits_at_most_a_minute() -> None:
    assert EvalJsInput(session_id="s1", expression="1", timeout_ms=60_000).timeout_ms == 60_000
    with pytest.raises(ValidationError):
        EvalJsInput(session_id="s1", expression="1", timeout_ms=60_001)


# ── drag lets go of the button once it is down (B2) ──


def drag_payload() -> DragDropInput:
    return DragDropInput(session_id="s1", source_x=1, source_y=1, target_x=50, target_y=50)


async def test_a_drag_whose_move_fails_still_releases_the_button(tmp_path: Path) -> None:
    manager, session = still_session(tmp_path)
    mouse = session.page.mouse
    mouse.move = AsyncMock(side_effect=[None, PlaywrightError("Target page, context or browser has been closed")])

    with pytest.raises(PlaywrightError, match="closed"):
        await gateway_for(manager)._drag_drop(drag_payload())

    mouse.down.assert_awaited_once()
    mouse.up.assert_awaited_once()


async def test_a_drag_cut_off_by_its_deadline_still_releases_the_button(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", 0.2)
    manager, session = still_session(tmp_path)
    mouse = session.page.mouse
    moves: list[tuple] = []

    async def move(*args, **_kwargs) -> None:
        moves.append(args)
        if len(moves) > 1:  # the move with the button down
            await hang()

    mouse.move = AsyncMock(side_effect=move)

    with pytest.raises(RuntimeError, match="did not answer"):
        await asyncio.wait_for(gateway_for(manager)._drag_drop(drag_payload()), timeout=TEST_BOUND_SECONDS)

    mouse.up.assert_awaited_once()


async def test_a_button_release_that_never_answers_is_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(drag_module, "MOUSE_UP_TIMEOUT_SECONDS", 0.2, raising=False)
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", PAGE_BOUND_SECONDS)
    manager, session = still_session(tmp_path)
    session.page.mouse.up = hang

    with pytest.raises(RuntimeError, match="not released"):
        await asyncio.wait_for(gateway_for(manager)._drag_drop(drag_payload()), timeout=TEST_BOUND_SECONDS)

    assert not session.lock.locked()


class Button:
    """The page's mouse button: down from down() until an up() that is not cut off has finished.

    It records whether the session lock was held at each up(): a release outside it lets the page guard,
    or another action, take the session with the button still pressed.
    """

    def __init__(
        self, mouse: SimpleNamespace, lock: asyncio.Lock, *, up_seconds: float = 0.0, down_hangs: bool = False
    ) -> None:
        self.down = False
        self.ups = 0
        self.locked_at_up: list[bool] = []
        self.up_started = asyncio.Event()
        self.up_seconds = up_seconds
        self.down_hangs = down_hangs
        self._lock = lock
        mouse.down = self._down
        mouse.up = self._up

    async def _down(self, *_args, **_kwargs) -> None:
        self.down = True
        if self.down_hangs:
            await hang()

    async def _up(self, *_args, **_kwargs) -> None:
        self.ups += 1
        self.locked_at_up.append(self._lock.locked())
        self.up_started.set()
        await asyncio.sleep(self.up_seconds)
        self.down = False


async def drag_outcome(manager) -> str:
    """How the drag answered: "returned", or the text of the RuntimeError it raised."""
    try:
        await asyncio.wait_for(gateway_for(manager)._drag_drop(drag_payload()), timeout=TEST_BOUND_SECONDS)
    except RuntimeError as exc:
        return str(exc)
    return "returned"


async def test_a_drag_whose_deadline_expires_while_the_button_is_let_go_still_lets_it_go(
    tmp_path: Path, monkeypatch
) -> None:
    """The drag's work ended in time, and its deadline expired while mouse.up() was in flight: that
    cancelled the release, and the page kept the button pressed for whatever ran next."""
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", 0.2)
    manager, session = still_session(tmp_path)
    button = Button(session.page.mouse, session.lock, up_seconds=0.5)

    outcome = await drag_outcome(manager)

    assert (button.down, button.ups, button.locked_at_up) == (False, 1, [True]), outcome
    assert outcome == "returned"
    assert not session.lock.locked()


async def test_a_drag_whose_deadline_expires_while_the_button_goes_down_still_lets_it_go(
    tmp_path: Path, monkeypatch
) -> None:
    """mouse.down() may have pressed the button before it stopped answering."""
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", 0.2)
    manager, session = still_session(tmp_path)
    button = Button(session.page.mouse, session.lock, down_hangs=True)

    outcome = await drag_outcome(manager)

    assert (button.down, button.ups, button.locked_at_up) == (False, 1, [True]), outcome
    assert "did not answer" in outcome
    assert not session.lock.locked()


async def test_a_drag_cancelled_while_the_button_is_let_go_still_lets_it_go_under_the_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """Round 4d (R4C-L1-6): the client cancels the drag just as it finishes, with mouse.up() in flight: the
    cancel stopped the wait for the release, and the button could stay pressed."""
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", PAGE_BOUND_SECONDS)
    manager, session = still_session(tmp_path)
    button = Button(session.page.mouse, session.lock, up_seconds=0.3)
    drag = asyncio.ensure_future(gateway_for(manager)._drag_drop(drag_payload()))
    await asyncio.wait_for(button.up_started.wait(), timeout=TEST_BOUND_SECONDS)

    drag.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(drag, timeout=TEST_BOUND_SECONDS)

    assert (button.down, button.ups, button.locked_at_up) == (False, 1, [True])
    assert not session.lock.locked()


async def test_a_failed_drag_cancelled_while_the_button_is_let_go_still_lets_it_go_under_the_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """Round 4e (Codex 3): the move with the button down failed, and the client cancelled while the release
    after that failure was in flight. That release was not carried through a cancel as the success path's
    is, so the button stayed pressed once the session was let go."""
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", PAGE_BOUND_SECONDS)
    manager, session = still_session(tmp_path)
    session.page.mouse.move = AsyncMock(side_effect=[None, PlaywrightError("Target crashed")])
    button = Button(session.page.mouse, session.lock, up_seconds=0.3)
    drag = asyncio.ensure_future(gateway_for(manager)._drag_drop(drag_payload()))
    await asyncio.wait_for(button.up_started.wait(), timeout=TEST_BOUND_SECONDS)

    drag.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(drag, timeout=TEST_BOUND_SECONDS)

    assert (button.down, button.ups, button.locked_at_up) == (False, 1, [True])
    assert not session.lock.locked()


class StuckEnd(ElementTarget):
    """An end named by element_id on a renderer that stopped answering while it was brought into view."""

    def __init__(self, element_id: str) -> None:
        self.element_id = element_id

    async def wait_until_actionable(self, **_kwargs) -> None:
        await hang()


async def test_a_drag_cut_off_while_an_end_comes_into_view_holds_the_session_a_bounded_time(
    tmp_path: Path, monkeypatch
) -> None:
    """Round 4d (R4C-L1-7): after the deadline, removing the scroll watch had no bound of its own, so on a
    stuck renderer the session stayed held for the world's own timeout (15 s, sent twice), past the
    deadline and the release bound the CHANGELOG gives."""
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(drag_module, "UNWATCH_TIMEOUT_SECONDS", 0.2, raising=False)
    manager, session = still_session(tmp_path)

    async def evaluate(_page, script: str, _arg) -> dict:
        if script == drag_module._UNWATCH_SCRIPT:
            await hang()
        return {"x": 0, "y": 0}

    manager.element_ids.evaluate = evaluate
    view = drag_module._bring_element_ends_into_view(manager, session, StuckEnd("e1"), (50.0, 50.0), ["target"])
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="did not answer"):
        await asyncio.wait_for(bounded(view), timeout=TEST_BOUND_SECONDS)

    assert time.monotonic() - started < 0.2 + 0.2 + HOLD_SLACK_SECONDS


# ── Titles and the bot-challenge probe under the session lock (B3) ──

# How long a test lets an observation run before it counts as stalled.
OBSERVE_WAIT_SECONDS = 2.0


class StuckPage:
    """A tab whose renderer is stuck in a script: its title() never answers."""

    url = ALLOWED

    def __init__(self) -> None:
        self.titles = 0

    async def title(self) -> str:
        self.titles += 1
        await hang()
        return "never"

    async def evaluate(self, *_args) -> None:
        await hang()

    def is_closed(self) -> bool:
        return False


def observed_session(manager, *pages) -> SimpleNamespace:
    """A session on pages[0] whose observation reads are stubbed; tab titles go through the real tab list."""
    session = SimpleNamespace(
        id="s1",
        page=pages[0],
        context=SimpleNamespace(pages=list(pages)),
        attached_pages=set(),
        console_messages=[],
        page_errors=[],
        request_failures=[],
        downloads=[],
    )
    manager.element_ids.evaluate = AsyncMock(return_value=[])
    manager.observation.page_summary = AsyncMock(
        return_value={
            "title": "active",
            "active_element": None,
            "text_excerpt": "",
            "dom_outline": {},
            "accessibility_outline": {},
        }
    )
    manager._session_summary = AsyncMock(return_value={"id": "s1"})
    manager._current_takeover_url = lambda _session: None
    manager.remote_access = SimpleNamespace(session_info=lambda _session: {})
    return session


async def finishes(call) -> object:
    """The call's result, or an assertion failure if it is still running after OBSERVE_WAIT_SECONDS."""
    task = asyncio.ensure_future(call)
    done, _ = await asyncio.wait({task}, timeout=OBSERVE_WAIT_SECONDS)
    if not done:
        task.cancel()
    assert done, "the call waited on a page that never answers"
    return task.result()


async def test_a_tab_whose_title_never_answers_does_not_stall_observe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tabs_module, "TAB_TITLE_TIMEOUT_SECONDS", 0.2, raising=False)
    manager = manager_for(tmp_path)
    session = observed_session(manager, MovingPage(ALLOWED, ALLOWED, title="active"), StuckPage())

    observed = await finishes(manager.observation.observation_payload(session, preset="text"))

    assert [tab["title"] for tab in observed["tabs"]] == ["active", ""]


async def test_the_title_of_a_tab_refused_before_the_read_is_not_read(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    stuck = StuckPage()
    manager.page_provenance.committed(stuck, REFUSED, url_refused=True)
    session = observed_session(manager, MovingPage(ALLOWED, ALLOWED, title="active"), stuck)

    tabs = await finishes(manager.tabs.summaries(session))

    assert stuck.titles == 0
    assert [tab["title"] for tab in tabs] == ["active", ""]


async def test_the_fast_presets_title_is_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", 0.2)
    manager = manager_for(tmp_path)
    session = observed_session(manager, StuckPage())
    shot = {"path": str(tmp_path / "fast.png"), "url": "/artifacts/s1/fast.png"}
    manager.observation._capture_screenshot_redacted = AsyncMock(return_value=shot)

    async def observe() -> str:
        try:
            await manager.observation.observation_payload(session, preset="fast")
        except RuntimeError as exc:
            return str(exc)
        return "answered"

    assert "did not answer" in await finishes(observe())


async def test_a_bot_challenge_probe_that_never_answers_counts_as_no_challenge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.browser.page_read.PAGE_READ_TIMEOUT_SECONDS", 0.2)
    manager = manager_for(tmp_path)
    session = observed_session(manager, StuckPage())

    assert await finishes(manager._check_bot_challenge(session)) is None


# ── CDP attach and detach under the session lock (B4) ──

# The CDP routes' bound, shortened for these tests; a stuck attach or detach answers after CDP_HANG_SECONDS.
CDP_BOUND_SECONDS = 0.2
CDP_HANG_SECONDS = 3.0


def tab_switched_cdp_client(tmp_path: Path, monkeypatch, *, attach_hangs: bool):
    """A CDP route client whose passthrough is still on the tab the session left, so the route re-attaches."""
    monkeypatch.setattr(cdp_routes, "CDP_TIMEOUT_SECONDS", CDP_BOUND_SECONDS)
    client, cdp = cdp_client(tmp_path, ALLOWED)
    cdp.page = SimpleNamespace(url=ALLOWED)

    async def from_page(page):
        if attach_hangs:
            await asyncio.sleep(CDP_HANG_SECONDS)
        return LockCheckingCdp(cdp.session)

    async def detach() -> None:
        if not attach_hangs:
            await asyncio.sleep(CDP_HANG_SECONDS)

    monkeypatch.setattr(CDPPassthrough, "from_page", from_page)
    cdp.detach = detach
    return client, cdp


@pytest.mark.parametrize("attach_hangs", [True, False], ids=["attach", "detach"])
def test_a_cdp_attach_or_detach_that_never_answers_is_504_and_lets_go_of_the_session(
    tmp_path: Path, monkeypatch, attach_hangs: bool
) -> None:
    client, cdp = tab_switched_cdp_client(tmp_path, monkeypatch, attach_hangs=attach_hangs)

    response = client.get("/sessions/s1/cdp/element?selector=p")

    assert response.status_code == 504, response.text
    assert cdp.session.lock.held is False

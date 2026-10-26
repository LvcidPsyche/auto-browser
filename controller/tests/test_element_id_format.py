"""An element_id is checked against the exact form observe issues before anything looks it up.

The id used to go into a CSS selector unescaped, so `x"], #pay, [data-x="`
named any element on the page. Now only `op-` and 24 lowercase hex digits get
as far as a lookup, and a selector_hint used as a selector is read as the id it
names. These need no browser; tests/test_element_ids.py runs the same rules
against real Chromium.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import Error as PlaywrightError

from app.action_errors import BrowserActionError
from app.browser import element_scripts, element_world
from app.browser.element_ids import ElementIds, selector_hint, target_element_id, validate_element_id
from app.browser.element_target import ElementTarget
from app.browser.services.actions import BrowserActionService
from tests.test_browser_scripts_syntax import NODE, _node_check

ISSUED = "op-0123456789abcdef01234567"
_SETTINGS = SimpleNamespace(action_timeout_ms=200)


@pytest.mark.parametrize("element_id", [ISSUED, "op-" + "f" * 24])
def test_an_id_in_the_issued_form_is_accepted(element_id: str) -> None:
    assert validate_element_id(element_id) == element_id


@pytest.mark.parametrize(
    "element_id",
    [
        "op-1a2b3c4d",  # the form observe issued before
        "op-email",
        "op-" + "A" * 24,
        "op-" + "0" * 23,
        "op-" + "0" * 25,
        ISSUED + "\n",
        " " + ISSUED,
        'x"], #pay, [data-x="',
        ISSUED + '"], #pay, [x="',
        "",
        None,
        7,
    ],
)
def test_anything_else_is_refused_as_invalid(element_id: object) -> None:
    with pytest.raises(BrowserActionError) as raised:
        validate_element_id(element_id)

    assert raised.value.code == "invalid_element_id"
    assert raised.value.retryable is False
    assert "browser.observe" in raised.value.message


def test_a_selector_hint_used_as_a_selector_names_its_element_id() -> None:
    assert target_element_id(selector=selector_hint(ISSUED), element_id=None) == ISSUED


def test_a_selector_hint_holding_a_malformed_id_is_refused() -> None:
    """The old ids' hint form names no element any more: it is refused, not matched against the page."""
    with pytest.raises(BrowserActionError) as raised:
        target_element_id(selector='[data-operator-id="op-1a2b3c4d"]', element_id=None)
    assert raised.value.code == "invalid_element_id"
    with pytest.raises(BrowserActionError):
        target_element_id(selector='[data-operator-id=""]', element_id=None)


@pytest.mark.parametrize(
    "selector",
    ["#pay", "button[data-operator-id]", f"[data-operator-id='{ISSUED}']", f'button[data-operator-id="{ISSUED}"]'],
)
def test_other_selectors_are_left_as_selectors(selector: str) -> None:
    assert target_element_id(selector=selector, element_id=None) is None


def test_an_element_id_wins_over_a_selector_given_with_it() -> None:
    assert target_element_id(selector="#pay", element_id=ISSUED) == ISSUED


def test_a_target_records_the_element_id_and_its_hint() -> None:
    expected = {"mode": "selector", "element_id": ISSUED, "selector": selector_hint(ISSUED)}

    assert BrowserActionService.resolve_target(element_id=ISSUED) == expected
    assert BrowserActionService.resolve_target(selector=selector_hint(ISSUED)) == expected
    assert BrowserActionService.resolve_target(selector="#go") == {"mode": "selector", "selector": "#go"}
    with pytest.raises(BrowserActionError):
        BrowserActionService.resolve_target(element_id='x"], #pay, [data-x="')


class _Untouchable:
    """A page that fails the test if anything reaches it."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the page was used ({name}) for a malformed element_id")


async def test_a_malformed_id_is_refused_before_the_page_is_touched() -> None:
    with pytest.raises(BrowserActionError) as raised:
        ElementIds(_SETTINGS).element(_Untouchable(), 'x"], #pay, [data-x="')

    assert raised.value.code == "invalid_element_id"
    assert "is not one browser.observe issued" in raised.value.message


# ── The world's CDP session ──────────────────────────────────────────────────


class _Session:
    """A CDP session that knows the frame tree and, unless told otherwise, never answers anything else."""

    def __init__(self, answer=None) -> None:
        self.sent: list[str] = []
        self._answer = answer

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append(method)
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F"}}}
        if self._answer is None:
            await asyncio.Event().wait()
        return self._answer(method)

    async def detach(self) -> None:
        self.sent.append("detach")


class _Page:
    def __init__(self, answer=None) -> None:
        self.sessions: list[_Session] = []
        self.context = self
        self._answer = answer

    async def new_cdp_session(self, page: object) -> _Session:
        self.sessions.append(_Session(self._answer))
        return self.sessions[-1]


async def test_a_page_that_never_answers_fails_within_the_action_timeout_and_gets_a_new_session() -> None:
    ids = ElementIds(_SETTINGS)
    page = _Page()

    for _ in range(2):
        with pytest.raises(BrowserActionError) as raised:
            await asyncio.wait_for(ids.evaluate(page, "(arg) => arg"), 5)
        assert raised.value.code == "page_not_responding" and raised.value.retryable is False
        assert "did not answer within 200 ms" in raised.value.message

    assert len(page.sessions) == 2


async def test_a_session_dropped_after_a_timeout_is_detached() -> None:
    """Each timeout used to leave its CDP session open for the life of the page."""
    ids = ElementIds(_SETTINGS)
    page = _Page()
    detached: list[_Session] = []

    async def detach(self) -> None:
        detached.append(self)
        await asyncio.Event().wait()  # a hung renderer never answers this either

    with patch.object(_Session, "detach", detach, create=True):
        with pytest.raises(BrowserActionError):
            await asyncio.wait_for(ids.evaluate(page, "(arg) => arg"), 5)
        for _ in range(3):
            await asyncio.sleep(0)

    assert detached == page.sessions


async def test_a_session_attached_after_its_deadline_is_detached_when_it_arrives() -> None:
    """Opus 12: an attach that completed after its timeout left that session open for the life of the page."""
    late = _Session()
    detached: list[_Session] = []

    class _SlowPage(_Page):
        async def new_cdp_session(self, page: object) -> _Session:
            await asyncio.sleep(0.4)  # past the 200 ms deadline
            return late

    async def detach(self) -> None:
        detached.append(self)

    with patch.object(_Session, "detach", detach):
        with pytest.raises(BrowserActionError) as raised:
            await ElementIds(_SETTINGS).evaluate(_SlowPage(), "(arg) => arg")
        assert raised.value.code == "page_not_responding"
        await asyncio.sleep(0.5)
        for _ in range(3):
            await asyncio.sleep(0)

    assert detached == [late]


async def test_a_session_whose_attach_was_cancelled_is_detached_when_it_arrives() -> None:
    """Codex F6: only a timed-out attach was detached once it completed; a cancelled one stayed open."""
    late = _Session()
    detached: list[_Session] = []

    class _SlowPage(_Page):
        async def new_cdp_session(self, page: object) -> _Session:
            await asyncio.sleep(0.1)  # inside the 200 ms deadline
            return late

    async def detach(self) -> None:
        detached.append(self)

    with patch.object(_Session, "detach", detach):
        call = asyncio.ensure_future(ElementIds(_SETTINGS).evaluate(_SlowPage(), "(arg) => arg"))
        await asyncio.sleep(0.02)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.sleep(0.2)
        for _ in range(3):
            await asyncio.sleep(0)

    assert detached == [late]


async def test_a_late_failure_on_a_dropped_session_leaves_the_current_one_alone() -> None:
    """Opus 13: a call still in flight on a session already dropped reset the one another call had opened."""

    class _Gone:
        async def send(self, method: str, params: dict | None = None) -> dict:
            raise PlaywrightError("Target page, context or browser has been closed")

        async def detach(self) -> None:
            pass

    world = element_world.PageWorld(_SETTINGS)
    dropped, current = _Gone(), _Session()
    world._cdp, world._frame_id = current, "F"

    with pytest.raises(PlaywrightError):
        await world._send(dropped, "Runtime.callFunctionOn")
    with pytest.raises(BrowserActionError):
        await world._within(asyncio.Event().wait(), "Runtime.callFunctionOn", 50, dropped)

    assert world._cdp is current and world._frame_id == "F"


async def test_a_call_that_may_have_changed_the_page_is_not_sent_again() -> None:
    def closed(method: str) -> dict:
        raise PlaywrightError("Target page, context or browser has been closed")

    page = _Page(closed)
    world = ElementIds(_SETTINGS)._world(page)

    with pytest.raises(PlaywrightError):
        await world.call(page, "() => 1", effect=element_world.CHANGES_PAGE)
    for effect in (element_world.READ, element_world.IDEMPOTENT):
        with pytest.raises(PlaywrightError):
            await world.call(page, "() => 1", effect=effect)

    sent = [method for session in page.sessions for method in session.sent if method == "Page.createIsolatedWorld"]
    assert len(sent) == 5, "a read or an idempotent change is sent once more on a new session; anything else is not"
    assert len(page.sessions) == 5


def test_a_refusal_repeats_a_bounded_part_of_whatever_describes_the_cover() -> None:
    """Opus 5, Codex 4: the world bounds each part it reads, and the refusal bounds the whole again."""
    target = ElementTarget(None, None, ISSUED)  # type: ignore[arg-type]
    cover = {"tag": "x" * 5000, "label": True, "labelSelector": 'label[for="' + "\\:" * 2500 + '"]'}

    refused = target.refusal("element_covered", "clicked", cover=cover)

    assert len(refused.message) < 600, len(refused.message)


def test_every_world_call_names_its_effect() -> None:
    """Codex 7: what may be sent twice is a decision at each caller, not a default."""
    signature = inspect.signature(element_world.PageWorld.call)

    assert signature.parameters["effect"].default is inspect.Parameter.empty
    assert signature.parameters["effect"].kind is inspect.Parameter.KEYWORD_ONLY


async def test_rest_answers_a_page_that_stopped_answering_with_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fable F7: a world timeout reached REST raw, as a 500 "Internal error"."""
    import app.main as main_module

    async def hangs(*_args, **_kwargs):
        return await ElementIds(_SETTINGS).evaluate(_Page(), "(arg) => arg")

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        for service in (main_module.manager, main_module.job_queue, main_module.cron_service, main_module.maintenance):
            for name in ("startup", "shutdown"):
                stack.enter_context(patch.object(service, name, new=AsyncMock()))
        stack.enter_context(patch.object(main_module.manager, "click", new=AsyncMock(side_effect=hangs)))
        client = stack.enter_context(TestClient(main_module.app, raise_server_exceptions=False))

        response = client.post("/sessions/s-1/actions/click", json={"selector": "#a"})

    assert response.status_code == 504
    assert response.json()["code"] == "page_not_responding"
    assert "did not answer within 200 ms" in response.json()["error"]


# ── The registry ─────────────────────────────────────────────────────────────


def _run_node(source: str) -> dict:
    result = subprocess.run(  # noqa: S603 - fixed argv
        [NODE, "-e", source], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_pruning_the_registry_costs_amortised_constant_work_per_id() -> None:
    """Every id issued past 4,096 rescanned the whole map, so a page that kept its nodes cost quadratic time."""
    out = _run_node(
        """
let derefs = 0;
globalThis.WeakRef = class {
  constructor(target) { this.target = target; }
  deref() { derefs += 1; return this.target.dead ? undefined : this.target; }
};
globalThis.document = {};
globalThis.Node = class {};
Object.defineProperty(Node.prototype, 'isConnected', { get() { return true; } });
Object.defineProperty(Node.prototype, 'ownerDocument', { get() { return document; } });
const node = (fields) => Object.assign(Object.create(Node.prototype), fields);
const registry = """
        + element_world.ELEMENT_REGISTRY_SCRIPT
        + """;
const first = [];
for (let i = 0; i < 4096; i += 1) {
  const el = node({ dead: i % 2 === 1 });
  first.push([el, registry.issue(el)]);
}
derefs = 0;
for (let i = 0; i < 8192; i += 1) registry.issue(node({ dead: false }));
const issuing = derefs;
const status = (dead) => [...new Set(first.filter(([el]) => el.dead === dead).map(([, id]) => registry.find(id).status))];
console.log(JSON.stringify({ issuing, live: status(false), dead: status(true) }));
"""
    )

    assert out["issuing"] <= 4 * 8192, out
    assert out["live"] == ["ok"] and out["dead"] == ["unknown"], out


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_a_global_of_the_registry_name_that_is_not_the_registry_is_refused() -> None:
    """Codex 9: only the object the world defined is the registry, even as an own property."""
    out = _run_node(
        """
globalThis.__autoBrowserElementIds = { issue: () => 'op-' + '0'.repeat(24), tag: 'forged' };
let refused = null;
try { """
        + element_world.ELEMENT_REGISTRY_SCRIPT
        + """; } catch (error) { refused = String(error.message); }
console.log(JSON.stringify({ refused }));
"""
    )

    assert out["refused"] and "not the controller's" in out["refused"], out


# _SETTLING_JS with a fake clock, animation frames and timer: each case calls
# settled() once per check and wakes the next check by a frame or by the timer.
_SETTLING_CASES = """
const clock = { now: 0 };
const frameQueue = [];
const timerQueue = [];
const performance = { now: () => clock.now };
const requestAnimationFrame = (callback) => frameQueue.push(callback);
const setTimeout = (callback) => timerQueue.push(callback);
const make = SETTLING;
const box = { x: 1, y: 2, width: 3, height: 4 };
const moved = { x: 9, y: 2, width: 3, height: 4 };
const wake = async (settling, by) => {
  const next = settling.next();
  if (by === 'frame') {
    clock.now += 16;
    frameQueue.splice(0).forEach((callback) => callback());
  } else {
    clock.now += 50;
    timerQueue.splice(0).forEach((callback) => callback());
  }
  await next;
};
const checks = async (steps) => {
  clock.now = 0;
  const settling = make();
  const seen = [];
  for (const [by, at] of steps) {
    if (by) await wake(settling, by);
    seen.push(settling.settled(at));
  }
  return seen;
};
(async () => {
  const timerSteps = Array.from({ length: 30 }, () => ['timer', box]);
  console.log(JSON.stringify({
    framesThenTimers: await checks([[null, box], ['frame', box], ['timer', box], ['timer', box], ['frame', box]]),
    afterACheckOutsideAFrame: await checks([[null, box], ['frame', box]]),
    movedBetweenFrames: await checks([[null, box], ['frame', box], ['timer', moved], ['frame', box]]),
    noFrameAtAll: await checks([[null, box], ...timerSteps]),
    framesThatStop: await checks([[null, box], ['frame', box], ...timerSteps]),
  }));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_a_box_settles_only_across_animation_frames_or_after_a_second_without_any() -> None:
    """Round 4b: a check woken by the 50 ms timer read the box of the frame before, so a moving element on a
    slow renderer counted as stable."""
    out = _run_node(_SETTLING_CASES.replace("SETTLING", element_scripts._SETTLING_JS))

    # Two checks in consecutive frames settle; checks woken by the timer between them never do.
    assert out["framesThenTimers"] == [False, False, False, False, True]
    # A check made outside a frame is not the first of the two.
    assert out["afterACheckOutsideAFrame"] == [False, False]
    # A box that moved between two frames (seen by the timer's check) starts over, even if it came back.
    assert out["movedBetweenFrames"] == [False, False, False, False]
    # With no frame at all, checks a timer apart count once the wait has gone 1 s (the 20th wake) without one.
    assert out["noFrameAtAll"].index(True) == 20 and not any(out["noFrameAtAll"][:20])
    # Once frames have come, timers never settle a box, however long the wait.
    assert not any(out["framesThatStop"])


ISOLATED_WORLD_SCRIPTS = sorted(
    (module.__name__.rsplit(".", 1)[-1], name)
    for module in (element_world, element_scripts)
    for name in dir(module)
    if name.endswith("_SCRIPT") and isinstance(getattr(module, name), str)
)


def test_the_isolated_world_scripts_were_discovered() -> None:
    names = {name for _module, name in ISOLATED_WORLD_SCRIPTS}
    assert {
        "ELEMENT_REGISTRY_SCRIPT",
        "ELEMENT_ACTIONABLE_SCRIPT",
        "ELEMENT_COVER_SCRIPT",
        "ELEMENT_SELECT_SCRIPT",
        "UPLOAD_MARK_SCRIPT",
    } <= names


@pytest.mark.skipif(NODE is None, reason="node not available")
@pytest.mark.parametrize(("module", "name"), ISOLATED_WORLD_SCRIPTS)
def test_the_isolated_world_scripts_are_valid_javascript(module: str, name: str) -> None:
    source = getattr({"element_world": element_world, "element_scripts": element_scripts}[module], name)
    ok, stderr = _node_check(f"void (\n{source}\n);")
    assert ok, f"{name} is not valid JavaScript:\n{stderr}"


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_the_functions_sent_to_the_isolated_world_are_valid_javascript() -> None:
    for declaration in (
        element_world.observation_declaration("(arg, elementIds) => arg"),
        element_world.element_declaration(element_scripts.ELEMENT_ACTIONABLE_SCRIPT),
        element_world.element_declaration(element_scripts.UPLOAD_LANDED_SCRIPT, attached=False),
    ):
        ok, stderr = _node_check(f"void (\n{declaration}\n);")
        assert ok, f"not valid JavaScript:\n{stderr}\n{declaration}"

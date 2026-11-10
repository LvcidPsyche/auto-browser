"""STORE-42 against real Chromium: a fast-profile click asks for approval by what it lands on, not by its label.

The fast profile asked for approval only when the decision's own risk_category said post, payment, account_change
or destructive, and that label is a model's reading of the page it acts on. These run execute_decision, the fast
path every agent step, browser.execute_action and REST decision goes through, on pages served locally, with the
target read as the click will find it (app/browser/click_target.py). Same harness as test_element_ids.py.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any

import pytest

from app.action_errors import PAGE_NOT_RESPONDING, BrowserActionError
from app.approvals import ApprovalRequiredError, held_for_execution
from app.browser.click_target import CLICK_TARGET_SCRIPT, READ_TIMEOUT_MS
from app.browser.services import actions as actions_module
from app.models import BrowserActionDecision
from tests.test_element_ids import _shared_chromium, browser_env, pytestmark  # noqa: F401

PAGE = """<title>start</title>
<button id="delete" onclick="document.title='DELETED'">Delete account</button>
<button id="close" onclick="document.title='CLOSED'">Close</button>
<div id="pay" role="button" onclick="document.title='PAID'"><span>Pay now</span></div>
<a id="article" href="/article">Delete your account: how it works</a>
<form id="search" action="/results"><input name="q" value="shoes"><button id="go">Search</button></form>
"""
# A 3 px control of its own inside a button: by its side, every way, is "Delete account".
SMALL_CONTROL = """<title>start</title>
<style>
  body { margin: 0 }
  #delete { position: absolute; left: 40px; top: 40px; width: 240px; height: 80px }
  #dot { position: absolute; left: 120px; top: 30px; width: 3px; height: 3px; background: #000 }
</style>
<button id="delete" onclick="document.title = 'DELETED'">Delete account<span id="dot"
  onclick="event.stopPropagation(); window.dots = (window.dots || 0) + 1"></span></button>
"""
# Buttons whose text is more than a name: a sentence under it, an icon font's ligature before it.
WORDY = """<title>start</title>
<button id="two" onclick="document.title='GONE'"><strong>Delete account</strong><br>
  <small>This permanently removes your profile and all of its data</small></button>
<button id="icon" onclick="document.title='PAID'"><span class="material-icons">credit_card</span> Pay now</button>
<button id="code" onclick="document.title='SENT'">Send code</button>
"""
# A frame of the page's own origin inside another, each with a border and padding to cross, and beside them a
# frame the page cannot reach into (sandboxed, so of an origin of its own).
FRAMES = """<title>start</title>
<style>
  body { margin: 0 }
  iframe { position: absolute; left: 60px; width: 380px; border: 6px solid #888; padding: 5px }
  #outer { top: 30px; height: 220px }
  #apart { top: 300px; height: 80px }
</style>
<iframe id="outer"></iframe>
<iframe id="apart" sandbox srcdoc="<button id='sealed'>Delete account</button>"></iframe>
<script>
document.getElementById('outer').srcdoc = `<body style="margin: 0">
  <iframe id="inner" style="margin: 12px; border: 3px solid #333; width: 300px; height: 110px"
    srcdoc="<button id='deep' style='margin: 20px' onclick=&quot;top.document.title = 'DELETED'&quot;>Delete
      account</button>"></iframe>
  <button id="shallow" style="display: block" onclick="top.document.title = 'CLOSED'">Close</button>`;
</script>
"""
# A button whose icon is a component with a shadow root of its own: the element at the icon is inside that root.
ICON_COMPONENT = """<title>start</title>
<button id="delete" onclick="document.title='DELETED'"><x-icon id="icon"></x-icon> Delete account</button>
<script>
document.getElementById('icon').attachShadow({mode: 'open'}).innerHTML =
  `<span class="glyph" style="display: inline-block; width: 24px; height: 24px; background: #c00"></span>`;
</script>
"""
ARTICLE = "<title>ARTICLE</title>"
RESULTS = "<title>RESULTS</title>"
# PAGE with a button before the others that only a selector piercing an open shadow root finds.
SHADOW = PAGE.replace(
    "</title>",
    """</title>
<div id="host"></div>
<script>
document.getElementById('host').attachShadow({mode: 'open'}).innerHTML =
  `<button class="wipe" onclick="document.title='WIPED'">Delete workspace</button>`;
</script>""",
)


def _click(risk: str, **target: Any) -> BrowserActionDecision:
    return BrowserActionDecision(action="click", reason="the model's word", risk_category=risk, **target)


async def _title(env, session_id: str) -> str:
    return await env.page(session_id).title()


def _record_witness_risk(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = actions_module.action_witness_context

    def recording(context: dict):
        seen.append(context["risk_category"])
        return real(context)

    monkeypatch.setattr(actions_module, "action_witness_context", recording)
    return seen


async def test_a_read_labelled_click_on_a_delete_button_asks_for_its_approval(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": PAGE}) as env:
        session_id = await env.create("/")
        delete = await env.element(session_id, "Delete account")

        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.execute_decision(session_id, _click("read", element_id=delete["element_id"]))

        approval = asked.value.approval
        assert (approval.kind, approval.action.risk_category) == ("destructive", "destructive")
        # The operator is told who chose the kind, before the account of the model that was steered.
        assert approval.reason == (
            'The controller reads this click\'s target as a button of kind "destructive"; the agent labelled the '
            "click \"read\". The agent's reason: the model's word"
        )
        assert await _title(env, session_id) == "start"

        # Approved, the same click spends it: its target reads the same, so it matches, whatever reason the
        # agent gives this time (an approval does not bind the reason).
        await env.manager.approve(approval.id)
        retried = _click("read", element_id=delete["element_id"]).model_copy(update={"reason": "another account"})
        await env.manager.execute_decision(session_id, retried, approval_id=approval.id)
        assert await _title(env, session_id) == "DELETED"
        assert (await env.manager.approvals.get(approval.id)).status == "executed"


async def test_a_write_labelled_click_by_selector_or_point_is_read_too(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": PAGE}) as env:
        session_id = await env.create("/")
        box = await env.page(session_id).locator("#pay span").bounding_box()

        for decision, kind in (
            (_click("write", selector="#delete"), "destructive"),
            (_click("write", x=box["x"] + box["width"] / 2, y=box["y"] + box["height"] / 2), "payment"),
        ):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, decision)
            assert asked.value.approval.kind == kind
        assert await _title(env, session_id) == "start"


async def test_a_selector_in_any_dialect_the_click_accepts_is_read(tmp_path: Path) -> None:
    # The click resolves its selector with Playwright, which reads more than CSS: a steered model has only to name
    # the button in another dialect.
    async with browser_env(tmp_path, {"/": SHADOW}) as env:
        session_id = await env.create("/")

        for selector in (
            "text=Delete account",
            'role=button[name="Delete account"]',
            "xpath=//button[@id='delete']",
            "button:has-text('Delete account')",
            "body >> #delete",
            ".wipe",
            # Playwright's first match is the shadow root's button, which comes first in the document;
            # document.querySelector never enters the shadow root, and finds Close.
            "#close, .wipe",
        ):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, _click("read", selector=selector))
            assert asked.value.approval.kind == "destructive", selector
        assert await _title(env, session_id) == "start"


async def test_a_selector_that_matches_nothing_is_not_classified_and_fails_as_the_click_does(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": PAGE}) as env:
        session_id = await env.create("/")
        loop = asyncio.get_running_loop()
        started = loop.time()

        with pytest.raises(BrowserActionError) as failed:
            await env.manager.execute_decision(session_id, _click("read", selector="text=No such button"))

        # The read gives up after READ_TIMEOUT_MS, and the click then waits its own ACTION_TIMEOUT_MS (3 s here).
        assert not isinstance(failed.value, ApprovalRequiredError)
        assert loop.time() - started < (READ_TIMEOUT_MS + 3000) / 1000 + 3
        assert await env.manager.approvals.list() == []
        assert await _title(env, session_id) == "start"


async def test_a_name_over_a_sentence_and_a_name_after_an_icons_ligature_are_read(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": WORDY}) as env:
        session_id = await env.create("/")

        for selector, kind in (("#two", "destructive"), ("#icon", "payment")):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, _click("read", selector=selector))
            assert asked.value.approval.kind == kind
        assert await _title(env, session_id) == "start"

        # A login form's "Send code" is not a post: it runs unattended.
        await env.manager.execute_decision(session_id, _click("write", selector="#code"))
        assert await _title(env, session_id) == "SENT"


async def _centre(locator) -> dict[str, float]:
    box = await locator.bounding_box()
    return {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}


async def test_a_point_inside_frames_of_the_pages_origin_is_read_there(tmp_path: Path) -> None:
    # The element at a point over a frame is the <iframe>, which is no button: the click was not classified.
    async with browser_env(tmp_path, {"/": FRAMES}) as env:
        session_id = await env.create("/")
        outer = env.page(session_id).frame_locator("#outer")
        deep = await _centre(outer.frame_locator("#inner").locator("#deep"))
        shallow = await _centre(outer.locator("#shallow"))

        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.execute_decision(session_id, _click("read", **deep))
        assert asked.value.approval.kind == "destructive"
        assert await _title(env, session_id) == "start"

        # One frame down, a harmless button runs as labelled.
        await env.manager.execute_decision(session_id, _click("write", **shallow))
        assert await _title(env, session_id) == "CLOSED"


async def test_an_element_in_a_shadow_root_inside_a_button_counts_as_the_button(tmp_path: Path) -> None:
    # closest() stops at a shadow root: the glyph of an icon component found no button around it, and a click
    # aimed at the icon of "Delete account" (by its point, or by a selector that pierces the root) was not read.
    async with browser_env(tmp_path, {"/": ICON_COMPONENT}) as env:
        session_id = await env.create("/")
        glyph = await _centre(env.page(session_id).locator("#icon .glyph"))

        for target in (glyph, {"selector": "#icon .glyph"}):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, _click("read", **target))
            assert asked.value.approval.kind == "destructive", target
        assert await _title(env, session_id) == "start"


async def test_a_point_inside_a_frame_of_another_origin_is_not_read(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": FRAMES}) as env:
        session_id = await env.create("/")
        sealed = await _centre(env.page(session_id).frame_locator("#apart").locator("#sealed"))

        # The page's own origin cannot see into it, so neither can the controller's world there: unread, the
        # click runs as labelled.
        await env.manager.execute_decision(session_id, _click("read", **sealed))

        assert await env.manager.approvals.list() == []


async def test_a_close_button_and_a_link_run_as_labelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    risks = _record_witness_risk(monkeypatch)
    async with browser_env(tmp_path, {"/": PAGE, "/article": ARTICLE}) as env:
        session_id = await env.create("/")

        await env.manager.execute_decision(session_id, _click("write", selector="#close"))
        assert await _title(env, session_id) == "CLOSED"
        await env.manager.execute_decision(session_id, _click("read", selector="#article"))
        await env.page(session_id).wait_for_url("**/article")

        assert risks == ["write", "read"]
        assert await env.manager.approvals.list() == []


async def test_a_read_labelled_form_submit_runs_recorded_as_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    risks = _record_witness_risk(monkeypatch)
    async with browser_env(tmp_path, {"/": PAGE, "/results": RESULTS}) as env:
        session_id = await env.create("/")

        await env.manager.execute_decision(session_id, _click("read", selector="#go"))

        await env.page(session_id).wait_for_url("**/results?q=shoes")
        assert risks == ["write"]
        assert await env.manager.approvals.list() == []


async def test_a_read_the_page_does_not_answer_does_not_stop_the_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with browser_env(tmp_path, {"/": PAGE}) as env:
        session_id = await env.create("/")
        delete = await env.element(session_id, "Delete account")
        evaluate = env.manager.element_ids.evaluate
        bounds: list[tuple[int, int, bool]] = []

        async def hung_read(page, script, arg=None, *, timeout_ms=None, setup_timeout_ms=None, keep_session=False):
            if script != CLICK_TARGET_SCRIPT:
                return await evaluate(page, script, arg, timeout_ms=timeout_ms, setup_timeout_ms=setup_timeout_ms)
            # What the world raises once a message's deadline passes with no answer (element_world.py).
            bounds.append((timeout_ms, setup_timeout_ms, keep_session))
            await asyncio.sleep(0.2)
            raise BrowserActionError("The page did not answer", code=PAGE_NOT_RESPONDING, retryable=False)

        monkeypatch.setattr(env.manager.element_ids, "evaluate", hung_read)

        # Unread, the click runs as labelled, and reports for itself what becomes of it.
        await asyncio.wait_for(
            env.manager.execute_decision(session_id, _click("read", element_id=delete["element_id"])), 20
        )

        assert bounds == [(READ_TIMEOUT_MS, READ_TIMEOUT_MS, True)]
        assert await _title(env, session_id) == "DELETED"
        assert await env.manager.approvals.list() == []


async def test_inside_an_outer_approval_the_click_is_not_asked_about_again(tmp_path: Path) -> None:
    # A governed run's approval: the operator approved this very click, labelled write, as a write.
    async with browser_env(tmp_path, {"/": PAGE}) as env:
        session_id = await env.create("/")
        decision = _click("write", selector="#delete")
        governed = await env.manager.approvals.create_or_reuse_pending(
            session_id=session_id, kind="write", reason="governed", action=decision
        )
        await env.manager.approve(governed.id)

        async with held_for_execution(env.manager.approvals, governed.id, may_give_back=True):
            await env.manager.execute_decision(session_id, decision, approval_id=governed.id)

        assert await _title(env, session_id) == "DELETED"


async def test_a_read_labelled_navigation_off_the_list_is_refused(tmp_path: Path) -> None:
    # STORE-42's decision: no new rule. A navigate is refused before it is asked for or run, and a link click is
    # refused by the request gate (PRE_REQUEST_BLOCKING, on by default) whatever its label.
    page = PAGE + '<a id="away" href="http://localhost:{port}/article">Read more</a>'
    async with browser_env(tmp_path, {"/": "", "/article": ARTICLE}) as env:
        env.pages["/"] = page.format(port=env.port)
        session_id = await env.create("/")

        with pytest.raises(PermissionError):
            await env.manager.execute_decision(
                session_id,
                BrowserActionDecision(
                    action="navigate", reason="r", url=f"http://localhost:{env.port}/article", risk_category="read"
                ),
            )
        await env.manager.execute_decision(session_id, _click("read", selector="#away"))

        # The gate fails the request: Chromium shows its error page, never the page off the list.
        assert await _title(env, session_id) != "ARTICLE"
        assert not env.page(session_id).url.startswith(f"http://localhost:{env.port}")


async def test_no_click_aimed_at_a_small_control_presses_the_button_around_it(tmp_path: Path) -> None:
    # The classifier reads the element at the point a click names; the press used to land up to 2.5 px from it
    # each way, on a neighbour nobody had read (tests/test_click_jitter.py pins the fallback).
    clicks = 12
    state = random.getstate()
    random.seed(20261110)
    try:
        async with browser_env(tmp_path, {"/": SMALL_CONTROL}) as env:
            session_id = await env.create("/")
            page = env.page(session_id)
            box = await page.locator("#dot").bounding_box()
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

            for _ in range(clicks):
                await env.manager.click(session_id, x=x, y=y)

            # Unchecked, a jittered press stays on a 3 px control about one time in three.
            assert (await page.title(), await page.evaluate("window.dots")) == ("start", clicks)
    finally:
        random.setstate(state)

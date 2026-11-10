"""STORE-42 against real Chromium: a fast-profile click asks for approval by what it lands on, not by its label.

The fast profile asked for approval only when the decision's own risk_category said post, payment, account_change
or destructive, and that label is a model's reading of the page it acts on. These run execute_decision, the fast
path every agent step, browser.execute_action and REST decision goes through, on pages served locally, with the
target read in the controller's isolated world (app/browser/click_target.py). Same harness as test_element_ids.py.
"""

from __future__ import annotations

import asyncio
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
ARTICLE = "<title>ARTICLE</title>"
RESULTS = "<title>RESULTS</title>"


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
        assert await _title(env, session_id) == "start"

        # Approved, the same click spends it: its target reads the same, so it matches.
        await env.manager.approve(approval.id)
        await env.manager.execute_decision(
            session_id, _click("read", element_id=delete["element_id"]), approval_id=approval.id
        )
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
        evaluate = env.manager.element_ids.evaluate
        bounds: list[tuple[int, int]] = []

        async def hung_read(page, script, arg=None, *, timeout_ms=None, setup_timeout_ms=None):
            if script != CLICK_TARGET_SCRIPT:
                return await evaluate(page, script, arg, timeout_ms=timeout_ms, setup_timeout_ms=setup_timeout_ms)
            # What the world raises once a message's deadline passes with no answer (element_world.py).
            bounds.append((timeout_ms, setup_timeout_ms))
            await asyncio.sleep(0.2)
            raise BrowserActionError("The page did not answer", code=PAGE_NOT_RESPONDING, retryable=False)

        monkeypatch.setattr(env.manager.element_ids, "evaluate", hung_read)

        # Unread, the click runs as labelled, and reports for itself what becomes of it.
        await asyncio.wait_for(env.manager.execute_decision(session_id, _click("read", selector="#delete")), 20)

        assert bounds == [(READ_TIMEOUT_MS, READ_TIMEOUT_MS)]
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

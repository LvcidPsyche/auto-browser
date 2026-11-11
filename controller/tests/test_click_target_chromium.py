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
from app.browser.click_target import (
    CLICK_TARGET_SCRIPT,
    DESCRIBE_TARGET_SCRIPT,
    MAX_LABEL_CHARS,
    PRESS_READ_TIMEOUT_MS,
    READ_TIMEOUT_MS,
    same_element_at,
)
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
# frame the page cannot reach into (sandboxed, so of an origin of its own; its script runs, to show a press).
FRAMES = """<title>start</title>
<style>
  body { margin: 0 }
  iframe { position: absolute; left: 60px; width: 380px; border: 6px solid #888; padding: 5px }
  #outer { top: 30px; height: 220px }
  #apart { top: 300px; height: 80px }
</style>
<iframe id="outer"></iframe>
<iframe id="apart" sandbox="allow-scripts"
  srcdoc="<button id='sealed' onclick=&quot;this.textContent = 'PRESSED'&quot;>Delete account</button>"></iframe>
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
# A design system's button: a component whose shadow tree is a button around a slot, its label slotted in from the
# page (text=Delete account finds the label, or the component), and one whose name and note are separate blocks.
SLOTTED = """<title>start</title>
<x-button id="host"><span id="label">Delete account</span></x-button>
<x-button id="noted"><div>Delete account</div>
  <div>This permanently removes your profile and all its data</div></x-button>
<x-button id="filter"><span>Remove</span> <b>filter</b></x-button>
<x-button id="bare">Erase everything</x-button>
<x-card id="card"><span slot="title">Storage</span><span slot="action">Delete</span></x-card>
<script>
document.getElementById('card').attachShadow({mode: 'open'}).innerHTML =
  `<h3><slot name="title"></slot></h3><button><slot name="action"></slot></button>`;
for (const host of document.querySelectorAll('x-button')) {
  host.attachShadow({mode: 'open'}).innerHTML = `<button class="inner"><slot></slot></button>`;
  host.shadowRoot.querySelector('.inner').addEventListener('click', () => { document.title = 'PRESSED ' + host.id; });
}
</script>
"""
# Buttons that show an icon and are named beside it: by a title, by a label on the icon, by an image's alt text,
# and by text in the shadow root of a component inside the button.
NAMED_BESIDE = """<title>start</title>
<style>button { display: block; width: 60px; height: 40px; margin: 8px }</style>
<button id="titled" title="Delete account" onclick="document.title='PRESSED'">&#128465;</button>
<button id="icon" onclick="document.title='PRESSED'"><i aria-label="Delete account">&#128465;</i></button>
<button id="image" onclick="document.title='PRESSED'"><img alt="Delete account" width="16" height="16"
  src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="></button>
<button id="shadow" onclick="document.title='PRESSED'"><x-text id="text"></x-text></button>
<button id="drawn" onclick="document.title='PRESSED'"><svg width="16" height="16"><title>
  Delete account</title><path d="M0 0h16v16H0z"/></svg></button>
<svg id="svg" role="button" tabindex="0" width="120" height="40" onclick="document.title='PRESSED'"><rect
  width="120" height="40" fill="#ddd"/><text x="8" y="24">Remove member</text></svg>
<button id="plain" title="Close this panel" onclick="document.title='CLOSED'">&#10005;</button>
<script>
document.getElementById('text').attachShadow({mode: 'open'}).innerHTML = `<span>Delete account</span>`;
</script>
"""
# A row's menu as the component libraries build one: its commands are menu items on a li, not buttons.
MENU = """<title>start</title>
<style>li { display: block; width: 220px; height: 30px; margin: 6px }</style>
<ul role="menu">
  <li id="rename" role="menuitem" tabindex="-1" onclick="document.title='RENAMED'"><span>Rename</span></li>
  <li id="item" role="menuitem" tabindex="-1" onclick="document.title='DELETED'"><span id="word">Delete
    account</span></li>
  <li id="choice" role="menuitemradio" tabindex="-1" onclick="document.title='CHOSEN'">Pay monthly</li>
  <li id="padded" role=" MenuItem " tabindex="-1" onclick="document.title='PADDED'"><span
    id="padded-word">Remove member</span></li>
  <li id="two" role="menuitem button" tabindex="-1" onclick="document.title='TWO'"><span
    id="two-word">Delete workspace</span></li>
</ul>
"""
# A label for a button, and one around a button: a press on either is a press on the button, as HTML has it.
LABELLED = """<title>start</title>
<style>label { display: block; margin: 12px }</style>
<button id="del" onclick="document.title='DELETED'">Delete account</button>
<label id="for" for="del">Erase my data</label>
<label id="around">Billing <input id="pay" type="button" value="Pay now" onclick="document.title='PAID'"></label>
<label id="field">Your name <input id="name"></label>
<button id="bin" onclick="document.title='EMPTIED'">&#128465;</button>
<label id="named" for="bin">Empty trash</label>
<button id="wipe" onclick="document.title='WIPED'">Delete everything</button>
<label id="drawn" for="wipe"><x-glyph id="glyph"></x-glyph></label>
<script>
document.getElementById('glyph').attachShadow({mode: 'open'}).innerHTML =
  `<span class="mark" style="display: inline-block; width: 24px; height: 24px">&#128465;</span>`;
</script>
"""
# What a press lands on that the read before it did not describe: the button that fills a wrapper the click names
# (a div, and a focusable row with an element id of its own), and a button a row shows only while the pointer is
# over it.
UNDER_THE_POINTER = """<title>start</title>
<style>
  body { margin: 0 }
  .wrap { position: absolute; left: 40px; width: 220px; height: 50px }
  .wrap button { display: block; width: 220px; height: 50px }
  #panel { top: 30px }
  #member { top: 110px }
  #row { position: absolute; left: 40px; top: 200px; width: 320px; height: 40px; background: #eee }
  #row button { display: none; position: absolute; left: 110px; top: 5px; width: 100px; height: 30px }
  #row:hover button { display: block }
</style>
<div id="panel" class="wrap"><button onclick="document.title='DELETED'">Delete account</button></div>
<div id="member" class="wrap" tabindex="0" aria-label="Member row"><button
  onclick="document.title='REMOVED'">Remove member</button></div>
<div id="row">Report <button onclick="document.title='GONE'">Delete</button></div>
"""
# Wrappers whose middle is not where a click on them lands: one wider than the viewport, whose middle is off the
# screen, and one in two pieces over two lines, whose middle is on text outside it. Playwright clicks the middle of
# the first piece the viewport shows, and a button is there: in the wide one a small button, 20 px to each side of
# that point, inside a border, so a point counted without the border (50 px left, 20 px up) misses it.
PAST_THE_MIDDLE = """<title>start</title>
<style>
  body { margin: 0; font: 16px/20px monospace }
  #wide { position: fixed; left: 0; top: 200px; width: 4000px; height: 60px; border: solid #ccc }
  #wide { border-width: 20px 0 0 50px }
  #wide button { position: absolute; left: calc(50vw - 70px); top: 10px; width: 40px; height: 40px; padding: 0 }
  #lines { width: 300px }
  #lines .lead, #lines .tail { display: inline-block; height: 20px }
  #lines .lead { width: 200px }
  #lines .tail { width: 150px }
  #pieces button { width: 80px; height: 60px; padding: 0; border: 0; vertical-align: top }
</style>
<div id="lines"><span class="lead">lead</span><span id="pieces"><button
  onclick="document.title='REMOVED'">Remove member</button> <span class="tail">tail</span></span></div>
<div id="wide"><button onclick="document.title='DELETED'">Delete account</button></div>
"""
# A wrapper whose middle is on the screen, under the site's fixed bar, with a button where Playwright's click
# lands: the middle of what the viewport shows of it.
COVERED = """<title>start</title>
<style>
  body { margin: 0 }
  #zone { position: relative; height: 150vh }
  #zone button { position: absolute; left: calc(50vw - 40px); top: calc(50vh - 20px); width: 80px; height: 40px }
  #bar { position: fixed; left: 0; right: 0; top: 70vh; height: 10vh; background: #eee }
</style>
<div id="zone"><button onclick="document.title='DELETED'">Delete account</button></div>
<div id="bar">Cookies and such</div>
"""
# A wrapper with no box of its own: its children are laid out in its place.
NO_BOX = """<title>start</title>
<div id="wrap" style="display: contents"><button onclick="document.title='DELETED'">Delete account</button></div>
"""
# A page whose script can take its main thread for a while: nothing sent to it is answered until it is done.
BUSY = """<title>start</title>
<button id="close">Close</button>
<script>window.spin = (ms) => { const end = performance.now() + ms; while (performance.now() < end) {} };</script>
"""
DOUBLE = "(arg) => arg.n * 2"
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


def _type(risk: str, **target: Any) -> BrowserActionDecision:
    return BrowserActionDecision(action="type", reason="the model's word", risk_category=risk, text="x", **target)


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
            (_click("upload", selector="#delete"), "destructive"),
            (_click("write", x=box["x"] + box["width"] / 2, y=box["y"] + box["height"] / 2), "payment"),
        ):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, decision)
            assert asked.value.approval.kind == kind
        assert await _title(env, session_id) == "start"


async def test_a_type_aimed_at_a_button_asks_as_a_click_on_it_does(tmp_path: Path) -> None:
    # A type puts the caret in its target by a press on it, whatever the target is: aimed at "Delete account" it
    # pressed the button, which nothing had read. A steered model had only to say "type" where it meant "click".
    async with browser_env(tmp_path, {"/": PAGE}) as env:
        session_id = await env.create("/")
        pay = await env.element(session_id, "Pay now")

        for decision, kind in (
            (_type("write", selector="#delete"), "destructive"),
            (_type("read", element_id=pay["element_id"]), "payment"),
        ):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, decision)
            assert asked.value.approval.kind == kind
            assert asked.value.approval.reason.startswith("The controller reads this type action's target as a button")
        assert await _title(env, session_id) == "start"

        # A field is no button: the type runs as labelled.
        await env.manager.execute_decision(session_id, _type("write", selector="[name=q]"))
        assert await env.page(session_id).input_value("[name=q]") == "x"
        assert await env.manager.approvals.list(status="approved") == []


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


async def test_a_label_slotted_into_a_shadow_button_is_read_as_that_buttons(tmp_path: Path) -> None:
    # closest() and innerText both stay inside one tree: the label a page slots into a component's shadow button
    # found no button around it, and that button read as having no text. Every "Delete account" of a design
    # system built this way ran as labelled.
    async with browser_env(tmp_path, {"/": SLOTTED}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        label = await _centre(page.locator("#label"))

        for target in ({"selector": "#label"}, label, {"selector": "#host .inner"}, {"selector": "#noted .inner"}):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, _click("read", **target))
            assert asked.value.approval.kind == "destructive", target
        assert await _title(env, session_id) == "start"

        # A harmless continuation split over two elements is still one: it runs.
        await env.manager.execute_decision(session_id, _click("write", selector="#filter .inner"))
        assert await _title(env, session_id) == "PRESSED filter"


async def test_a_component_that_is_a_button_is_read_as_the_button_inside_it(tmp_path: Path) -> None:
    # Text a page gives a component straight, with no element around it, is under the pointer as the component:
    # the walk up from it never enters the component's own tree, where the button is.
    async with browser_env(tmp_path, {"/": SLOTTED}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)

        for target in ({"selector": "#bare"}, await _centre(page.locator("#bare"))):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, _click("read", **target))
            assert asked.value.approval.kind == "destructive", target
        assert await _title(env, session_id) == "start"

        # Only a component all of whose content sits in one button: a card with a button among its parts is a card.
        assert (await page.locator("#bare").evaluate(DESCRIBE_TARGET_SCRIPT, 400))["tag"] == "button"
        assert (await page.locator("#card").evaluate(DESCRIBE_TARGET_SCRIPT, 400))["tag"] == "x-card"


async def test_a_label_of_a_button_is_read_as_that_button(tmp_path: Path) -> None:
    # A press on a label is one on its control. Read as a label, "Erase my data" for the "Delete account" button
    # was no button at all, and neither was the word beside "Pay now" inside its label.
    async with browser_env(tmp_path, {"/": LABELLED}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        named = await _centre(page.locator("#for"))
        box = await page.locator("#around").bounding_box()
        word = {"x": box["x"] + 6, "y": box["y"] + box["height"] / 2}

        for target, kind in (
            ({"selector": "#for"}, "destructive"),
            ({"selector": "text=Erase my data"}, "destructive"),
            (named, "destructive"),
            (word, "payment"),
        ):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, _click("read", **target))
            assert asked.value.approval.kind == kind, target
        assert await _title(env, session_id) == "start"

        # A label of a field is no button: the click runs, and puts the caret in the field.
        await env.manager.execute_decision(session_id, _click("read", selector="#field"))
        assert await page.evaluate("document.activeElement.id") == "name"


async def test_a_button_is_read_by_its_label_and_a_label_from_inside_a_shadow_root(tmp_path: Path) -> None:
    # The label was read only when the press was on the label. A button that shows an icon and is named by a
    # label beside it had no name when the click named the button, and a label holding a component's glyph was
    # not found from the glyph: closest() stops at the shadow root the glyph is in.
    async with browser_env(tmp_path, {"/": LABELLED}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        mark = await _centre(page.locator("#glyph .mark"))

        for target in ({"selector": "#bin"}, await _centre(page.locator("#bin")), {"selector": "#glyph .mark"}, mark):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, _click("read", **target))
            assert asked.value.approval.kind == "destructive", target
        assert await _title(env, session_id) == "start"


async def test_a_menu_item_is_read_as_a_button_is(tmp_path: Path) -> None:
    # role=menuitem was no button: the "Delete account" of a menu ran as labelled, by the item, by the text in it
    # and by a point on it.
    async with browser_env(tmp_path, {"/": MENU}) as env:
        session_id = await env.create("/")
        word = await _centre(env.page(session_id).locator("#word"))

        for target in ({"selector": "#item"}, {"selector": "#word"}, word):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, _click("read", **target))
            assert asked.value.approval.kind == "destructive", target
        assert await _title(env, session_id) == "start"

        # A menu item the table has no word for runs, and so does a choice in a menu, which is no command.
        await env.manager.execute_decision(session_id, _click("read", selector="#rename"))
        assert await _title(env, session_id) == "RENAMED"
        await env.manager.execute_decision(session_id, _click("read", selector="#choice"))
        assert await _title(env, session_id) == "CHOSEN"


async def test_a_role_with_spaces_around_it_or_a_second_token_is_read_by_its_first(tmp_path: Path) -> None:
    # The walk to the button matched the role attribute's whole text, and the answer takes its first token: the
    # text inside role="menuitem button", or inside a role with spaces around it, was described itself, no
    # button, and its press ran as labelled. Named by the item, the same click asked.
    async with browser_env(tmp_path, {"/": MENU}) as env:
        session_id = await env.create("/")

        for item, word in (("#padded", "#padded-word"), ("#two", "#two-word")):
            point = await _centre(env.page(session_id).locator(word))
            for target in ({"selector": item}, {"selector": word}, point):
                with pytest.raises(ApprovalRequiredError) as asked:
                    await env.manager.execute_decision(session_id, _click("read", **target))
                assert asked.value.approval.kind == "destructive", target
        assert await _title(env, session_id) == "start"


async def test_a_button_named_by_a_title_or_by_what_is_inside_it_is_read_by_that_name(tmp_path: Path) -> None:
    # An icon button's name is its title, a label or alt text on the icon, or text a component inside it shows:
    # the read took the button's own aria-label and innerText only, and each of these ran as labelled.
    async with browser_env(tmp_path, {"/": NAMED_BESIDE}) as env:
        session_id = await env.create("/")

        # And an SVG's name is its <title>, which is not rendered, or its <text>: innerText has neither.
        for button in ("#titled", "#icon", "#image", "#shadow", "#drawn", "#svg"):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, _click("read", selector=button))
            assert asked.value.approval.kind == "destructive", button
        assert await _title(env, session_id) == "start"

        await env.manager.execute_decision(session_id, _click("read", selector="#plain"))
        assert await _title(env, session_id) == "CLOSED"


async def test_a_point_inside_a_frame_of_another_origin_is_not_read(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/": FRAMES}) as env:
        session_id = await env.create("/")
        button = env.page(session_id).frame_locator("#apart").locator("#sealed")
        sealed = await _centre(button)

        # The page's own origin cannot see into it, so neither can the controller's world there: unread, the
        # click runs as labelled, before the decision and at the press.
        await env.manager.execute_decision(session_id, _click("read", **sealed))

        assert await button.inner_text() == "PRESSED"
        assert await env.manager.approvals.list() == []


async def test_two_points_are_not_one_element_where_neither_can_be_read(tmp_path: Path) -> None:
    # Inside a frame of another origin every point reads as the frame, and off the page every point reads as
    # nothing: two controls side by side in such a frame compared as one element, and the jittered press was kept.
    async with browser_env(tmp_path, {"/": FRAMES}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        ids = env.manager.element_ids
        sealed = await _centre(page.frame_locator("#apart").locator("#sealed"))
        shallow = await _centre(page.frame_locator("#outer").locator("#shallow"))

        assert await same_element_at(ids, page, (sealed["x"], sealed["y"]), (sealed["x"] + 2, sealed["y"] + 2)) is False
        assert await same_element_at(ids, page, (-50.0, -50.0), (-48.0, -48.0)) is False
        # Where the frame can be read, two points on one button are one element.
        near = (shallow["x"] + 2, shallow["y"] + 2)
        assert await same_element_at(ids, page, (shallow["x"], shallow["y"]), near) is True


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

        # Asked before the decision, and once more at the press, for less long: the pointer is waiting there.
        assert bounds == [
            (READ_TIMEOUT_MS, READ_TIMEOUT_MS, True),
            (PRESS_READ_TIMEOUT_MS, PRESS_READ_TIMEOUT_MS, True),
        ]
        assert await _title(env, session_id) == "DELETED"
        assert await env.manager.approvals.list() == []


async def test_a_read_given_up_on_a_busy_page_keeps_the_session_and_its_answers_apart(tmp_path: Path) -> None:
    # The world's tests give up a read on a fake session. Here the page is really busy: the read's answer comes
    # late, on a session that was kept, and must reach no later call as its own.
    async with browser_env(tmp_path, {"/": BUSY}) as env:
        session_id = await env.create("/")
        page, ids = env.page(session_id), env.manager.element_ids
        assert await ids.evaluate(page, DOUBLE, {"n": 1}) == 2
        world = ids._world(page)
        session = world._cdp
        assert session is not None

        # The page's main thread is taken for 2.5 s, and the page says so as that starts: waited for by a fixed
        # sleep, a stalled machine sent the reads after the page was free again.
        async with page.expect_console_message(lambda message: message.text == "spinning", timeout=10_000):
            await page.evaluate("() => { setTimeout(() => { console.log('spinning'); window.spin(2500); }, 30); }")
        beside = asyncio.ensure_future(ids.evaluate(page, DOUBLE, {"n": 21}, timeout_ms=8000, setup_timeout_ms=8000))
        with pytest.raises(BrowserActionError) as given_up:
            await ids.evaluate(page, DOUBLE, {"n": 100}, timeout_ms=300, setup_timeout_ms=300, keep_session=True)

        assert given_up.value.code == PAGE_NOT_RESPONDING
        assert not beside.done()  # given up while the page was still busy, not answered late
        assert world._cdp is session
        # The call beside it, on the same session, gets its own answer once the page is free, and so does every
        # call after it.
        assert await asyncio.wait_for(beside, 10) == 42
        assert await asyncio.gather(*(ids.evaluate(page, DOUBLE, {"n": n}) for n in range(6))) == [0, 2, 4, 6, 8, 10]
        assert world._cdp is session


async def test_the_third_line_of_a_label_is_handed_over_whole(tmp_path: Path) -> None:
    # The page hands over a cut of each label (click_target._LABEL_CUT), and the rules read its first three lines:
    # three lines as long as a line may be, the name on the last.
    line = ("Storage and backups " * 3)[:MAX_LABEL_CHARS]
    name = "Delete account and all of its saved data"
    assert len(line) == len(name) == MAX_LABEL_CHARS
    tall = (
        f'<title>start</title><button id="tall" onclick="document.title=\'GONE\'">{line}<br>{line}<br>{name}</button>'
    )
    async with browser_env(tmp_path, {"/": tall}) as env:
        session_id = await env.create("/")

        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.execute_decision(session_id, _click("read", selector="#tall"))

        assert asked.value.approval.kind == "destructive"
        assert await _title(env, session_id) == "start"


async def test_an_approval_presented_for_its_click_is_spent_though_the_target_is_not_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The retry read its target anew, and a read that failed gave the click no kind: it ran asking for nothing,
    # the approval it presented stayed approved, and the same approval ran the click again and again.
    async with browser_env(tmp_path, {"/": PAGE}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        click = _click("read", selector="#delete")
        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.execute_decision(session_id, click)
        approval = asked.value.approval

        async def unread(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(actions_module, "read_click_target", unread)
        # Not yet approved, it runs nothing.
        with pytest.raises(PermissionError, match="is not approved"):
            await env.manager.execute_decision(session_id, click, approval_id=approval.id)
        assert await _title(env, session_id) == "start"

        await env.manager.approve(approval.id)
        await env.manager.execute_decision(session_id, click, approval_id=approval.id)
        assert await _title(env, session_id) == "DELETED"
        assert (await env.manager.approvals.get(approval.id)).status == "executed"

        await page.evaluate("document.title = 'start'")
        with pytest.raises(PermissionError):
            await env.manager.execute_decision(session_id, click, approval_id=approval.id)
        assert await _title(env, session_id) == "start"

        # An approval asked for another action is still only shown on one that needs none.
        await env.manager.execute_decision(session_id, _click("write", selector="#close"), approval_id=approval.id)
        assert await _title(env, session_id) == "CLOSED"


async def test_a_click_that_names_the_wrapper_of_a_button_asks_at_the_press(tmp_path: Path) -> None:
    # The read before the decision describes the element the action names: here a div, which is no button. The
    # press lands on the button that fills it, and ran as labelled, by a selector, an element id or a type.
    async with browser_env(tmp_path, {"/": UNDER_THE_POINTER}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        row = await env.element(session_id, "Member row")
        click = _click("read", selector="#panel")

        for decision in (click, _click("write", element_id=row["element_id"]), _type("write", selector="#panel")):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, decision)
            assert asked.value.approval.kind == "destructive", decision
            assert asked.value.approval.reason.startswith("The controller reads what this "), decision
        assert await _title(env, session_id) == "start"

        # Approved, the click runs under that approval: at the press the button reads as the kind approved.
        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.execute_decision(session_id, click)
        approval = asked.value.approval
        assert approval.action.risk_category == "destructive"
        await env.manager.approve(approval.id)
        await env.manager.execute_decision(session_id, click, approval_id=approval.id)
        assert await _title(env, session_id) == "DELETED"
        assert (await env.manager.approvals.get(approval.id)).status == "executed"

        # Inside an outer approval, which an operator gave for this very click, the press is not read either.
        await page.evaluate("document.title = 'start'")
        governed = await env.manager.approvals.create_or_reuse_pending(
            session_id=session_id, kind="write", reason="governed", action=_click("write", selector="#panel")
        )
        await env.manager.approve(governed.id)
        async with held_for_execution(env.manager.approvals, governed.id, may_give_back=True):
            await env.manager.execute_decision(session_id, _click("write", selector="#panel"), approval_id=governed.id)
        assert await _title(env, session_id) == "DELETED"

        # And the click route presses for its caller, as it did.
        await page.evaluate("document.title = 'start'")
        await env.manager.click(session_id, selector="#panel")
        assert await _title(env, session_id) == "DELETED"


@pytest.mark.parametrize("wrapper", ["#wide", "#pieces"])
async def test_a_wrapper_whose_middle_is_not_where_its_click_lands_asks_at_the_press(
    tmp_path: Path, wrapper: str
) -> None:
    # The human-like press aims at the middle of the element's box, and where the element is not what is there the
    # click went to Playwright's own, which picks its own point: the read was made at the middle (off the screen,
    # or on text beside the wrapper) and the press landed on the button.
    async with browser_env(tmp_path, {"/": PAST_THE_MIDDLE}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        box = await page.locator(wrapper).bounding_box()
        middle = [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2]
        on_it = "([sel, x, y]) => { const el = document.elementFromPoint(x, y); return !!el && !!el.closest(sel) }"
        assert not await page.evaluate(on_it, [wrapper, *middle]), "the fixture's middle is on the wrapper"

        for decision in (_click("read", selector=wrapper), _type("write", selector=wrapper)):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, decision)
            assert asked.value.approval.kind == "destructive", decision
        assert await _title(env, session_id) == "start"

        # Approved, it presses the button, where Playwright's click would have.
        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.execute_decision(session_id, _click("read", selector=wrapper))
        await env.manager.approve(asked.value.approval.id)
        await env.manager.execute_decision(
            session_id, _click("read", selector=wrapper), approval_id=asked.value.approval.id
        )
        assert await _title(env, session_id) in ("DELETED", "REMOVED")

        # The click route, which reads nothing, still clicks as Playwright places it.
        await page.evaluate("document.title = 'start'")
        await env.manager.click(session_id, selector=wrapper)
        assert await _title(env, session_id) in ("DELETED", "REMOVED")


async def test_a_wrapper_covered_at_its_middle_asks_at_the_press(tmp_path: Path) -> None:
    # The third way into the click Playwright places (after a middle off the screen and a box in pieces): the
    # middle of the wrapper is under something else. Read there, the read saw the site's bar.
    async with browser_env(tmp_path, {"/": COVERED}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        box = await page.locator("#zone").bounding_box()
        middle = [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2]
        assert await page.evaluate("([x, y]) => document.elementFromPoint(x, y).id", middle) == "bar"

        for decision in (_click("read", selector="#zone"), _type("write", selector="#zone")):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, decision)
            assert asked.value.approval.kind == "destructive", decision
        assert await _title(env, session_id) == "start"


async def test_a_wrapper_with_no_box_of_its_own_is_refused_by_name_and_not_pressed(tmp_path: Path) -> None:
    # Playwright places a click on such a wrapper on one of its children, and there is no box to count that point
    # from, so the press cannot be read where it would land. It was pressed unread; then, with the step taken
    # apart, it failed as any action does ("Action failed"), which told the agent nothing it could act on.
    async with browser_env(tmp_path, {"/": NO_BOX}) as env:
        session_id = await env.create("/")
        assert await env.page(session_id).locator("#wrap").bounding_box() is None

        for decision in (_click("read", selector="#wrap"), _type("write", selector="#wrap")):
            with pytest.raises(BrowserActionError) as refused:
                await env.manager.execute_decision(session_id, decision)
            assert refused.value.code == "target_has_no_box", decision
            assert "Name an element inside it" in str(refused.value)
        assert await _title(env, session_id) == "start"

        # The click route, which reads nothing, still clicks as Playwright places it.
        await env.manager.click(session_id, selector="#wrap")
        assert await _title(env, session_id) == "DELETED"


async def test_a_button_the_page_shows_as_the_pointer_arrives_asks_at_the_press(tmp_path: Path) -> None:
    # A row shows its "Delete" only while the pointer is over the row. Read before the pointer moved, the point
    # was the row; the press landed on the button the pointer's arrival had just shown.
    async with browser_env(tmp_path, {"/": UNDER_THE_POINTER}) as env:
        session_id = await env.create("/")
        click = _click("read", x=200.0, y=220.0)

        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.execute_decision(session_id, click)

        approval = asked.value.approval
        assert (approval.kind, approval.action.risk_category) == ("destructive", "destructive")
        assert approval.reason == (
            'The controller reads what this click would press as a button of kind "destructive"; the agent '
            "labelled the click \"read\". The agent's reason: the model's word"
        )
        assert await _title(env, session_id) == "start"

        await env.manager.approve(approval.id)
        await env.manager.execute_decision(session_id, click, approval_id=approval.id)
        assert await _title(env, session_id) == "GONE"


async def test_an_approval_for_one_kind_does_not_press_a_button_of_another(tmp_path: Path) -> None:
    # Approved to delete, the click finds "Pay now" under the pointer: not pressed, asked about as a payment, and
    # the approval it ran under is spent, since its action had begun.
    async with browser_env(tmp_path, {"/": UNDER_THE_POINTER}) as env:
        session_id = await env.create("/")
        click = _click("read", selector="#panel")
        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.execute_decision(session_id, click)
        to_delete = asked.value.approval
        await env.manager.approve(to_delete.id)
        await env.page(session_id).evaluate("document.querySelector('#panel button').textContent = 'Pay now'")

        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.execute_decision(session_id, click, approval_id=to_delete.id)

        assert asked.value.approval.kind == "payment"
        assert await _title(env, session_id) == "start"
        assert (await env.manager.approvals.get(to_delete.id)).status == "executed"
        with pytest.raises(PermissionError, match="was spent"):
            await env.manager.execute_decision(session_id, click, approval_id=to_delete.id)


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

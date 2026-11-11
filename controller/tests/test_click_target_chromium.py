"""STORE-42 against real Chromium: a fast-profile click asks for approval by what it lands on, not by its label.

The fast profile asked for approval only when the decision's own risk_category said post, payment, account_change
or destructive, and that label is a model's reading of the page it acts on. These run execute_decision, the fast
path every agent step, browser.execute_action and REST decision goes through, on pages served locally, with the
target read as the click will find it (app/browser/click_target.py). Same harness as test_element_ids.py.
"""

from __future__ import annotations

import asyncio
import html
import random
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import ElementHandle, Frame, Locator

from app.action_errors import PAGE_NOT_RESPONDING, BrowserActionError
from app.approvals import ApprovalRequiredError, held_for_execution
from app.browser.click_target import (
    _ELEMENT_AT_SCRIPT,
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
<button id="html-named" onclick="document.title='OPENED'"><title>Delete account</title><text>Open</text></button>
<svg id="fifth" role="button" tabindex="0" width="120" height="90" onclick="document.title='DRAWN'"><rect
  width="120" height="90" fill="#ddd"/><text x="8" y="15">Q1</text><text x="8" y="30">Q2</text><text x="8"
  y="45">Q3</text><text x="8" y="60">Q4</text><text x="8" y="75">Delete account</text></svg>
<svg id="fourth" role="button" tabindex="0" width="120" height="75" onclick="document.title='PRESSED'"><rect
  width="120" height="75" fill="#ddd"/><text x="8" y="15">Q1</text><text x="8" y="30">Q2</text><text x="8"
  y="45">Q3</text><text x="8" y="60">Delete account</text></svg>
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
  <li id="linked" role="menuitem" tabindex="-1" onclick="document.title='LINKED'"><span><a id="link"
    href="#linked">Delete project</a></span></li>
</ul>
<div id="tile" role="button" tabindex="0" onclick="document.title='TILE'"><a id="tile-link"
  href="#tile">Remove member</a></div>
<button id="in-button" style="display: block; margin: 6px" onclick="document.title='BUTTON'">Delete <a
  id="button-link" href="#in-button">account</a></button>
<div id="row" role="button" tabindex="0" onclick="document.title='ROW'">Delete request #12 <a id="own-role"
  role="menuitem" href="#row" onclick="event.stopPropagation(); document.title='OPENED'">Open</a></div>
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
# A form whose button is disabled until it is ready: it then draws the button again, a new element in the
# disabled one's place (renderAgain), or enables the one it has (enable).
RENDERED_AGAIN = """<title>start</title>
<body style="margin: 0">
<button id="go" disabled>Go</button>
<script>
function renderAgain() {
  const fresh = document.createElement('button');
  fresh.id = 'go';
  fresh.textContent = 'Go';
  fresh.onclick = () => { document.title = 'DONE'; };
  document.getElementById('go').replaceWith(fresh);
}
function enable() {
  const go = document.getElementById('go');
  go.onclick = () => { document.title = 'DONE'; };
  go.disabled = false;
}
</script>
"""
# A card that is a link. An inline element around a block has three boxes, and the first is empty, at the corner
# of the block, where this card keeps a star of its own. The card is wider than the viewport, so its middle is
# off the screen and its click is one Playwright places.
CARD = """<title>start</title>
<style>
  body { margin: 0 }
  #place { position: fixed; left: 0; top: 100px }
  #card div { position: relative; width: 4000px; height: 60px }
  #star { position: absolute; left: 0; top: 0; width: 24px; height: 24px }
</style>
<div id="place"><a id="card" href="#opened" onclick="document.title = 'OPENED'"><div><span id="star"
  onclick="event.preventDefault(); event.stopPropagation(); document.title = 'STARRED'">*</span>Card</div></a></div>
"""
# The card with a quarter of a pixel of the link before its block: a first box of which the screen shows under a
# square pixel, here under something of the page's own. A quarter, so that the box is under one square pixel
# whichever height a platform's font gives a line of one pixel (one or two).
THIN_FIRST_BOX = (
    CARD.replace(
        '<a id="card" href="#opened" onclick="document.title = \'OPENED\'"><div>',
        '<a id="card" style="font: 1px/1px monospace" href="#opened" onclick="document.title = \'OPENED\'">'
        '<i style="display: inline-block; width: 0.25px; height: 0.25px"></i><div style="font: 16px/1.2 sans-serif">',
    )
    + '<div id="lid" style="position: fixed; left: 0; top: 98px; width: 8px; height: 5px" '
    "onclick=\"document.title = 'LID'\"></div>"
)
# A fixed card wider than the viewport, with under a pixel of its height on the screen, at the top edge.
SLIVER = """<title>start</title>
<style>
  body { margin: 0 }
  #place { position: fixed; left: 0; top: -59.1px }
  #card { display: block; width: 4000px; height: 60px }
</style>
<div id="place"><a id="card" href="#opened" onclick="document.title = 'OPENED'">Card</a></div>
"""
# A wrapper with no box of its own: its children are laid out in its place.
NO_BOX = """<title>start</title>
<div id="wrap" style="display: contents"><button onclick="document.title='DELETED'">Delete account</button></div>
"""
# A frame of the page's own origin in a wrapper. Its button fills it and counts its presses in the page's title.
_FRAMED_BUTTON = (
    "<body style='margin: 0'><button style='position: absolute; left: 0; top: 0; width: 100%; height: 100%' "
    "onclick=&quot;top.document.title = 'DELETED x' + (top.presses = (top.presses || 0) + 1)&quot;>"
    "Delete account</button>"
)
_FRAMED_STYLE = "#wrap { width: 300px; padding: 20px }"
FRAMED = f"""<title>start</title>
<style>
  body {{ margin: 0 }}
  iframe {{ display: block; width: 300px; height: 120px; border: 0 }}
  {_FRAMED_STYLE}
</style>
<div id="wrap"><iframe id="frame" srcdoc="{_FRAMED_BUTTON}"></iframe></div>
"""
# The same under a wrapper wider than the viewport, the frame where Playwright places a click on the wrapper.
FRAMED_WIDE = FRAMED.replace(
    _FRAMED_STYLE,
    "#wrap { position: relative; width: 4000px; height: 120px } "
    "iframe { position: absolute; left: calc(50vw - 150px); top: 0 }",
)
FRAMED_NO_BOX = FRAMED.replace(_FRAMED_STYLE, "#wrap { display: contents }")
# The same button in a document of the page's own that an object or an embed holds, served beside the page.
_HELD_STYLE = (
    "<title>start</title><style>body { margin: 0 } #wrap { width: 300px; padding: 20px } "
    "object, embed { display: block; width: 300px; height: 120px; border: 0 }</style>"
)
HELD_BY = {
    "object": _HELD_STYLE + '<div id="wrap"><object id="frame" type="text/html" data="/held"></object></div>',
    "embed": _HELD_STYLE + '<div id="wrap"><embed id="frame" type="text/html" src="/held"></div>',
}
HELD = html.unescape(_FRAMED_BUTTON)
# What stands at a point with no document the page can reach, after three frames of the page's own.
NOT_ENTERED = {
    "a frame of another origin": '<iframe id="frame" sandbox="allow-scripts" srcdoc="<p>Next</p>"></iframe>',
    "an object that holds no document": '<object id="frame" type="application/x-nothing">none</object>',
}
# ([x, y]) => [the tag of what the read at a point answers, how many of the page's windows it asked for their
# frame element on the way]. Run where the page's script runs, with each window the page can reach marked
# first; one of another origin cannot be asked at all.
_READ_AT_AND_COUNT = (
    "([x, y]) => { let asked = 0; "
    "for (let index = 0; index < window.length; index += 1) { try { const held = window[index]; "
    "const frame = held.frameElement; "
    "Object.defineProperty(held, 'frameElement', { get() { asked += 1; return frame; } }); "
    "} catch (error) {} } "
    "const el = (" + _ELEMENT_AT_SCRIPT + ")(x, y); return [el ? el.localName : null, asked]; }"
)
# A frame with a link in it wider than the frame shows, and a button of the page itself beside the frame.
# The selector names the link through the frame, as a locator made from a frame locator does.
IN_THE_FRAME = "#frame >> internal:control=enter-frame >> "
CUT_BY_ITS_FRAME = (
    "<title>start</title><style>body { margin: 0 } "
    "#frame { position: fixed; left: FRAME_LEFTpx; top: 100px; width: FRAME_WIDTHpx; height: 120px; border: 0 } "
    "#side { position: fixed; left: SIDE_LEFTpx; top: 110px; width: 160px; height: 80px }</style>"
    '<iframe id="frame" src="/inner"></iframe>'
    '<button id="side" onclick="document.title = \'SIDE PRESSED\'">SIDE_LABEL</button>'
)
CUT_LINK = (
    "<body style='margin: 0'><div style='position: fixed; left: 0; top: 20px'>"
    "<a id='link' href='#opened' style='display: block; width: LINK_WIDTHpx; height: 60px' "
    "onclick=\"event.preventDefault(); top.document.title = 'OPENED'\">Card</a></div>"
)


def _cut_by_its_frame(frame_left: int, frame_width: int, link_width: int, side: str) -> dict[str, str]:
    page = (
        CUT_BY_ITS_FRAME.replace("FRAME_LEFT", str(frame_left))
        .replace("FRAME_WIDTH", str(frame_width))
        .replace("SIDE_LEFT", str(frame_left + link_width // 2 - 60))
        .replace("SIDE_LABEL", side)
    )
    return {"/": page, "/inner": CUT_LINK.replace("LINK_WIDTH", str(link_width))}


# A frame drawn at half its size and still wider than the screen, with a link as wide in it and something of
# the frame's own over the middle of the link.
SCALED_FRAME = (
    "<title>start</title><style>body { margin: 0 } #frame { position: fixed; left: 0; top: 100px; width: 3000px; "
    "height: 240px; border: 0; transform: scale(0.5); transform-origin: 0 0 }</style>"
    '<iframe id="frame" src="/inner"></iframe>'
)
SCALED_LINK = (
    "<body style='margin: 0'><div style='position: fixed; left: 0; top: 40px'>"
    "<a id='link' href='#opened' style='display: block; width: 3000px; height: 120px' "
    "onclick=\"event.preventDefault(); top.document.title = 'OPENED'\">Card</a></div>"
    "<div style='position: fixed; left: 1400px; top: 40px; width: 200px; height: 120px' "
    "onclick=\"top.document.title = 'COVER PRESSED'\"></div>"
)
# A frame in a dialog the page centres with a transform, so the frame stands inside something CSS transforms; a
# button in the frame; and over the button a frame of the frame's own, whose button fills it.
DIALOG_FRAME = (
    "<title>start</title><style>body { margin: 0 } #dialog { position: fixed; left: 50%; top: 50%; "
    "transform: translate(-50%, -50%); width: 400px; height: 200px } "
    "#frame { display: block; width: 400px; height: 200px; border: 0 }</style>"
    '<div id="dialog"><iframe id="frame" src="/inner"></iframe></div>'
)
IN_THE_DIALOG = (
    "<body style='margin: 0'><button id='go' style='position: absolute; left: 100px; top: 80px; width: 60px; "
    "height: 24px' onclick=\"top.document.title = 'PRESSED'\">Go</button>"
)
A_FRAME_OVER_IT = (
    "<iframe src='/over' style='position: absolute; left: 90px; top: 70px; width: 80px; height: 44px; "
    "border: 0'></iframe>"
)
OVER_IT = (
    "<body style='margin: 0'><button style='width: 80px; height: 44px' "
    "onclick=\"top.document.title = 'THE FRAME OVER IT PRESSED'\">Next</button>"
)
# A frame the page cannot reach into (sandboxed, so in a process of its own) with padding, and in it two buttons,
# one over the other, each noting its own press. Playwright counts a box in such a frame from the corner of the
# frame's border, so the box it gives is up and left of the element by the padding: 11 px here, which leaves the
# middle of that box one pixel inside the lower button's top edge.
PADDED_APART = (
    "<title>start</title><style>body { margin: 0 } #frame { position: absolute; left: 100px; top: 100px; "
    "width: 400px; height: 200px; border: 0; padding: 11px }</style>"
    '<iframe id="frame" sandbox="allow-scripts" srcdoc="'
    "<body style='margin: 0'>"
    "<button id='above' style='position: absolute; left: 100px; top: 56px; width: 120px; height: 24px' "
    "onclick=&quot;this.textContent = 'PRESSED'&quot;>Next</button>"
    "<button id='go' style='position: absolute; left: 100px; top: 80px; width: 120px; height: 24px' "
    "onclick=&quot;this.textContent = 'PRESSED'&quot;>Go</button>"
    '"></iframe>'
)
# A button that notes its press, for a page that turns it off for a while (a form does while it validates).
TURNED_OFF = (
    "<title>start</title><body style='margin: 0'>"
    "<button id='go' style='position: absolute; left: 100px; top: 100px; width: 120px; height: 40px' "
    "onclick=\"document.title = 'PRESSED'\">Go</button>"
)
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


async def test_a_link_inside_a_menu_item_or_a_button_is_read_as_that_item(tmp_path: Path) -> None:
    # The walk to the button stopped at the first link. A menu whose items carry a URL keeps each item's text in
    # a link, and a press on that text is one on the item: named by the link, by its text or by a point on it,
    # the item's "Delete project" was described as a link, which is not classified, and ran as labelled.
    async with browser_env(tmp_path, {"/": MENU}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)

        for link in ("#link", "#tile-link", "#button-link"):
            for target in ({"selector": link}, await _centre(page.locator(link))):
                with pytest.raises(ApprovalRequiredError) as asked:
                    await env.manager.execute_decision(session_id, _click("read", **target))
                assert asked.value.approval.kind == "destructive", (link, target)
        with pytest.raises(ApprovalRequiredError):
            await env.manager.execute_decision(session_id, _click("read", selector="text=Delete project"))
        assert await _title(env, session_id) == "start"

        # A link that is itself an item is read by its own names, not by those of the row around it.
        await env.manager.execute_decision(session_id, _click("read", selector="#own-role"))
        assert await _title(env, session_id) == "OPENED"


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
        # (#fourth: the fourth of an SVG's names is still read.)
        for button in ("#titled", "#icon", "#image", "#shadow", "#drawn", "#svg", "#fourth"):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, _click("read", selector=button))
            assert asked.value.approval.kind == "destructive", button
        assert await _title(env, session_id) == "start"

        await env.manager.execute_decision(session_id, _click("read", selector="#plain"))
        assert await _title(env, session_id) == "CLOSED"

        # Those names are an SVG's own. An HTML element called title is no name: it is not shown, and what one
        # called text shows is in innerText. And the first four are read, as the docs say, however many an
        # SVG holds: a chart that is a button has a text for every tick.
        await env.manager.execute_decision(session_id, _click("read", selector="#html-named"))
        assert await _title(env, session_id) == "OPENED"
        await env.manager.execute_decision(session_id, _click("read", selector="#fifth"))
        assert await _title(env, session_id) == "DRAWN"


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


@pytest.mark.parametrize(("wrapper", "pressed"), [("#wide", "DELETED"), ("#pieces", "REMOVED")])
async def test_a_wrapper_whose_middle_is_not_where_its_click_lands_asks_at_the_press(
    tmp_path: Path, wrapper: str, pressed: str
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
        assert await _title(env, session_id) == pressed

        # The click route, which reads nothing, still clicks as Playwright places it.
        await page.evaluate("document.title = 'start'")
        await env.manager.click(session_id, selector=wrapper)
        assert await _title(env, session_id) == pressed


async def test_a_click_playwright_places_on_a_card_link_lands_where_playwrights_own_does(tmp_path: Path) -> None:
    # The page took the element's first box whatever its size, and an inline link around a block has an empty
    # one first, at the block's corner: the labelled click pressed that corner pixel, here the card's star,
    # where Playwright's click (the click route) presses the middle of what the viewport shows of the card.
    async with browser_env(tmp_path, {"/": CARD}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        areas = await page.locator("#card").evaluate(
            "(el) => Array.from(el.getClientRects()).map((box) => box.width * box.height)"
        )
        # The shape the fixture stands for: an empty box, the block's, an empty box.
        assert len(areas) == 3 and areas[0] == 0 and areas[1] > 0, areas

        await env.manager.execute_decision(session_id, _click("read", selector="#card"))
        assert await _title(env, session_id) == "OPENED"

        await page.evaluate("document.title = 'start'")
        await env.manager.click(session_id, selector="#card")
        assert await _title(env, session_id) == "OPENED"


async def test_a_click_playwright_places_passes_over_a_first_box_of_under_a_square_pixel(tmp_path: Path) -> None:
    # Playwright takes the first box of which the viewport shows more than 0.99 of a square pixel, and not the
    # first it shows anything of. A quarter of a pixel of this link stands before its block, under something
    # of the page's own: a point taken there is one the pointer never finds the link at, and the click fails
    # when its time is up, where Playwright's own (the click route) presses the card.
    async with browser_env(tmp_path, {"/": THIN_FIRST_BOX}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        areas = await page.locator("#card").evaluate(
            "(el) => Array.from(el.getClientRects()).map((box) => box.width * box.height)"
        )
        # The shape the fixture stands for: a box of under a square pixel, then the block's.
        assert THIN_FIRST_BOX != CARD and 0 < areas[0] < 0.99 < areas[1], areas

        await env.manager.execute_decision(session_id, _click("read", selector="#card"))
        assert await _title(env, session_id) == "OPENED"

        await page.evaluate("document.title = 'start'")
        await env.manager.click(session_id, selector="#card")
        assert await _title(env, session_id) == "OPENED"


async def test_a_click_playwright_places_on_a_sliver_of_its_target_lands_where_playwrights_own_does(
    tmp_path: Path,
) -> None:
    # The point was taken inside the viewport only along an axis that shows a whole pixel of the box, and
    # otherwise at the middle of the whole box. Playwright asks for an area, over 0.99 of a pixel: a wide card
    # with 0.9 px of its height on the screen is pressed there by its click, and the labelled click aimed at
    # the middle of the card, above the screen, and failed when its time was up.
    async with browser_env(tmp_path, {"/": SLIVER}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        box = await page.locator("#card").bounding_box()
        assert 0 < box["y"] + box["height"] < 1, box

        await env.manager.execute_decision(session_id, _click("read", selector="#card"))
        assert await _title(env, session_id) == "OPENED"

        await page.evaluate("document.title = 'start'")
        await env.manager.click(session_id, selector="#card")
        assert await _title(env, session_id) == "OPENED"


@pytest.mark.parametrize("ready", ["renderAgain()", "enable()"])
@pytest.mark.parametrize("route", ["read", "direct"])
async def test_a_disabled_target_the_page_enables_or_renders_again_is_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, ready: str
) -> None:
    # Playwright's click looks its target up again when the element it was waiting on is replaced. The wait for
    # "enabled" that took the trial click's place held one element: a form that drew its disabled button again
    # ended the click at once ("Element is not attached to the DOM"), where every release had waited for the new
    # one. The page draws it again here once the checks are waiting, so no step before them meets the change.
    # It is the checks that wait, on the same element as on a new one: a click that pressed without them is slow
    # enough, along the pointer's path, to meet a button the page has enabled by then.
    real = actions_module.BrowserActionService._checks_of_a_click
    waiting, passed = asyncio.Event(), asyncio.Event()

    async def checks(*given: Any, **named: Any) -> None:
        waiting.set()
        await real(*given, **named)
        passed.set()

    monkeypatch.setattr(actions_module.BrowserActionService, "_checks_of_a_click", staticmethod(checks))
    async with browser_env(tmp_path, {"/": RENDERED_AGAIN}) as env:
        session_id = await env.create("/")
        if route == "read":
            click = env.manager.execute_decision(session_id, _click("read", selector="#go"))
        else:
            click = env.manager.click(session_id, selector="#go")
        clicking = asyncio.ensure_future(click)
        try:
            await asyncio.wait_for(waiting.wait(), 30)
            await asyncio.sleep(0.3)
            assert not clicking.done(), "the click did not wait for its disabled target"
            assert not passed.is_set(), "the click's checks passed a disabled target"
            await env.page(session_id).evaluate(ready)
            await clicking
        finally:
            clicking.cancel()
        assert await _title(env, session_id) == "DONE"


async def test_a_target_the_page_hides_once_its_checks_have_passed_is_told_it_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Between Playwright's checks and the read of its place a page can take the target away. Hidden then, it has
    # no box and nothing inside it to name: the click presses nothing and says the target moved (retryable),
    # where it said "Name an element inside it". A page cannot be timed to hide it between two of the
    # controller's steps, so the step before does it here, once its real checks have passed.
    real = actions_module.BrowserActionService._checks_of_a_click
    calls = 0

    async def checks_then_the_page_hides_it(locator: Any, budget: Any) -> None:
        nonlocal calls
        calls += 1
        await real(locator, budget)
        if calls == 2:  # the placed click's own checks; the first are the human-like press's
            await locator.evaluate("(el) => { el.hidden = true }")

    monkeypatch.setattr(
        actions_module.BrowserActionService, "_checks_of_a_click", staticmethod(checks_then_the_page_hides_it)
    )
    async with browser_env(tmp_path, {"/": PAST_THE_MIDDLE}) as env:
        session_id = await env.create("/")
        with pytest.raises(BrowserActionError) as refused:
            await env.manager.execute_decision(session_id, _click("read", selector="#wide"))
        assert (refused.value.code, refused.value.retryable) == ("target_moved", True)
        assert calls == 2
        assert await _title(env, session_id) == "start"


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
        page = env.page(session_id)
        assert await page.locator("#wrap").bounding_box() is None
        # The page tells such a wrapper from an element that is not shown, which has no box either and nothing
        # inside it to name.
        await page.evaluate("document.body.insertAdjacentHTML('beforeend', '<p id=gone hidden>x</p>')")
        place = actions_module._PRESS_PLACE_SCRIPT
        wrapper, gone = await page.locator("#wrap").evaluate(place), await page.locator("#gone").evaluate(place)
        assert (wrapper["boxless"], wrapper["contents"]) == (True, True)
        assert (gone["boxless"], gone["contents"]) == (True, False)

        for decision in (_click("read", selector="#wrap"), _type("write", selector="#wrap")):
            with pytest.raises(BrowserActionError) as refused:
                await env.manager.execute_decision(session_id, decision)
            assert refused.value.code == "target_has_no_box", decision
            assert "Name an element inside it" in str(refused.value)
        assert await _title(env, session_id) == "start"

        # The click route, which reads nothing, still clicks as Playwright places it.
        await env.manager.click(session_id, selector="#wrap")
        assert await _title(env, session_id) == "DELETED"


@pytest.mark.parametrize("selector", ["#wrap", "#frame"])
async def test_a_click_on_a_frame_or_its_wrapper_presses_nothing_in_the_frame_before_it_is_read(
    tmp_path: Path, selector: str
) -> None:
    # The checks before a press were Playwright's trial click, and a trial click presses: the mouse goes down
    # and up for real, and a listener in the target's own frame cancels the events. Over a frame the target
    # holds they went to the frame's document, so its "Delete account" was pressed once, and only then did the
    # read at the press ask for the approval.
    async with browser_env(tmp_path, {"/": FRAMED}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        for decision in (_click("read", selector=selector), _type("write", selector=selector)):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, decision)
            assert asked.value.approval.kind == "destructive", decision
            assert await _title(env, session_id) == "start", decision

        # Approved, the button is pressed, once.
        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.execute_decision(session_id, _click("read", selector=selector))
        await env.manager.approve(asked.value.approval.id)
        await env.manager.execute_decision(
            session_id, _click("read", selector=selector), approval_id=asked.value.approval.id
        )
        assert await _title(env, session_id) == "DELETED x1"

        # And once by the click route, which reads nothing: it pressed twice, the trial click and its own.
        await page.evaluate("document.title = 'start'; window.presses = 0")
        await env.manager.click(session_id, selector=selector)
        assert await _title(env, session_id) == "DELETED x1"


@pytest.mark.parametrize("holder", ["object", "embed"])
@pytest.mark.parametrize("selector", ["#wrap", "#frame"])
async def test_a_button_in_a_document_an_object_or_an_embed_holds_is_read_as_one_in_a_frame(
    tmp_path: Path, holder: str, selector: str
) -> None:
    # The read at a point went into iframe and frame elements and no further: a page's own document in an
    # object or an embed was that element, no button, and its "Delete account" was pressed by a click
    # labelled read, by a type, and by a click on a point.
    async with browser_env(tmp_path, {"/": HELD_BY[holder], "/held": HELD}) as env:
        session_id = await env.create("/")
        middle = await _centre(env.page(session_id).locator("#frame"))
        for decision in (
            _click("read", selector=selector),
            _type("write", selector=selector),
            _click("read", **middle),
        ):
            with pytest.raises(ApprovalRequiredError) as asked:
                await env.manager.execute_decision(session_id, decision)
            assert asked.value.approval.kind == "destructive", decision
            assert await _title(env, session_id) == "start", decision

        # Approved, the button is pressed, once.
        await env.manager.approve(asked.value.approval.id)
        await env.manager.execute_decision(session_id, _click("read", **middle), approval_id=asked.value.approval.id)
        assert await _title(env, session_id) == "DELETED x1"


@pytest.mark.parametrize("at_the_point", list(NOT_ENTERED))
async def test_the_look_among_a_pages_windows_is_made_for_an_embed_alone(tmp_path: Path, at_the_point: str) -> None:
    # An embed has no contentDocument, so its document is looked for among the windows of its page. The look was
    # made for whatever had no document the page could reach: a frame of another site above all (a payment field,
    # a challenge), the commonest frame a click lands on. In the controller's own world the first look costs
    # about 2 ms a window: on a page of 255 frames the half second of the read before a press ran out.
    markup = _HELD_STYLE + "<iframe hidden></iframe>" * 3 + f'<div id="wrap">{NOT_ENTERED[at_the_point]}</div>'
    async with browser_env(tmp_path, {"/": markup}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        middle = await _centre(page.locator("#frame"))
        found, asked = await page.evaluate(_READ_AT_AND_COUNT, [middle["x"], middle["y"]])
        assert found == ("object" if "object" in at_the_point else "iframe")
        assert asked == 0


@pytest.mark.parametrize(("others", "found"), [(3, "button"), (255, "button"), (256, "embed")])
async def test_an_embed_is_entered_where_it_is_one_of_the_first_256_windows_of_its_page(
    tmp_path: Path, others: int, found: str
) -> None:
    # The look ends at the embed's own window, and at the 256th window of the page whatever it found: an embed
    # that comes later is read as the element, no button, which the docs say.
    markup = _HELD_STYLE + "<iframe hidden></iframe>" * others + HELD_BY["embed"].removeprefix(_HELD_STYLE)
    async with browser_env(tmp_path, {"/": markup, "/held": HELD}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        assert await page.evaluate("window.length") == others + 1
        middle = await _centre(page.locator("#frame"))
        assert await page.evaluate(_READ_AT_AND_COUNT, [middle["x"], middle["y"]]) == [found, min(others + 1, 256)]


@pytest.mark.parametrize(
    ("route", "frame_width", "side"),
    [
        ("direct", 700, "Next"),
        ("read", 700, "Next"),
        ("read", 700, "Delete account"),
        ("direct", 1000, "Next"),
        ("read", 1000, "Next"),
    ],
)
async def test_a_click_on_a_link_its_frame_cuts_presses_the_link_and_not_the_page_beside_the_frame(
    tmp_path: Path, route: str, frame_width: int, side: str
) -> None:
    # Whether the pointer finds the target at the middle of its box was asked in the target's own document, and
    # for one in a frame the answer was always yes: "checked by Playwright", which checks at its own point, the
    # middle of what the viewport shows. A link wider than the screen has the middle of its box elsewhere:
    # outside its frame (700 px wide), or in it under something of the page's own (1000 px, where the link's
    # document finds the link at that point and the page does not find the frame). The click that named the
    # link pressed the page's button there, and ended as done.
    async with browser_env(tmp_path, _cut_by_its_frame(0, frame_width, 1600, side)) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        link = await _centre(page.frame_locator("#frame").locator("#link"))
        button = await page.locator("#side").bounding_box()
        # The shape the fixture stands for: the middle of the link's box is on the page's button.
        assert button["x"] < link["x"] < button["x"] + button["width"], (link, button)
        assert button["y"] < link["y"] < button["y"] + button["height"], (link, button)

        if route == "direct":
            await env.manager.click(session_id, selector=IN_THE_FRAME + "#link")
        else:
            await env.manager.execute_decision(session_id, _click("read", selector=IN_THE_FRAME + "#link"))
        assert await _title(env, session_id) == "OPENED"


@pytest.mark.parametrize("route", ["direct", "read"])
async def test_a_click_on_a_link_in_a_frame_whose_middle_is_off_the_screen_presses_the_link(
    tmp_path: Path, route: str
) -> None:
    # The same with the middle of the link's box past the edge of the viewport: the press went down there, on
    # nothing, and the action ended as done with nothing clicked.
    async with browser_env(tmp_path, _cut_by_its_frame(100, 1000, 3000, "Next")) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        link = await _centre(page.frame_locator("#frame").locator("#link"))
        assert link["x"] > page.viewport_size["width"], link

        if route == "direct":
            await env.manager.click(session_id, selector=IN_THE_FRAME + "#link")
        else:
            await env.manager.execute_decision(session_id, _click("read", selector=IN_THE_FRAME + "#link"))
        assert await _title(env, session_id) == "OPENED"


@pytest.mark.parametrize("route", ["direct", "read"])
async def test_a_click_on_a_link_in_a_frame_that_runs_off_the_screen_presses_what_the_screen_shows(
    tmp_path: Path, route: str
) -> None:
    # The point Playwright picks is the middle of what the screen shows of the element. In a frame the page
    # can tell only what the frame shows: with the frame's right half off the screen that middle was off it
    # too, and a click that is read, which is placed by the controller, failed when its time was up where a
    # plain click pressed the link. The element's box on the screen and the screen's size now go with the
    # question.
    async with browser_env(tmp_path, _cut_by_its_frame(1000, 1000, 3000, "Next")) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        await page.evaluate("document.getElementById('side').remove()")
        frame = await page.locator("#frame").bounding_box()
        assert frame["x"] + frame["width"] / 2 > page.viewport_size["width"], frame

        if route == "direct":
            await env.manager.click(session_id, selector=IN_THE_FRAME + "#link")
        else:
            await env.manager.execute_decision(session_id, _click("read", selector=IN_THE_FRAME + "#link"))
        assert await _title(env, session_id) == "OPENED"


@pytest.mark.parametrize("route", ["direct", "read"])
async def test_a_click_on_a_link_in_a_frame_that_css_scales_is_pressed_where_the_link_is(
    tmp_path: Path, route: str
) -> None:
    # A point of the screen is carried into a frame by the element's two boxes, the one its own document
    # gives and the one on the screen, in proportion. Carried by their corners alone, in a frame drawn at
    # half its size the pointer is looked for a quarter of the way along this link, beside what lies over
    # its middle, and found on the link; and the point a click that is read is placed at, counted in the
    # frame's own pixels, is twice as far along as the screen has it: off the screen here, where the press
    # went down on nothing and the action ended as done. (Unchecked, the press at the middle of the box
    # went to what lies there.)
    async with browser_env(tmp_path, {"/": SCALED_FRAME, "/inner": SCALED_LINK}) as env:
        session_id = await env.create("/")
        link = await _centre(env.page(session_id).frame_locator("#frame").locator("#link"))
        assert 700 < link["x"] < 800, link

        if route == "direct":
            await env.manager.click(session_id, selector=IN_THE_FRAME + "#link")
        else:
            await env.manager.execute_decision(session_id, _click("read", selector=IN_THE_FRAME + "#link"))
        assert await _title(env, session_id) == "OPENED"


async def test_a_read_click_in_a_scaled_frame_does_not_press_what_the_page_puts_over_the_frame(
    tmp_path: Path,
) -> None:
    # Playwright does not look through a frame that CSS transforms: its hover puts the pointer where it is
    # told, and with no event reaching the frame it calls that done. Something of the page's own over that
    # point took the controller's press. The frames on the way up are asked once more, where the press goes.
    cover = (
        '<div style="position: fixed; left: 600px; top: 120px; width: 80px; height: 60px" '
        "onclick=\"document.title = 'PAGE COVER PRESSED'\"></div>"
    )
    async with browser_env(tmp_path, {"/": SCALED_FRAME + cover, "/inner": SCALED_LINK}) as env:
        session_id = await env.create("/")
        with pytest.raises(BrowserActionError) as refused:
            await env.manager.execute_decision(session_id, _click("read", selector=IN_THE_FRAME + "#link"))
        assert (refused.value.code, refused.value.retryable) == ("target_moved", True)
        # The cause, and not a click that failed there for another reason (a hover that ran out of time).
        assert "the pointer does not find the target" in str(refused.value)
        assert await _title(env, session_id) == "start"


async def test_a_read_click_two_frames_down_in_a_scaled_frame_does_not_press_what_the_page_puts_over_them(
    tmp_path: Path,
) -> None:
    # The same with the link one frame further down. The target's own document finds it, and so does the
    # document of the frame around that one: it is the page's own, the third asked, that finds its cover. A
    # check that stopped at the frame around the target's own said yes, and the cover was pressed.
    cover = (
        '<div style="position: fixed; left: 600px; top: 120px; width: 80px; height: 60px" '
        "onclick=\"document.title = 'PAGE COVER PRESSED'\"></div>"
    )
    between = (
        "<body style='margin: 0'><iframe id='frame' src='/deeper' "
        "style='position: absolute; left: 0; top: 0; width: 100%; height: 100%; border: 0'></iframe>"
    )
    pages = {"/": SCALED_FRAME + cover, "/inner": between, "/deeper": SCALED_LINK}
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/")
        with pytest.raises(BrowserActionError) as refused:
            await env.manager.execute_decision(session_id, _click("read", selector=IN_THE_FRAME * 2 + "#link"))
        assert (refused.value.code, refused.value.retryable) == ("target_moved", True)
        assert await _title(env, session_id) == "start"
    # With nothing over them the link is pressed.
    async with browser_env(tmp_path, {**pages, "/": SCALED_FRAME}) as env:
        session_id = await env.create("/")
        await env.manager.execute_decision(session_id, _click("read", selector=IN_THE_FRAME * 2 + "#link"))
        assert await _title(env, session_id) == "OPENED"


async def test_a_read_click_does_not_press_into_a_frame_that_lies_over_its_target_in_a_transformed_frame(
    tmp_path: Path,
) -> None:
    # In a frame that stands inside anything CSS transforms, Playwright's hover puts the pointer where it is told
    # and watches for the event in the target's own document: one that goes into a frame lying over the target
    # there never comes, and it calls that done. The controller then asked the frames on the way up and not the
    # target, so the press went to the button of the frame over it, and the action ended as done.
    pages = {"/": DIALOG_FRAME, "/inner": IN_THE_DIALOG + A_FRAME_OVER_IT, "/over": OVER_IT}
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/")
        with pytest.raises(BrowserActionError) as refused:
            await env.manager.execute_decision(session_id, _click("read", selector=IN_THE_FRAME + "#go"))
        assert (refused.value.code, refused.value.retryable) == ("target_moved", True)
        assert await _title(env, session_id) == "start"
    # With nothing over it, a button in such a frame is pressed.
    async with browser_env(tmp_path, {"/": DIALOG_FRAME, "/inner": IN_THE_DIALOG}) as env:
        session_id = await env.create("/")
        await env.manager.execute_decision(session_id, _click("read", selector=IN_THE_FRAME + "#go"))
        assert await _title(env, session_id) == "PRESSED"


async def test_a_target_the_page_makes_invisible_once_the_pointer_is_placed_is_not_pressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Once Playwright's hover had put the pointer on the target, only the target's box was asked for, and an
    # element made invisible keeps its box: the press went to what was behind it, and the action ended as done.
    # A page does it where hovering a placeholder swaps it for the control. It cannot be timed to do it between
    # two of the controller's steps, so the hover that places the pointer does it here.
    real = Locator.hover
    hidden = 0

    async def hover_then_the_page_hides_it(self: Any, **named: Any) -> None:
        nonlocal hidden
        await real(self, **named)
        if "position" in named:  # the hover that places the pointer: the checks' own is a trial
            hidden += 1
            await self.evaluate("(el) => { el.style.visibility = 'hidden' }")

    monkeypatch.setattr(Locator, "hover", hover_then_the_page_hides_it)
    async with browser_env(tmp_path, {"/": PAST_THE_MIDDLE}) as env:
        session_id = await env.create("/")
        page = env.page(session_id)
        await page.evaluate(
            "() => { window.downs = 0; addEventListener('mousedown', () => { window.downs += 1 }, true) }"
        )
        with pytest.raises(BrowserActionError) as refused:
            await env.manager.execute_decision(session_id, _click("read", selector="#wide"))
        assert (refused.value.code, refused.value.retryable) == ("target_moved", True)
        assert hidden == 1
        assert await page.evaluate("() => window.downs") == 0
        assert await _title(env, session_id) == "start"


async def test_a_shadow_button_under_its_slotted_label_in_a_frame_is_still_pressed(tmp_path: Path) -> None:
    # Where a placed click is pressed the target is asked once more, and then its frames. The way up from what the
    # pointer hits goes by the slot a node is given to, so a label slotted into a component's button is that
    # button's, as Playwright's check has it. By its parents alone it was not, and this click was refused.
    inner = SLOTTED.replace("document.title = 'PRESSED '", "top.document.title = 'PRESSED '")
    assert inner != SLOTTED
    page = (
        '<title>start</title><body style="margin: 0">'
        '<iframe id="frame" src="/inner" style="width: 600px; height: 400px; border: 0"></iframe>'
    )
    async with browser_env(tmp_path, {"/": page, "/inner": inner}) as env:
        session_id = await env.create("/")
        await env.manager.execute_decision(session_id, _click("write", selector=IN_THE_FRAME + "#filter .inner"))
        assert await _title(env, session_id) == "PRESSED filter"


async def test_a_click_on_a_button_in_a_frame_that_the_pointer_finds_is_still_the_controllers_own_press(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The check through the frames must not send every click in a frame to Playwright's own placing: where
    # the pointer finds the target, in a frame of the page's own or two frames down, the press is the
    # human-like one, as it was.
    placed: list[Any] = []
    real = actions_module.BrowserActionService.click_as_playwright_places_it

    async def counted(self: Any, *given: Any, **named: Any) -> None:
        placed.append(given)
        await real(self, *given, **named)

    monkeypatch.setattr(actions_module.BrowserActionService, "click_as_playwright_places_it", counted)
    async with browser_env(tmp_path, {"/": FRAMED}) as env:
        session_id = await env.create("/")
        await env.manager.click(session_id, selector=IN_THE_FRAME + "button")
        assert await _title(env, session_id) == "DELETED x1"
        assert placed == []
    async with browser_env(tmp_path, {"/": FRAMES}) as env:
        session_id = await env.create("/")
        deep = "#outer >> internal:control=enter-frame >> #inner >> internal:control=enter-frame >> #deep"
        await env.manager.click(session_id, selector=deep)
        assert await _title(env, session_id) == "DELETED"
        assert placed == []
    # And in a frame drawn at half its size.
    scaled = FRAMED.replace("border: 0 }", "border: 0; transform: scale(0.5); transform-origin: 0 0 }")
    assert scaled != FRAMED
    async with browser_env(tmp_path, {"/": scaled}) as env:
        session_id = await env.create("/")
        await env.manager.click(session_id, selector=IN_THE_FRAME + "button")
        assert await _title(env, session_id) == "DELETED x1"
        assert placed == []
    # And in a frame whose document came with no doctype (quirks mode), where a bare :hover matches links alone:
    # whether the pointer is over the target is asked in a form that holds there too.
    quirks = {
        "/": '<title>start</title><body style="margin: 0">'
        '<iframe id="frame" src="/inner" style="width: 300px; height: 120px; border: 0"></iframe>',
        "/inner": "<button onclick=\"top.document.title = 'PRESSED'\">Go</button>",
    }
    async with browser_env(tmp_path, quirks) as env:
        session_id = await env.create("/")
        button = env.page(session_id).frame_locator("#frame").locator("button")
        assert await button.evaluate("() => document.compatMode") == "BackCompat"
        await env.manager.click(session_id, selector=IN_THE_FRAME + "button")
        assert await _title(env, session_id) == "PRESSED"
        assert placed == []


async def test_a_click_playwright_places_over_a_frame_presses_nothing_before_it_is_read(tmp_path: Path) -> None:
    # The placed click had a trial click of its own, so on a wrapper wider than the viewport the frame's button
    # was pressed twice before the approval was asked.
    async with browser_env(tmp_path, {"/": FRAMED_WIDE}) as env:
        session_id = await env.create("/")
        with pytest.raises(ApprovalRequiredError) as asked:
            await env.manager.execute_decision(session_id, _click("read", selector="#wrap"))
        assert asked.value.approval.kind == "destructive"
        assert await _title(env, session_id) == "start"

    # And a wrapper with no box of its own was refused in the words "Nothing was pressed" with the frame's
    # button pressed once.
    async with browser_env(tmp_path, {"/": FRAMED_NO_BOX}) as env:
        session_id = await env.create("/")
        with pytest.raises(BrowserActionError) as refused:
            await env.manager.execute_decision(session_id, _click("read", selector="#wrap"))
        assert refused.value.code == "target_has_no_box"
        assert await _title(env, session_id) == "start"


@pytest.mark.parametrize("route", ["direct", "read"])
async def test_a_press_in_a_padded_frame_of_another_process_does_not_land_beside_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    # Where Playwright's box of an element is off (above), the question asked before a human-like press was off
    # with it: the point is carried into the frame by that same box, so it always found the element. The press at
    # the middle of the box, moved by the jitter, went down on the button above, and the action ended as done.
    # The element is asked as well whether the pointer is over it, as the browser has it. Where it is not, the
    # click is Playwright's to place, whose own point, with no jitter, is still on the element here.
    real = actions_module.random.uniform

    def up_and_left(low: float, high: float) -> float:
        return -2.0 if (low, high) == (-actions_module.JITTER_PX, actions_module.JITTER_PX) else real(low, high)

    monkeypatch.setattr(actions_module.random, "uniform", up_and_left)
    async with browser_env(tmp_path, {"/": PADDED_APART}) as env:
        session_id = await env.create("/")
        frame = env.page(session_id).frame_locator("#frame")
        # The shape the fixture stands for: the frame's document begins at 111, 111, and the button at 100, 80 of
        # it is given a box that begins at 200, 180.
        box = await frame.locator("#go").bounding_box()
        assert (box["x"], box["y"]) == (200, 180), box

        if route == "direct":
            await env.manager.click(session_id, selector=IN_THE_FRAME + "#go")
        else:
            await env.manager.execute_decision(session_id, _click("read", selector=IN_THE_FRAME + "#go"))
        assert await frame.locator("#above").inner_text() == "Next"
        assert await frame.locator("#go").inner_text() == "PRESSED"


@pytest.mark.parametrize("route", ["direct", "read"])
async def test_a_link_wider_than_its_frame_is_placed_where_playwrights_own_click_goes(
    tmp_path: Path, route: str
) -> None:
    # Playwright's click takes the middle of what the screen shows of a box, whatever a frame on the way cuts off
    # (590 px into this frame of 600), and the click's checks are made at that point. The placed click of a read
    # action took the middle of what the frame shows of it (300 px in), a point nothing had checked: with
    # something of the frame's own there (a chat bubble) Playwright's hover waited the click's time out, where
    # the click that is not read pressed the link.
    pages = _cut_by_its_frame(100, 600, 4000, "Next")
    pages["/inner"] += (
        "<div style='position: fixed; left: 250px; top: 10px; width: 100px; height: 80px' "
        "onclick=\"top.document.title = 'CHAT OPENED'\">Chat</div>"
    )
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/")
        if route == "direct":
            await env.manager.click(session_id, selector=IN_THE_FRAME + "#link")
        else:
            await env.manager.execute_decision(session_id, _click("read", selector=IN_THE_FRAME + "#link"))
        assert await _title(env, session_id) == "OPENED"


# A link in two pieces over two lines, in a block CSS may turn: fixed sizes, so no font decides where the line breaks.
# The middle of the link's box is on neither piece (the lead is there on the first line, nothing on the second), so
# the click is one Playwright places.
TURNED_LINES = (
    "<!doctype html><title>start</title><style>body { margin: 0; font: 16px/30px monospace } "
    "#block { position: absolute; left: 400px; top: 300px; width: 300px } "
    "#block span { display: inline-block; height: 20px; vertical-align: top } </style>"
    '<div id="block" style="transform: TURN"><span style="width: 200px"></span>'
    '<a id="link" href="#" onclick="document.title = \'OPENED\'; return false">'
    '<span style="width: 40px"></span> <span style="width: 100px"></span></a></div>'
)


@pytest.mark.parametrize("turn", ["none", "rotate(30deg)", "skewX(40deg)"])
@pytest.mark.parametrize("route", ["direct", "read"])
async def test_a_link_over_two_lines_in_a_block_css_turns_is_pressed_where_playwright_presses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, turn: str
) -> None:
    # Playwright counts a position from the box it finds for an element: the box around the whole of it, turned
    # with it. The place script counted from the box around the element's own boxes as the screen has them. The
    # two are one box until an element of several boxes stands in something CSS turns or skews: 30 degrees put
    # them 26 px apart on a link like this one. A read click was then placed off the link, and Playwright's hover
    # at that position waited the click's time out, where the click that is not read pressed the link.
    placed: list[Any] = []
    real = actions_module.BrowserActionService.click_as_playwright_places_it

    async def counted(self: Any, *given: Any, **named: Any) -> None:
        placed.append(given)
        await real(self, *given, **named)

    monkeypatch.setattr(actions_module.BrowserActionService, "click_as_playwright_places_it", counted)
    async with browser_env(tmp_path, {"/": TURNED_LINES.replace("TURN", turn)}) as env:
        session_id = await env.create("/")
        if route == "direct":
            await env.manager.click(session_id, selector="#link")
        else:
            await env.manager.execute_decision(session_id, _click("read", selector="#link"))
        assert await _title(env, session_id) == "OPENED"
        # The shape the fixture stands for: the press at the middle of the box does not find the link.
        assert len(placed) == 1


async def test_a_link_the_page_moves_while_its_place_is_read_is_pressed_where_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The place is counted from the box Playwright finds, which is read one round trip before the script that
    # picks the point. Counted from that box whatever it was, a link the page moved between the two reads was aimed
    # at off by its move, and Playwright's hover waited the click's time out there: counted from the script's own
    # box, read in the one run that picks the point, it had been pressed. The two boxes are one unless they differ
    # in size, so the place is counted from Playwright's box only where its size is not the script's.
    placing = False
    boxes = 0
    real_place = actions_module.BrowserActionService.click_as_playwright_places_it
    real_box = Locator.bounding_box

    async def placed(self: Any, *given: Any, **named: Any) -> None:
        nonlocal placing
        placing = True
        try:
            await real_place(self, *given, **named)
        finally:
            placing = False

    async def a_box_and_then_a_move(self: Any, *given: Any, **named: Any) -> Any:
        nonlocal boxes
        box = await real_box(self, *given, **named)
        if placing:
            boxes += 1
            if boxes == 1:
                await self.page.evaluate("() => { document.querySelector('#block').style.left = '440px'; }")
        return box

    monkeypatch.setattr(actions_module.BrowserActionService, "click_as_playwright_places_it", placed)
    monkeypatch.setattr(Locator, "bounding_box", a_box_and_then_a_move)
    async with browser_env(tmp_path, {"/": TURNED_LINES.replace("TURN", "none")}) as env:
        session_id = await env.create("/")
        await env.manager.execute_decision(session_id, _click("read", selector="#link"))
        assert await _title(env, session_id) == "OPENED"
        # The box the place is counted from, where the move was made, and the box the pointer is moved by.
        assert boxes == 2


async def test_the_place_is_counted_from_playwrights_box_only_where_its_size_is_not_the_scripts(
    tmp_path: Path,
) -> None:
    # Both sides of the half pixel that tells the two boxes apart, asked of the script itself: a box of the
    # script's own size is the script's box wherever it says the link was (the page moved it since), and a box
    # of another size is Playwright's box around a link of several boxes that CSS turns.
    place = actions_module._PRESS_PLACE_SCRIPT
    async with browser_env(tmp_path, {"/": TURNED_LINES.replace("TURN", "none")}) as env:
        session_id = await env.create("/")
        link = env.page(session_id).locator("#link")
        box = await link.bounding_box()
        assert box is not None
        own = await link.evaluate(place, [box, None])
        elsewhere = {**box, "x": box["x"] - 40, "y": box["y"] - 7}

        assert await link.evaluate(place, [elsewhere, None]) == own
        assert await link.evaluate(place, [{**elsewhere, "width": box["width"] + 0.4}, None]) == own
        assert await link.evaluate(place, [{**elsewhere, "height": box["height"] - 0.4}, None]) == own
        for other_size in ({"width": box["width"] + 1}, {"height": box["height"] - 1}):
            counted = await link.evaluate(place, [{**elsewhere, **other_size}, None])
            assert (counted["x"], counted["y"]) == pytest.approx((own["x"] + 40, own["y"] + 7))


@pytest.mark.parametrize("route", ["direct", "read"])
async def test_a_button_the_page_turns_off_while_the_pointer_is_on_its_way_is_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    # "Enabled" was waited for before the pointer set out, and the last check before the press asked only what
    # the pointer finds. The pointer's way and the pause take about half a second: a button the page turned off
    # in that time got a mouse down that the browser gives to nobody, and the action ended as done with nothing
    # done. Enabled is asked once more with the last check; a no is a click Playwright places, whose checks wait
    # for the button as a click waits.
    pages: list[Any] = []
    off_for_ms = [400]
    real = actions_module.BrowserActionService.pause_before_press

    async def turned_off_in_the_pause() -> None:
        await real()
        await pages.pop().evaluate(
            "(ms) => { const go = document.querySelector('#go'); go.disabled = true; "
            "setTimeout(() => { go.disabled = false }, ms) }",
            off_for_ms[0],
        )

    monkeypatch.setattr(
        actions_module.BrowserActionService, "pause_before_press", staticmethod(turned_off_in_the_pause)
    )

    async def click(env: Any, session_id: str) -> None:
        pages.append(env.page(session_id))
        if route == "direct":
            await env.manager.click(session_id, selector="#go")
        else:
            await env.manager.execute_decision(session_id, _click("read", selector="#go"))
        assert pages == []

    async with browser_env(tmp_path, {"/": TURNED_OFF}) as env:
        session_id = await env.create("/")
        await click(env, session_id)
        assert await _title(env, session_id) == "PRESSED"

    # One that stays off ends the click when its time does, and the action is not called done.
    off_for_ms[0] = 60_000
    async with browser_env(tmp_path, {"/": TURNED_OFF}) as env:
        session_id = await env.create("/")
        with pytest.raises(BrowserActionError) as refused:
            await click(env, session_id)
        assert (refused.value.code, refused.value.retryable) == ("browser_action_failed", True)
        assert await _title(env, session_id) == "start"


def _frames_deep(depth: int) -> dict[str, str]:
    """A page with ``depth`` frames one inside the other, each filling the one around it, a button in the last."""
    frame = (
        "<body style='margin: 0'><iframe id='frame' src='/dNEXT' "
        "style='position: absolute; left: 0; top: 0; width: 100%; height: 100%; border: 0'></iframe>"
    )
    pages = {"/": "<title>start</title>" + frame.replace("NEXT", "1")}
    for level in range(1, depth):
        pages[f"/d{level}"] = frame.replace("NEXT", str(level + 1))
    pages[f"/d{depth}"] = (
        "<body style='margin: 0'><button id='deep' style='margin: 40px' "
        "onclick=\"top.document.title = 'PRESSED'\">Open</button>"
    )
    return pages


@pytest.mark.parametrize("route", ["direct", "read"])
async def test_a_button_more_frames_down_than_the_check_goes_up_is_still_pressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    # The check that the pointer finds a target through its frames goes up POINTER_CHECK_FRAMES of them and then
    # gives the click to Playwright's placing. Asked once more where Playwright's hover had put the pointer, it
    # ran out the same way, and there a no ends the click: a read click on a button eight frames down failed
    # every time, where the click that is not read, Playwright's own, pressed it.
    # Each walk that ran out also left one element handle behind, the last frame's, taken and never asked.
    taken: list[Any] = []
    disposed: list[Any] = []

    def counting(owner: Any, name: str, into: list[Any], of_itself: bool = False) -> None:
        real = getattr(owner, name)

        async def counted(self: Any, *given: Any, **named: Any) -> Any:
            got = await real(self, *given, **named)
            into.append(self if of_itself else got)
            return got

        monkeypatch.setattr(owner, name, counted)

    counting(Locator, "element_handle", taken)
    counting(Frame, "frame_element", taken)
    counting(ElementHandle, "dispose", disposed, of_itself=True)
    depth = actions_module.POINTER_CHECK_FRAMES
    async with browser_env(tmp_path, _frames_deep(depth)) as env:
        session_id = await env.create("/")
        selector = IN_THE_FRAME * depth + "#deep"
        if route == "direct":
            await env.manager.click(session_id, selector=selector)
        else:
            await env.manager.execute_decision(session_id, _click("read", selector=selector))
        assert await _title(env, session_id) == "PRESSED"
        assert len(taken) > depth and [handle for handle in taken if handle not in disposed] == []


@pytest.mark.parametrize(("depth", "by_playwright"), [(7, 0), (8, 1)])
async def test_the_controllers_own_press_reaches_a_button_seven_frames_down_and_no_further(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int, by_playwright: int
) -> None:
    # The check asks POINTER_CHECK_FRAMES documents, the target's own first: seven frames down the eighth is the
    # page's own and answers, and the press is the controller's; eight down the check runs out, and the click is
    # Playwright's to place.
    assert actions_module.POINTER_CHECK_FRAMES == 8
    placed: list[Any] = []
    real = actions_module.BrowserActionService.click_as_playwright_places_it

    async def counted(self: Any, *given: Any, **named: Any) -> None:
        placed.append(given)
        await real(self, *given, **named)

    monkeypatch.setattr(actions_module.BrowserActionService, "click_as_playwright_places_it", counted)
    async with browser_env(tmp_path, _frames_deep(depth)) as env:
        session_id = await env.create("/")
        await env.manager.click(session_id, selector=IN_THE_FRAME * depth + "#deep")
        assert await _title(env, session_id) == "PRESSED"
        assert len(placed) == by_playwright


@pytest.mark.parametrize("route", ["direct", "read"])
async def test_what_the_page_puts_over_a_button_that_many_frames_down_is_not_pressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    # Past the frames it asks, the check before a human-like press cannot tell, and that is a no: the click is
    # then Playwright's to place, whose own check goes all the way up. A yes there presses what the page has put
    # over the frames while the pointer was on its way. Playwright's checks, made before the pointer sets out,
    # have to pass first, so the step that makes them puts the cover up here once they have.
    real = actions_module.BrowserActionService._checks_of_a_click
    calls = 0
    cover = (
        '<div style="position: fixed; left: 0; top: 0; width: 300px; height: 200px" '
        "onclick=\"document.title = 'COVER PRESSED'\"></div>"
    )

    async def checks_then_the_page_covers_it(locator: Any, budget: Any) -> None:
        nonlocal calls
        calls += 1
        await real(locator, budget)
        if calls == 1:
            await locator.page.evaluate("(cover) => document.body.insertAdjacentHTML('beforeend', cover)", cover)

    monkeypatch.setattr(
        actions_module.BrowserActionService, "_checks_of_a_click", staticmethod(checks_then_the_page_covers_it)
    )
    depth = actions_module.POINTER_CHECK_FRAMES
    async with browser_env(tmp_path, _frames_deep(depth)) as env:
        session_id = await env.create("/")
        selector = IN_THE_FRAME * depth + "#deep"
        with pytest.raises(BrowserActionError):
            if route == "direct":
                await env.manager.click(session_id, selector=selector)
            else:
                await env.manager.execute_decision(session_id, _click("read", selector=selector))
        assert calls >= 1
        assert await _title(env, session_id) == "start"


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

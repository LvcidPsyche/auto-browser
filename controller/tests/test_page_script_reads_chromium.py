"""In-page scripts read the DOM through the prototypes' own getters and methods, which nothing a page writes into its
markup or onto its own nodes answers for. Real Chromium, through the real BrowserManager.

A form's named controls stand in for the form's own properties, in the page's world and in the element ids' world
alike: in <form><input name="parentNode"></form>, form.parentNode is that input. By selector a script runs in the
page's own world, where a page can also give one of its nodes a property of its own (a shadow root's `host`, a
label's `control`). Read as properties, either steered the walks that decide whether focus is in the named element,
which fields an approval reads, what a click lands on and whether the pointer finds its target. Each page here answers
for one such read, and the script does not follow it. Not covered, and not coverable: a page whose script replaces a
prototype's getter before a selector's script runs, which then gets the page's getter. Only the page server is local
(test_allowed_hosts_navigation's harness).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.action_errors import BrowserActionError
from app.browser.click_target import classified_risk, read_click_target, same_element_at
from app.browser.services.actions import _RECEIVES_POINTER_SCRIPT, POINTER_CHECK_TIMEOUT_MS, BrowserActionService
from app.models import BrowserActionDecision
from tests._chromium import requires_chromium
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium: that module's autouse browser
    _shared_chromium,
    browser_env,
)
from tests.test_secret_field_receipts_chromium import HIDDEN, TEXT, _asked_for, _assert_shown, _typed_into

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

NOTE = "ordinary note text"

# A component the page lists as a text box, which hands its focus to a field in a form inside it. The form has a
# control named nodeType, which form.nodeType answers for. Tall enough that a press at its middle lands on the
# component itself, not on the field.
IN_A_FORM_WITH_A_CONTROL_NAMED_NODE_TYPE = (
    '<title>note</title><div id="box" tabindex="0" role="textbox" aria-label="Note" style="height: 200px">'
    '<form><input type="hidden" name="nodeType"><input id="inner" aria-label="Inner"></form></div>'
    "<script>box.addEventListener(`focus`, () => inner.focus());</script>"
)


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_type_goes_into_a_field_in_a_form_with_a_control_named_node_type(tmp_path: Path, by: str) -> None:
    # dom.up() read the parent's nodeType as a property: from the field, the form answered with its control, the walk
    # stopped at the form, and focus in the field was not focus in the component. The type was refused. In both
    # worlds: markup alone does it.
    async with browser_env(tmp_path, {"/note": IN_A_FORM_WITH_A_CONTROL_NAMED_NODE_TYPE}) as env:
        session, records = await _typed_into(env, "/note", by, "Note", "#box", NOTE)

        assert await session.page.input_value("#inner") == NOTE
        _assert_shown(records, NOTE)


# A password field the type names, whose select-all sends focus to a plain field in another component's open shadow
# root; that shadow root gives itself the named wrapper for its `host`.
HANDS_FOCUS_TO_A_ROOT_THAT_NAMES_THE_WRAPPER_ITS_HOST = (
    '<title>login</title><div id="named" style="display: inline-block"><input id="pw" type="password"></div>'
    '<div id="other"></div><script>'
    "const made = other.attachShadow({mode: `open`}); made.innerHTML = `<input aria-label=Note>`;"
    "Object.defineProperty(made, `host`, {value: named});"
    "pw.addEventListener(`keydown`, (event) => { if (event.ctrlKey) made.firstChild.focus(); });</script>"
)


async def test_a_type_by_selector_is_refused_where_focus_went_to_a_shadow_root_that_names_the_wrapper_its_host(
    tmp_path: Path,
) -> None:
    # By selector the clear's focus checks run in the page's own world, and dom.up() read a shadow root's host as a
    # property: the page's own answer put the plain field of the other component inside the named wrapper, so the
    # check after select-all said focus was in it, and the text went to a field the type never named. Read through
    # the prototype's getter, focus is outside the wrapper, and nothing is typed.
    async with browser_env(tmp_path, {"/login": HANDS_FOCUS_TO_A_ROOT_THAT_NAMES_THE_WRAPPER_ITS_HOST}) as env:
        session_id = await env.create("/login")
        session = env.manager.sessions[session_id]

        with pytest.raises(BrowserActionError) as refused:
            await env.manager.type(session_id, selector="#named", text=TEXT)

        assert refused.value.code == "focus_lost"
        assert await session.page.evaluate("() => [pw.value, made.firstChild.value]") == ["", ""]


# What an approval's read of the fields a type's text can go to says, where the page answers for a read the script
# made: (the page, the read the browser's own state gives).
ANSWERED_FOR_BY_THE_PAGE = {
    # A label for a password field elsewhere, which the page gives a `control` of its own: a plain field.
    "a-label-with-a-control-of-its-own": (
        '<label id="named" for="pw">Secret</label><input id="pw" type="password"><textarea id="note"></textarea>'
        "<script>Object.defineProperty(named, `control`, {value: note});</script>",
        "secret",
    ),
    # A form with a plain field and a control named querySelectorAll, which form.querySelectorAll answers for.
    "a-form-with-a-control-named-querySelectorAll": (
        '<form id="named"><input type="hidden" name="querySelectorAll"><textarea name="note"></textarea></form>',
        "plain",
    ),
    # A form editable by what it is in, named as a secret, with a plain field and a control named isContentEditable.
    "a-form-with-a-control-named-isContentEditable": (
        '<div contenteditable><form id="named" aria-label="Password"><input type="hidden" name="isContentEditable">'
        '<textarea name="note"></textarea></form></div>',
        "secret",
    ),
}


async def _approval_read(tmp_path: Path, page: str, *, fullscreen_by: str | None = None) -> tuple[str, str]:
    """How the approval of a type by selector into #named on ``page`` reads its field, and the text it shows.
    ``fullscreen_by``: the element clicked first, as an earlier click of the agent's, which the page answers by
    making an element fullscreen (a page can do so only on a gesture)."""
    async with browser_env(tmp_path, {"/named": f"<title>named</title>{page}"}) as env:
        session_id = await env.create("/named")
        if fullscreen_by is not None:
            shown = env.manager.sessions[session_id].page
            await shown.locator(fullscreen_by).click()
            await shown.wait_for_function("() => document.fullscreenElement !== null", timeout=5000)
        change = BrowserActionDecision(
            action="type", reason="change it", selector="#named", text=TEXT, risk_category="post"
        )
        approval = await _asked_for(env, session_id, change)
    return approval["field_read"], approval["action"]["text"]


def _read_as(read: str) -> tuple[str, str]:
    """An approval's field read, and the text it shows by it."""
    return read, TEXT if read == "plain" else HIDDEN[read]


@pytest.mark.parametrize("case", list(ANSWERED_FOR_BY_THE_PAGE))
async def test_an_approval_reads_the_fields_the_browser_has_whatever_the_page_answers(
    tmp_path: Path, case: str
) -> None:
    # NAMED_FIELDS_SCRIPT read a label's control, the walk's querySelectorAll and the element's isContentEditable as
    # properties: the page's own control read plain where the label's real one is a password field; the form's
    # control made the read throw (unread); and the form's control hid that the form takes text, so its secret name
    # was not read and the approval said plain.
    page, read = ANSWERED_FOR_BY_THE_PAGE[case]

    assert await _approval_read(tmp_path, page) == _read_as(read)


# What an approval's read says where what takes text, as the browser has it, is not what the markup says.
TAKES_TEXT_AS_THE_BROWSER_HAS_IT = {
    # An input whose type has a space after it: a type the browser does not know, so a text field, named as a secret.
    "an-input-whose-type-is-hidden-and-a-space": (
        '<div id="named"><input type="hidden " name="password"><textarea></textarea></div>',
        "secret",
    ),
    # A text box named as a secret, editable by what it is in.
    "a-secret-text-box-editable-by-what-it-is-in": (
        '<div id="named" contenteditable><div role="textbox" aria-label="Password">x</div></div>',
        "secret",
    ),
    # B1-fix1 item 9 (orchestrator probe, Opus F15): inputs in an editor that take no text, a hidden token and a
    # checkbox, with names a secret's words are in. Every input in an editor is editable, and the editable test ran
    # before the input's type, so they read secret (1.18.1: plain). An input is a field by its type alone.
    "inputs-that-take-no-text-in-an-editor": (
        '<div id="named" contenteditable aria-label="Note"><p>x</p><input type="hidden" name="csrf_token">'
        '<input type="checkbox" name="otp_ok"></div>',
        "plain",
    ),
    # An element whose contenteditable says inherit, in nothing editable: it takes no text. Named as a secret beside
    # a plain field, so that counting it at all, as a field that takes text now or as one that could later, reads
    # otherwise than plain (B1-fix1 item 30, staged F15).
    "an-element-whose-contenteditable-is-inherit": (
        '<div id="named"><textarea></textarea><span contenteditable="inherit" aria-label="Password">x</span></div>',
        "plain",
    ),
}


@pytest.mark.parametrize("case", list(TAKES_TEXT_AS_THE_BROWSER_HAS_IT))
async def test_an_approval_counts_the_fields_the_browser_takes_text_in(tmp_path: Path, case: str) -> None:
    # NAMED_FIELDS_SCRIPT trimmed an input's type attribute before it looked the type up, so type="hidden " was no
    # field, though the browser has it as a text field; and it counted what is editable by the attribute, so an
    # element editable by what it is in was not counted, and one whose attribute says inherit, in nothing editable,
    # was. The field test now goes by the input's own type and by isContentEditable.
    page, read = TAKES_TEXT_AS_THE_BROWSER_HAS_IT[case]

    assert await _approval_read(tmp_path, page) == _read_as(read)


# An editor of 20,000 paragraphs, each with an id of its own.
AN_EDITOR_WHOSE_BLOCKS_HAVE_IDS = (
    '<div id="named" contenteditable aria-label="Note">'
    + "".join(f'<p id="p{i}">x</p>' for i in range(20000))
    + "</div>"
)


async def test_an_approval_reads_an_editor_whose_blocks_carry_ids_of_their_own_within_its_bound(tmp_path: Path) -> None:
    # B1-fix1 item 11 (Opus F3): every editable element was counted, and blocks alike in every attribute answered
    # once; blocks with ids of their own are not alike, and 20,000 of them took 3.3 s, past the read's bound of a
    # second: unread, where 1.18.1 read plain. An editor's editing host counts, and what in it carries a field's name
    # (any of its naming attributes but id): a block named only by its id is not read, as in 1.18.1.
    assert await _approval_read(tmp_path, AN_EDITOR_WHOSE_BLOCKS_HAVE_IDS) == _read_as("plain")


# What an approval's read says of fields that cannot take text now, beside what can.
TAKES_NO_TEXT_NOW = {
    # A disabled plain field beside a component the page lists as a text box, which takes its keys itself.
    "a-disabled-field-beside-a-component-that-takes-its-keys": (
        '<div id="named"><input disabled aria-label="Name"><div tabindex="0" role="textbox" aria-label="Code"></div>'
        "</div>",
        "unread",
    ),
    "a-read-only-field": ('<div id="named"><textarea readonly></textarea></div>', "unread"),
    "a-field-not-shown": ('<div id="named"><input aria-label="Note" hidden></div>', "unread"),
    # B1-fix1 item 8 (orchestrator probe, Opus F2): a field visibility hides, and one in an inert subtree (across a
    # shadow root too, where an inert host makes its shadow content inert and closest() does not see it), take no
    # text either; both read plain beside a component that takes its keys.
    "a-field-visibility-hides-beside-a-component-that-takes-its-keys": (
        '<div id="named"><input aria-label="Name" style="visibility: hidden">'
        '<div tabindex="0" role="textbox" aria-label="Code"></div></div>',
        "unread",
    ),
    "an-inert-field-beside-a-component-that-takes-its-keys": (
        '<div id="named"><div inert><input aria-label="Name"></div>'
        '<div tabindex="0" role="textbox" aria-label="Code"></div></div>',
        "unread",
    ),
    "a-field-in-an-inert-hosts-shadow-root-beside-a-component-that-takes-its-keys": (
        '<div id="named"><div id="host" inert></div><div tabindex="0" role="textbox" aria-label="Code"></div>'
        "</div><script>host.attachShadow({mode: 'open'}).innerHTML = '<input aria-label=Name>';</script>",
        "unread",
    ),
    # The control: a field that cannot take text now still says secret, for a page can enable it as the type focuses.
    "a-disabled-password-field-beside-a-plain-one": (
        '<div id="named"><input type="password" disabled><textarea></textarea></div>',
        "secret",
    ),
}


@pytest.mark.parametrize("case", list(TAKES_NO_TEXT_NOW))
async def test_an_approval_reads_plain_only_by_a_field_that_can_take_text_now(tmp_path: Path, case: str) -> None:
    # A disabled, read-only or hidden field counted as a field the text could go to, so a plain one made the approval
    # read plain and show the text, where the keys go to a component that takes them itself and names nothing.
    page, read = TAKES_NO_TEXT_NOW[case]

    assert await _approval_read(tmp_path, page) == _read_as(read)


# A plain field beside a frame of a kind the frame tags left out, whose document is not read.
BESIDE_A_FRAME = {
    "a-fenced-frame": '<div id="named"><textarea></textarea><fencedframe></fencedframe></div>',
    "a-portal": '<div id="named"><textarea></textarea><portal></portal></div>',
}


@pytest.mark.parametrize("frame", list(BESIDE_A_FRAME))
async def test_an_approval_does_not_read_plain_beside_a_fenced_frame_or_a_portal(tmp_path: Path, frame: str) -> None:
    # The frame tags were IFRAME, FRAME, OBJECT and EMBED, so a fenced frame or a portal was no frame to the read,
    # and a plain field beside one read plain, though the text can go into the frame's document.
    assert await _approval_read(tmp_path, BESIDE_A_FRAME[frame]) == _read_as("unread")


# A button the page lists, whose middle a fenced frame inside it fills.
A_FENCED_FRAME_IN_THE_BUTTON = (
    '<title>frame</title><div id="box" role="button" aria-label="Open" style="width: 300px; height: 150px">'
    '<fencedframe style="display: block; width: 300px; height: 150px; border: 0"></fencedframe></div>'
)


async def test_a_click_by_element_id_on_a_button_a_fenced_frame_fills_is_refused(tmp_path: Path) -> None:
    # What is in a frame is never what observe described, so a frame the pointer finds, even inside the element,
    # covers it (_COVER_AT_JS). A fenced frame was not among the frame tags, and the click went into it.
    async with browser_env(tmp_path, {"/frame": A_FENCED_FRAME_IN_THE_BUTTON}) as env:
        session_id = await env.create("/frame")
        items = (await env.manager.observe(session_id, preset="text"))["interactables"]
        (button,) = [item for item in items if item.get("label") == "Open"]

        with pytest.raises(BrowserActionError) as refused:
            await env.manager.click(session_id, element_id=button["element_id"])

        assert refused.value.code == "element_covered"


# A form the page lists as a text box, focusable, with controls named for the methods a hold puts its listeners on and
# takes them off with.
A_FORM_WITH_CONTROLS_NAMED_FOR_THE_LISTENER_METHODS = (
    '<title>note</title><form id="named" tabindex="0" role="textbox" aria-label="Note" style="height: 100px">'
    '<input type="hidden" name="addEventListener"><input type="hidden" name="removeEventListener"></form>'
)


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_hold_on_a_form_with_controls_named_for_the_listener_methods_is_taken(tmp_path: Path, by: str) -> None:
    # The hold called addEventListener and removeEventListener on its node, and a focused form's controls answer for
    # both: the take threw, which by element_id fails the type and by selector withholds the preview of a field that
    # reads plain. Called through EventTarget.prototype's own methods, the hold is taken and given back.
    async with browser_env(tmp_path, {"/note": A_FORM_WITH_CONTROLS_NAMED_FOR_THE_LISTENER_METHODS}) as env:
        _session, records = await _typed_into(env, "/note", by, "Note", "#named", NOTE)

        _assert_shown(records, NOTE)


# A button the page draws as a div, around a form whose hidden input is named parentNode: form.parentNode answers
# that input, which matches 'input'.
DELETE_IN_A_FORM = (
    '<title>account</title><div role="button" aria-label="Delete account"><form>'
    '<span id="words">Delete account</span><input type="hidden" name="parentNode"></form></div>'
)


@pytest.mark.parametrize("by", ["selector", "point"])
async def test_a_click_on_text_in_a_form_is_read_as_the_button_the_form_is_in(tmp_path: Path, by: str) -> None:
    # DESCRIBE_TARGET_SCRIPT's walk up to the button read parentNode as a property: from the form it went to the
    # form's hidden input, which it took for the click's target, no button, so a click labelled read on "Delete
    # account" went on unclassified. In both worlds: by selector in the page's, by a point in the element ids'.
    async with browser_env(tmp_path, {"/account": DELETE_IN_A_FORM}) as env:
        session_id = await env.create("/account")
        page = env.manager.sessions[session_id].page
        named: dict[str, Any] = {"selector": "#words"}
        if by == "point":
            box = await page.locator("#words").bounding_box()
            named = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}
        click = BrowserActionDecision(action="click", reason="look", risk_category="read", **named)

        target = await read_click_target(env.manager.element_ids, page, click, session_id=session_id)

        assert (target["tag"], target["role"]) == ("div", "button")
        assert classified_risk(target, "read") == "destructive"


# A button named parentNode in a form, under a layer of the form's that takes the pointer: form.parentNode answers
# the button.
UNDER_A_LAYER_OF_ITS_FORM = (
    '<title>layer</title><form style="position: relative"><button id="target" name="parentNode">Cancel</button>'
    '<div style="position: absolute; inset: 0"></div></form>'
)
# A form over a button outside it, with a hidden input named parentNode: from the form the walk went to the input,
# from the input back to the form, for good.
A_FORM_OVER_THE_BUTTON = (
    '<title>over</title><button id="target" style="position: absolute; left: 0; top: 0; width: 200px; height: 50px">'
    'Go</button><form style="position: absolute; left: 0; top: 0; width: 200px; height: 50px">'
    '<input type="hidden" name="parentNode"></form>'
)


@pytest.mark.parametrize("page", [UNDER_A_LAYER_OF_ITS_FORM, A_FORM_OVER_THE_BUTTON], ids=["layer", "over"])
async def test_the_pointer_check_finds_no_target_under_a_form_whose_control_is_named_parent_node(
    tmp_path: Path, page: str
) -> None:
    # _RECEIVES_POINTER_SCRIPT walked up from what the pointer hits by parentNode read as a property, with no bound:
    # the form's control took the walk to the button (the press then went to the layer over it), or round the form and
    # its control without end, which held the page. The pointer finds the form's layer, or the form, not the button.
    # The check answers its no itself, well within its bound, and the page answers after it: a no past the bound, or
    # an error, reads as the same False to the click (B1-fix1 item 31, staged F16).
    async with browser_env(tmp_path, {"/page": page}) as env:
        session_id = await env.create("/page")
        tab = env.manager.sessions[session_id].page
        locator = tab.locator("#target")
        box = await locator.bounding_box()
        point = [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2]

        loop = asyncio.get_running_loop()
        began = loop.time()
        found = await asyncio.wait_for(BrowserActionService._receives_pointer_at(locator, *point), 10)
        took = loop.time() - began
        answered = await asyncio.wait_for(locator.evaluate(_RECEIVES_POINTER_SCRIPT, [*point, None, False, False]), 5)

        assert found is False
        assert answered is False
        assert took < POINTER_CHECK_TIMEOUT_MS / 2000, took
        assert await asyncio.wait_for(tab.evaluate("() => 1 + 1"), 5) == 2


# A button the read click asks for by selector, on pages that answer for what the pointer check asks of the browser:
# a document's image named for its elementFromPoint, which it then stands in for; and a button two frames down, the
# frame between giving the frame it holds no box at all.
ANSWERED_FOR_BY_THE_POINTER_CHECKS_PAGE = {
    "elementFromPoint": (
        {
            "/go": '<title>go</title><img name="elementFromPoint" alt="">'
            "<button id=go onclick=\"document.title = 'PRESSED'\">Go</button>"
        },
        "#go",
    ),
    "getBoundingClientRect": (
        {
            "/go": "<title>go</title><iframe id=outer src=/outer></iframe>",
            "/outer": "<iframe id=inner src=/inner></iframe><script>document.getElementById('inner')"
            ".getBoundingClientRect = () => ({ left: 0, top: 0, width: 0, height: 0 });</script>",
            "/inner": "<button id=go onclick=\"top.document.title = 'PRESSED'\">Go</button>",
        },
        "#outer >> internal:control=enter-frame >> #inner >> internal:control=enter-frame >> #go",
    ),
}


@pytest.mark.parametrize("case", list(ANSWERED_FOR_BY_THE_POINTER_CHECKS_PAGE))
async def test_a_read_click_by_selector_presses_whatever_its_page_answers_for_the_pointer_check(
    tmp_path: Path, case: str
) -> None:
    # B1-fix1 item 16 (Opus F13): by selector the pointer check runs in the page's own world, and asked
    # document.elementFromPoint and getBoundingClientRect as properties: a document's named image stood in for the
    # one (the check threw, a no), and a frame's own method gave the box the point was carried into it by, so a page
    # refused, or moved, where the click was checked. Each is asked of its prototype, as DOM_JS's reads are.
    pages, selector = ANSWERED_FOR_BY_THE_POINTER_CHECKS_PAGE[case]
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/go")
        click = BrowserActionDecision(action="click", reason="go", risk_category="read", selector=selector)

        await env.manager.execute_decision(session_id, click)

        assert await env.manager.sessions[session_id].page.title() == "PRESSED"


# B1-fix1 item 17 (Opus T1): the reads by selector no test noticed. A label for a "Delete account" button that the
# page gives no control of its own; a button under a label for it that the page gives no control; and a word in a
# button drawn as a div, under a layer of the button's, that the page calls editable.
A_LABEL_FOR_DELETE_WITH_NO_CONTROL_OF_ITS_OWN = (
    "<title>label</title><label id=words for=delete>Read more</label><button id=delete>Delete account</button>"
    "<script>Object.defineProperty(document.getElementById('words'), 'control', { value: null });</script>"
)
A_BUTTON_UNDER_ITS_LABEL_WITH_NO_CONTROL_OF_ITS_OWN = (
    "<title>label</title><div style='position: relative; width: 200px; height: 50px'>"
    "<button id=target style='position: absolute; inset: 0'>Go</button>"
    "<label id=over for=target style='position: absolute; inset: 0'></label></div>"
    "<script>Object.defineProperty(document.getElementById('over'), 'control', { value: null });</script>"
)
A_WORD_THE_PAGE_CALLS_EDITABLE_UNDER_ITS_BUTTONS_LAYER = (
    "<title>word</title><div role=button style='position: relative; width: 200px; height: 50px'>"
    "<span id=target>Go</span><i style='position: absolute; inset: 0'></i></div>"
    "<script>Object.defineProperty(document.getElementById('target'), 'isContentEditable', { value: true });</script>"
)


async def test_a_click_by_selector_on_a_label_the_page_gives_no_control_is_read_as_its_button(
    tmp_path: Path,
) -> None:
    # C2: DESCRIBE_TARGET_SCRIPT takes a press on a label for its control, through HTMLLabelElement.prototype's.
    async with browser_env(tmp_path, {"/label": A_LABEL_FOR_DELETE_WITH_NO_CONTROL_OF_ITS_OWN}) as env:
        session_id = await env.create("/label")
        page = env.manager.sessions[session_id].page
        click = BrowserActionDecision(action="click", reason="look", risk_category="read", selector="#words")

        target = await read_click_target(env.manager.element_ids, page, click, session_id=session_id)

        assert target["tag"] == "button"
        assert classified_risk(target, "read") == "destructive"


@pytest.mark.parametrize(
    ("page", "placed"),
    [
        (A_BUTTON_UNDER_ITS_LABEL_WITH_NO_CONTROL_OF_ITS_OWN, False),
        (A_WORD_THE_PAGE_CALLS_EDITABLE_UNDER_ITS_BUTTONS_LAYER, True),
    ],
    ids=["label-control", "isContentEditable"],
)
async def test_the_pointer_check_by_selector_reads_what_the_browser_has(
    tmp_path: Path, page: str, placed: bool
) -> None:
    # A2 and A3: _RECEIVES_POINTER_SCRIPT takes a hit on a label for the target as one on the target, through the
    # label's prototype; and, where the click was placed, a word in a button as the button, by whether the word is
    # editable as the browser has it. Read off the page's nodes, both said no here.
    async with browser_env(tmp_path, {"/page": page}) as env:
        session_id = await env.create("/page")
        locator = env.manager.sessions[session_id].page.locator("#target")
        box = await locator.bounding_box()

        found = await asyncio.wait_for(
            BrowserActionService._receives_pointer_at(
                locator, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, placed=placed
            ),
            10,
        )

        assert found is True


# An editor of 20,000 paragraphs alike, each named for a line.
AN_EDITOR_WHOSE_BLOCKS_ARE_NAMED_ALIKE = (
    '<div id="named" contenteditable aria-label="Note">' + '<p name="line">x</p>' * 20000 + "</div>"
)


async def test_an_approval_reads_an_editor_whose_named_blocks_are_alike_within_its_bound(tmp_path: Path) -> None:
    # E15: fields alike in every attribute are answered once. Each answered on its own, 20,000 took past the read's
    # bound of a second: unread.
    assert await _approval_read(tmp_path, AN_EDITOR_WHOSE_BLOCKS_ARE_NAMED_ALIKE) == _read_as("plain")


# A button the page draws as a div, whose middle a frame of a kind the frame tags left out fills.
A_BUTTON_A_FRAME_FILLS = {
    tag: '<title>frame</title><div id="box" role="button" aria-label="Open" style="width: 300px; height: 150px">'
    f'<{tag} style="display: block; width: 300px; height: 150px; border: 0"></{tag}></div>'
    for tag in ("fencedframe", "portal")
}


@pytest.mark.parametrize("tag", list(A_BUTTON_A_FRAME_FILLS))
async def test_two_points_on_a_fenced_frame_or_a_portal_are_never_taken_for_one_element(
    tmp_path: Path, tag: str
) -> None:
    # B1-fix1 item 19 (Opus F9): SAME_ELEMENT_SCRIPT's frame tags were IFRAME, FRAME, OBJECT and EMBED, so two points
    # on a fenced frame or a portal read as one element, and a click by coordinates there kept its jitter, though
    # inside the frame two points may be two controls. It takes them as frames, as DOM_JS's dom.frame does.
    async with browser_env(tmp_path, {"/frame": A_BUTTON_A_FRAME_FILLS[tag]}) as env:
        session_id = await env.create("/frame")
        page = env.manager.sessions[session_id].page
        box = await page.locator("#box").bounding_box()
        point = (box["x"] + 150, box["y"] + 75)

        assert await same_element_at(env.manager.element_ids, page, point, (point[0] + 2, point[1] + 1)) is False


@pytest.mark.parametrize("tag", list(A_BUTTON_A_FRAME_FILLS))
async def test_a_pointer_a_fenced_frame_or_a_portal_in_the_target_holds_is_found_over_the_target(
    tmp_path: Path, tag: str
) -> None:
    # B1-fix1 item 19 (Opus F9): where the target's document saw no move of the pointer arrive, the pointer must be
    # over a frame inside the target (`onlyFrames`), and the frames it took were the four tags: one over a fenced
    # frame or a portal in the target was a no. It takes them as frames, as dom.frame does.
    async with browser_env(tmp_path, {"/frame": A_BUTTON_A_FRAME_FILLS[tag]}) as env:
        session_id = await env.create("/frame")
        locator = env.manager.sessions[session_id].page.locator("#box")
        box = await locator.bounding_box()
        point = [box["x"] + 150, box["y"] + 75]

        found = await locator.evaluate(_RECEIVES_POINTER_SCRIPT, [*point, True, False, False, True])

        assert found is True


# B1-fix2 items 1 to 3: an editor at the top of a shadow root inside an editor; plain fields a modal dialog makes inert,
# or one that escapes an inert subtree; and an inert ancestor past the walk's 256 steps.
A_COMPONENT = '<div tabindex="0" role="textbox" aria-label="Code"></div>'
READS_AS_THE_BROWSER_TAKES_ITS_KEYS = {
    # 1: the shadow root's own editor takes focus and the text (Chromium 156, measured): an editing host.
    "an-editor-at-the-top-of-a-shadow-root-in-an-editor": (
        '<div id="named" contenteditable aria-label="Note"><p>hi</p><x-host id="h"></x-host></div>'
        "<script>h.attachShadow({mode: 'open'}).innerHTML ="
        ' \'<div contenteditable id="password" style="min-height: 20px">s</div>\';</script>',
        "secret",
    ),
    # 2a: a plain field outside an open modal dialog takes no focus.
    "a-plain-field-behind-a-modal-dialog": (
        f'<div id="named"><input aria-label="Name">{A_COMPONENT}</div><dialog id="d"><button>x</button></dialog>'
        "<script>d.showModal();</script>",
        "unread",
    ),
    # 2b: a modal dialog escapes the inert subtree it is in, and a plain field in it takes focus.
    "a-plain-field-in-a-modal-dialog-in-an-inert-subtree": (
        '<div inert><dialog id="d"><div id="named"><input aria-label="Name"></div></dialog></div>'
        "<script>d.showModal();</script>",
        "plain",
    ),
    # 3: an inert ancestor 259 nodes up, past the walk's 256 steps (display: contents builds no boxes; a page whose
    # boxes are about 258 deep crashes this Chromium's renderer).
    "a-plain-field-under-an-inert-ancestor-past-the-walk": (
        '<div id="d0" inert></div><script>let at = document.getElementById("d0");'
        ' for (let i = 0; i < 258; i += 1) { const d = document.createElement("div");'
        ' d.style.display = "contents"; at.appendChild(d); at = d; }'
        ' const n = document.createElement("div"); n.id = "named";'
        f" n.innerHTML = '<input aria-label=Name>{A_COMPONENT}'; at.appendChild(n);</script>",
        "unread",
    ),
    # B1-fix5 item 1 (Codex round 5 #1, Opus round 5 F1): only the top open modal dialog escapes an inert subtree.
    # A bottom one (opened first) under [inert] in the top one: its plain field takes no focus, the component beside
    # it does (Chromium 156, measured).
    "a-plain-field-in-a-bottom-modal-dialog-under-inert-in-the-top-one": (
        f"<dialog id='outer'><div id='named'><div inert><dialog id='inner'><input aria-label='Name'></dialog></div>"
        f"{A_COMPONENT}</div></dialog><script>document.getElementById('inner').showModal();"
        " document.getElementById('outer').showModal();</script>",
        "unread",
    ),
    # The top one nested in the bottom one, no inert: its field takes focus.
    "a-plain-field-in-a-top-modal-dialog-nested-in-the-bottom-one": (
        "<dialog id='d1'><dialog id='d2'><div id='named'><input aria-label='Name'></div></dialog></dialog>"
        "<script>document.getElementById('d1').showModal(); document.getElementById('d2').showModal();</script>",
        "plain",
    ),
    # A shadow root's modal dialog (opened first) under [inert] in a light one opened last: its field takes no focus.
    "a-plain-field-in-a-shadow-roots-modal-dialog-under-inert-in-the-top-one": (
        f"<dialog id='d2'><div id='named'><div inert><x-host id='h'></x-host></div>{A_COMPONENT}</div></dialog>"
        "<script>const r = document.getElementById('h').attachShadow({mode: 'open'});"
        ' r.innerHTML = \'<dialog id="d1"><input aria-label="Name"></dialog>\';'
        " r.getElementById('d1').showModal(); document.getElementById('d2').showModal();</script>",
        "unread",
    ),
    # B1-fix6 item 2 (the orchestrator's mutant M5-dialog-first): a modal dialog that is itself inert. Its plain field
    # takes no focus (Chromium 156, measured): the walk reads the dialog's own inert before it counts the dialog.
    "a-plain-field-in-a-modal-dialog-that-is-itself-inert": (
        f"<dialog id='d' inert><div id='named'><input aria-label='Name'>{A_COMPONENT}</div></dialog>"
        "<script>document.getElementById('d').showModal();</script>",
        "unread",
    ),
}


# B1-fix3 item 5: an element the page makes fullscreen (on #go) is modal, as an open modal dialog is.
GOES_FULLSCREEN = "<button id='go' onclick=\"document.getElementById('full').requestFullscreen()\">Go</button>"
IN_A_SHADOW_ROOT = (
    "<script>h.attachShadow({mode: 'open'}).innerHTML = "
    f'\'<div id="full" style="height: 60px">{A_COMPONENT}</div>\';</script>'
    "<button id='go' onclick=\"h.shadowRoot.getElementById('full').requestFullscreen()\">Go</button>"
)
READS_AS_THE_BROWSER_TAKES_ITS_KEYS_IN_FULLSCREEN = {
    # A plain field outside the fullscreen element takes no focus, and the component inside it does (Chromium 156).
    "a-plain-field-outside-a-fullscreen-element": (
        f'<div id="named"><input aria-label="Name"><div id="full" style="height: 60px">{A_COMPONENT}</div></div>'
        + GOES_FULLSCREEN,
        "unread",
    ),
    # A fullscreen element escapes the inert subtree it is in, and a plain field in it takes focus.
    "a-plain-field-in-a-fullscreen-element-in-an-inert-subtree": (
        '<div inert><div id="full"><div id="named"><input aria-label="Name"></div></div></div>' + GOES_FULLSCREEN,
        "plain",
    ),
    # One in a shadow root is not found by the document's query: the host the document names fullscreen stands in.
    "a-plain-field-outside-the-host-of-a-fullscreen-element-in-a-shadow-root": (
        '<div id="named"><input aria-label="Name"><x-host id="h"></x-host></div>' + IN_A_SHADOW_ROOT,
        "unread",
    ),
    # B1-fix4 item 2: while a modal dialog is open it alone decides, and the fullscreen element escapes no inert
    # subtree. A plain field in a fullscreen element under [inert] in the dialog takes no focus; the component beside
    # it does (the click that makes the element fullscreen is in the dialog, the page's only part that takes one).
    "a-plain-field-in-a-fullscreen-element-under-inert-in-a-modal-dialog": (
        "<dialog id='d'><button id='go' onclick=\"document.getElementById('full').requestFullscreen()\">Go</button>"
        f'<div id="named"><div inert><div id="full" style="height: 60px"><input aria-label="Name"></div></div>'
        f"{A_COMPONENT}</div></dialog><script>d.showModal();</script>",
        "unread",
    ),
    # A plain field in a modal dialog, beside a fullscreen element in the dialog, takes focus.
    "a-plain-field-in-a-modal-dialog-beside-a-fullscreen-element-in-it": (
        "<dialog id='d'><div id=\"named\"><input aria-label=\"Name\"></div><div id='full' style='height: 60px'>v</div>"
        "<button id='go' onclick=\"document.getElementById('full').requestFullscreen()\">Go</button></dialog>"
        "<script>d.showModal();</script>",
        "plain",
    ),
    # So does one in a modal dialog a page fullscreen on a container opens outside it.
    "a-plain-field-in-a-modal-dialog-opened-outside-the-fullscreen-element": (
        "<div id='full' style='height: 60px'>v</div><dialog id='d'><div id=\"named\"><input aria-label=\"Name\"></div>"
        "</dialog>"
        + GOES_FULLSCREEN
        + "<script>full.addEventListener('fullscreenchange', () => d.showModal());</script>",
        "plain",
    ),
}


@pytest.mark.parametrize("case", list(READS_AS_THE_BROWSER_TAKES_ITS_KEYS_IN_FULLSCREEN))
async def test_an_approval_reads_the_fields_as_the_browser_gives_them_keys_in_fullscreen(
    tmp_path: Path, case: str
) -> None:
    # B1-fix3 item 5 (Opus round 3 #1, reproduced by the orchestrator, S/b1/orch3/probe_fs.py). The read took a
    # modal dialog for what is modal, where Chromium has a fullscreen element so too (:modal): beside a component in
    # it, a plain decoy outside, which takes no keys, read plain (2a's bypass again, the text shown), and a plain
    # field in a fullscreen element in an inert subtree read idle.
    page, read = READS_AS_THE_BROWSER_TAKES_ITS_KEYS_IN_FULLSCREEN[case]
    assert await _approval_read(tmp_path, page, fullscreen_by="#go") == _read_as(read)


@pytest.mark.parametrize("case", list(READS_AS_THE_BROWSER_TAKES_ITS_KEYS))
async def test_an_approval_reads_the_fields_as_the_browser_gives_them_keys(tmp_path: Path, case: str) -> None:
    # B1-fix2 items 1 to 3 (Codex round 2 #1, #2, #3). An editing host was one whose element above, across a shadow
    # root, was not editable, so an editor at the top of a shadow root inside an editor, named by its id alone, was not
    # read (335257c4 read it: secret). The inert walk knew only the inert attribute, so a field a modal dialog made
    # inert counted as live, and one in a modal dialog that escaped an inert subtree as idle; and it said not inert
    # once it ran out of steps.
    page, read = READS_AS_THE_BROWSER_TAKES_ITS_KEYS[case]
    assert await _approval_read(tmp_path, page) == _read_as(read)

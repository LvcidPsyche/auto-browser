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
from app.browser.click_target import classified_risk, read_click_target
from app.browser.services.actions import BrowserActionService
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


async def _approval_read(tmp_path: Path, page: str) -> tuple[str, str]:
    """How the approval of a type by selector into #named on ``page`` reads its field, and the text it shows."""
    async with browser_env(tmp_path, {"/named": f"<title>named</title>{page}"}) as env:
        session_id = await env.create("/named")
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
    # An element whose contenteditable says inherit, in nothing editable: it takes no text.
    "an-element-whose-contenteditable-is-inherit": (
        '<div id="named"><span contenteditable="inherit">x</span></div>',
        "unread",
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
    async with browser_env(tmp_path, {"/page": page}) as env:
        session_id = await env.create("/page")
        locator = env.manager.sessions[session_id].page.locator("#target")
        box = await locator.bounding_box()

        found = await asyncio.wait_for(
            BrowserActionService._receives_pointer_at(
                locator, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            ),
            10,
        )

        assert found is False

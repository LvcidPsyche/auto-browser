"""Every read that gates a click or a type, and observe, answer on a page whose forms and document carry a named
control for every DOM property as they answer on the same page without them, and the action ends as it ends there.
Real Chromium, through the real BrowserManager.

A form's named controls answer for the form's own properties, and for names it has no property of, in every
JavaScript world, and a document's named elements answer for the document's in the page's own world. The names are
the browser's: the own property names of every prototype on a form's chain (HTMLFormElement.prototype up to
Object.prototype) and of every element interface's, and those on the document's chain and of every document
interface's. A hostile page is the control page with, added once the controller has observed it, a control in each form
for each form name, a hidden input on one page and a select holding text on another (CONTROLS), and a named img for
each document name; observe is then asked again. A click page has a third hostile page, two selects of each name in
each form (TWO_OF_EACH_NAME), read by element_id and by a point only. Observe is asked as well on pages with the names
split (ENDS_THE_WORK_EARLY), and a type that goes ahead on pages with the names split (STOPS_PLAYWRIGHT,
GOES_ANOTHER_WAY). Each read runs as the controller runs it: by selector in the page's own world, by element_id and by
a point in the element ids' world, and observe's in the element ids' world. Apart: a form with a plain field and a
control named getAttribute or querySelectorAll, before a type into that field; a form drawn as a button and a control
named labels; a form around a password field and a control named hasAttribute; a button that a form's text names;
forms under something disabled and a control named isContentEditable; a form under a header that a scroll uncovers and
a control named scrollIntoView; and what the enabled read follows a target to, beside Playwright's own check.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest

from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError
from app.browser.click_target import (
    _LABEL_CUT,
    CLICK_TARGET_SCRIPT,
    READ_TIMEOUT_MS,
    PressNeedsApproval,
    _read_selected,
    reading_presses,
    refuse_press_not_asked_about,
    same_element_at,
)
from app.browser.element_scripts import (
    ELEMENT_ENABLED_SCRIPT,
    ELEMENT_FOCUS_PLACE_SCRIPT,
    ELEMENT_HAS_FOCUS_SCRIPT,
    FIELD_ATTRIBUTES_SCRIPT,
    FIELD_HOLD_READ_SCRIPT,
    FIELD_HOLD_RELEASE_SCRIPT,
    FIELD_HOLD_TAKE_SCRIPT,
)
from app.browser.element_world import READ
from app.browser.services.actions import (
    _POINTER_SEEN_SCRIPT,
    _POINTER_WATCH_SCRIPT,
    _POINTER_WATCH_STOP_SCRIPT,
    _PRESS_PLACE_SCRIPT,
    _RECEIVES_POINTER_SCRIPT,
    FIELD_RECHECK_TIMEOUT_MS,
)
from app.models import BrowserActionDecision
from tests._chromium import requires_chromium
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium: that module's autouse browser
    _shared_chromium,
    browser_env,
)
from tests.test_secret_field_receipts_chromium import TEXT

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

# Counts the presses and the keys that reach the page: on the control page a click labelled read on "Remove" or
# "Delete account" is held for approval, and nothing is pressed.
COUNTS_PRESSES = (
    "<title>named</title><script>window.presses = 0; window.keys = 0;"
    " addEventListener('click', (event) => { if (event.isTrusted) window.presses += 1; }, true);"
    " addEventListener('keydown', (event) => { if (event.isTrusted) window.keys += 1; }, true);</script>"
)
# Where the named imgs go: hidden, so that they change no box.
NAMED_ELEMENTS = '<div id="named-elements" hidden></div>'
# Hands focus from the type's target to the field in it, as a component does.
HANDS_FOCUS_ON = (
    "<script>(() => { const named = document.getElementById('target');"
    " const field = document.getElementById('body'); named.addEventListener('focus', () => field.focus()); })();"
    "</script>"
)
FIELDS = '<textarea id="body" aria-label="Body"></textarea><input type="password" aria-label="Pin">'
BUTTON = 'role="button" style="width: 240px; height: 60px"'
NOTE = 'tabindex="0" role="textbox" aria-label="Note" style="height: 200px"'
# A glyph whose words are in its open shadow root, which a read of the button's label gathers.
GLYPH = (
    '<span id="glyph"></span>'
    "<script>document.getElementById('glyph').attachShadow({mode: 'open'}).innerHTML = '<b>Delete</b>';</script>"
)
# A form a click page's target is, by its id, focusable so that observe lists it, and in harmless words.
FORM_TARGET = '<form id="{id}" data-form tabindex="0" style="width: 240px; height: 40px">{words}</form>'
# Three such forms, each under something whose being disabled the enabled read follows the form to, as Playwright's
# enabled check does (element_scripts._FOLLOW_LABEL_JS): a role=button that is aria-disabled, a button disabled by its
# attribute (the form put in it by script: markup cannot), and a label whose control is a disabled input (before the
# form, so that a select the test adds to the form is not the label's control).
UNDER_SOMETHING_DISABLED = (
    '<div role="button" aria-disabled="true">' + FORM_TARGET.format(id="target", words="Open") + "</div>"
    '<button id="off" disabled style="display: block"></button>'
    + FORM_TARGET.format(id="in-a-button", words="Show")
    + "<script>document.getElementById('off').append(document.getElementById('in-a-button'));</script>"
    '<label><input aria-label="Off" disabled>' + FORM_TARGET.format(id="in-a-label", words="Name") + "</label>"
)
DISABLED_TARGETS = ("#target", "#in-a-button", "#in-a-label")
# Each page: its markup, inside COUNTS_PRESSES and NAMED_ELEMENTS. A form carries data-form; the action names #target,
# or each target TARGETS names. A click page holds a "Delete account" button, or a "Remove" one where the form is the
# target (a select's "Remove filter" read as its name would excuse it), or forms under something disabled, which a click
# waits on to be enabled; a type page a plain field, which takes the text (FIELD names it), and a password field, or
# the password field alone, which takes the text and fills the form that is the target, or a form that is the target
# and takes the text itself. Where the form is inside the button it has a title and the glyph, which the button's label
# is read from too.
CLICK_PAGES = {
    "the-form-is-the-target": f'<form id="target" data-form {BUTTON}>Remove</form>',
    "the-form-is-inside-the-target": (
        f'<div id="target" aria-label="Delete account" {BUTTON}>'
        f'<form data-form title="Account" style="height: 60px">{GLYPH}</form></div>'
    ),
    "the-form-is-an-ancestor-of-the-target": (
        f'<form data-form><div id="target" aria-label="Delete account" {BUTTON}>Delete account</div></form>'
    ),
    "the-form-is-the-target-under-something-disabled": UNDER_SOMETHING_DISABLED,
}
TARGETS = {"the-form-is-the-target-under-something-disabled": DISABLED_TARGETS}
# A click's budget on these pages: a click on a target that is not enabled waits that long for it.
CLICK_BUDGET_MS = 1000
TYPE_PAGES = {
    "the-form-is-the-target": f'<form id="target" data-form {NOTE}>{FIELDS}</form>{HANDS_FOCUS_ON}',
    "the-form-is-inside-the-target": f'<div id="target" {NOTE}><form data-form>{FIELDS}</form></div>{HANDS_FOCUS_ON}',
    "the-form-is-an-ancestor-of-the-target": (
        '<form data-form><textarea id="target" aria-label="Body"></textarea><input type="password" aria-label="Pin">'
        "</form>"
    ),
    "the-form-is-the-target-and-the-field-fills-it": (
        '<form id="target" data-form tabindex="-1" aria-label="Sign in" style="width: 300px">'
        '<input id="pin" type="password" aria-label="Pin" style="display: block; box-sizing: border-box; width: 300px">'
        "</form>"
    ),
    "the-form-is-the-target-and-takes-the-text": (
        '<form id="target" data-form contenteditable="true" tabindex="0" aria-label="Password" style="min-height: 100px">'
        "</form>"
    ),
}
FIELD = {
    "the-form-is-the-target": "#body",
    "the-form-is-inside-the-target": "#body",
    "the-form-is-the-target-and-the-field-fills-it": "#pin",
}
# Before a type that goes ahead: every field emptied, focus taken off what has it, through the prototypes, and the
# keys counted from none.
EMPTIES_THE_FIELDS = """() => {
  const fields = Document.prototype.querySelectorAll.call(document, 'textarea, input:not([type=hidden])');
  for (const el of fields) Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set.call(el, '');
  const focused = Object.getOwnPropertyDescriptor(Document.prototype, 'activeElement').get.call(document);
  if (focused) HTMLElement.prototype.blur.call(focused);
  window.keys = 0;
}"""
# Focus put in the element a selector names, through the prototypes: Playwright's own focus reads the element's
# dispatchEvent, which a form that takes the text answers for.
FOCUSES = "(selector) => HTMLElement.prototype.focus.call(Document.prototype.querySelector.call(document, selector))"
# Left out of the form's names where the form is the target: Playwright reads it off the element a locator finds,
# to name that element in its log, before a script is run on it.
READ_BY_THE_LOCATOR = ("nodeName",)

# The own property names of every prototype on a form's chain and of every element interface's (each constructor of
# the window whose prototype chain reaches Element.prototype: HTML, SVG, MathML), and of every prototype on the
# document's chain and of every document interface's.
PROPERTY_NAMES = """() => {
  const chain = (start) => {
    const names = [];
    for (let proto = start; proto; proto = Object.getPrototypeOf(proto)) {
      names.push(...Object.getOwnPropertyNames(proto));
    }
    return names;
  };
  const interfaces = (base) => Object.getOwnPropertyNames(window).flatMap((name) => {
    const value = Object.getOwnPropertyDescriptor(window, name).value;
    return typeof value === 'function' && value.prototype && base.isPrototypeOf(value.prototype)
      ? Object.getOwnPropertyNames(value.prototype)
      : [];
  });
  const named = (start, base) => [...new Set([...chain(start), ...interfaces(base)])];
  return {
    form: named(HTMLFormElement.prototype, Element.prototype),
    document: named(Object.getPrototypeOf(document), Document.prototype),
  };
}"""
# The control a form is given for each of its names, each kind on a hostile page of its own: a hidden input, or a
# stand-in that is truthy, array-like and holds text, a select whose one option goes on from a click target's own words
# ("Remove") harmlessly (click_target's own pair). Under one name the two make the form answer with a list of both, on
# which Playwright's own locator finds no element.
CONTROLS = {
    "hidden inputs": '<input type="hidden" name="{name}">',
    "selects": '<select name="{name}" hidden><option>Remove filter</option></select>',
}
# The third kind, on a click page: two selects of each name, which the form answers with a list of both (a
# RadioNodeList), truthy, array-like and holding text. Read by element_id and by a point only, the element_id and the
# point taken before the names are added: on such a page Playwright's own locator finds no element, even for :root
# ("TypeError: root.querySelectorAll is not a function or its return value is not iterable", measured).
TWO_OF_EACH_NAME = CONTROLS["selects"] * 2
TWO_OF_EACH_NAME_KIND = "two selects of each name"
# Adds, in one call, a control for each form name to each form and a named img for each document name: every place is
# found before any is added to.
ADDS_THE_NAMES = """([formNames, documentNames, control]) => {
  const forms = document.querySelectorAll('[data-form]');
  const elements = document.getElementById('named-elements');
  const quoted = (name) => name.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
  const controls = formNames.map((name) => control.split('{name}').join(quoted(name)));
  const imgs = documentNames.map((name) => `<img name="${quoted(name)}" alt="">`);
  for (const form of forms) form.insertAdjacentHTML('beforeend', controls.join(''));
  elements.insertAdjacentHTML('beforeend', imgs.join(''));
}"""
# What an element_id observe gave names: the element's tag and id, read through the prototypes.
RESOLVES_TO = (
    "(el) => [Object.getOwnPropertyDescriptor(Element.prototype, 'tagName').get.call(el),"
    " Element.prototype.getAttribute.call(el, 'id')]"
)

# A form observe lists by its tabindex alone, with nothing but its id to be named by and focus in it: observe's naming
# of the form and of the focused element reads the form down to its last fallback.
FOCUSED_FORM = "the-focused-form-is-named-by-its-id"
# A field in a form that its aria-labelledby names, with focus in it: observe lists it, reports it as focused and lists
# it among the form's fields, and names it each time by the form's text.
LABELLED_BY_THE_FORM = "the-form-is-what-the-focused-fields-aria-labelledby-names"
# Observe's pages: each click and type page, the focused form and the field the form names.
OBSERVED_PAGES = {
    **{f"click-{placement}": markup for placement, markup in CLICK_PAGES.items()},
    **{f"type-{placement}": markup for placement, markup in TYPE_PAGES.items()},
    FOCUSED_FORM: (
        '<form id="target" data-form tabindex="0" style="height: 60px"></form>'
        "<script>document.getElementById('target').focus();</script>"
    ),
    LABELLED_BY_THE_FORM: (
        '<form id="labelling" data-form>Delete account <input id="field" aria-labelledby="labelling"></form>'
        "<script>document.getElementById('field').focus();</script>"
    ),
}
# The names at which a read of the form made as a property, answered by a control, ends observe's work on the form
# (each run alone on these pages): a method's or tagName's throws and ends the script, innerText's names the form
# before its images and attributes are read, and isContentEditable's takes the form for a field, whose text is not
# read. With one of them on a page, such a read hides every read of the form after it, so each is on a page of its
# own, and the rest of the names are on another.
ENDS_THE_WORK_EARLY = (
    "getAttribute",
    "querySelectorAll",
    "querySelector",
    "closest",
    "getBoundingClientRect",
    "tagName",
    "innerText",
    "isContentEditable",
)

# A form with a plain field; with a control named after a DOM property, a hidden input so named, after the field.
PLAIN_FORM = '<form action="/save" method="post"><input id="f" type="text" aria-label="Note">{control}</form>'
# What a type writes into the plain field; what the field holds, and emptying it, through the prototypes.
WRITTEN = "a note"
THE_FIELD = "Document.prototype.querySelector.call(document, '#f')"
FIELD_HOLDS = f"() => Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').get.call({THE_FIELD})"
EMPTIES_THE_FIELD = (
    f"() => Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call({THE_FIELD}, '')"
)


def _first_line(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {lines[0][:200] if lines else ''}"


async def _answer(call: Any) -> Any:
    """What a read answered, or the first line of what it raised."""
    try:
        value = await call
    except BrowserActionError as refused:
        return f"refused ({refused.code})"
    except Exception as exc:  # noqa: BLE001 - every failure is an answer to compare
        return f"raised {_first_line(exc)}"
    return json.loads(json.dumps(value, default=str))


async def _targets(env: Any, session_id: str, page: Any, selectors: Collection[str]) -> list[dict[str, Any]]:
    """Each target ``selectors`` name by its id: the selector, the element_id observe gives it and its box."""
    listed = [item["element_id"] for item in (await env.manager.observe(session_id, preset="text"))["interactables"]]
    ids = {
        (await env.manager.element_ids.element(page, one).evaluate(RESOLVES_TO, effect=READ))[1]: one for one in listed
    }
    return [
        {"selector": selector, "element_id": ids[selector[1:]], "box": await page.locator(selector).bounding_box()}
        for selector in selectors
    ]


async def _session(env: Any, placement: str, control: str | None) -> tuple[str, Any, list[dict[str, Any]], list[str]]:
    """A session on the page, observed (for each target's element_id and box, _targets), then on a hostile page given
    the names, each as ``control`` (CONTROLS); and the form names it was given."""
    session_id = await env.create("/page")
    page = env.manager.sessions[session_id].page
    targets = await _targets(env, session_id, page, TARGETS.get(placement, ("#target",)))
    form_names: list[str] = []
    if control is not None:
        names = await page.evaluate(PROPERTY_NAMES)
        left_out = READ_BY_THE_LOCATOR if placement.startswith("the-form-is-the-target") else ()
        form_names = [name for name in names["form"] if name not in left_out]
        await page.evaluate(ADDS_THE_NAMES, [form_names, names["document"], control])
    return session_id, page, targets, form_names


def _added_field(field: dict[str, Any], added: Collection[str]) -> bool:
    """Whether a field of the page summary's form is one of the controls the test added (ADDS_THE_NAMES)."""
    return field["name"] in added and (field["type"] == "hidden" or field["tag"] == "select")


async def _what_observe_answers(env: Any, page: Any, seen: Any, added: Collection[str]) -> Any:
    """Of an observation (or what observe raised): each element it lists, with what its element_id names, and its page
    summary's forms, without the hidden inputs the test added, and focused element, whose element_id is given as its
    place in the list."""
    if not isinstance(seen, dict):
        return seen
    ids = [item["element_id"] for item in seen["interactables"]]
    listed = []
    for item in seen["interactables"]:
        shown = {key: value for key, value in item.items() if key not in ("element_id", "selector_hint")}
        named = await _answer(
            env.manager.element_ids.element(page, item["element_id"]).evaluate(RESOLVES_TO, effect=READ)
        )
        listed.append(shown | {"names": named})
    forms = [
        form | {"fields": [field for field in form["fields"] if not _added_field(field, added)]}
        for form in seen["dom_outline"]["forms"]
    ]
    focused = seen["active_element"]
    if isinstance(focused, dict) and focused.get("element_id") in ids:
        focused = focused | {"element_id": f"listed {ids.index(focused['element_id'])}"}
    return {"listed": listed, "forms": forms, "focused": focused}


async def _observed(env: Any, session_id: str, page: Any, added: Collection[str]) -> Any:
    """What observe answers on the session's page (_what_observe_answers)."""
    seen = await _answer(env.manager.observe(session_id, preset="text"))
    return await _what_observe_answers(env, page, seen, added)


async def _click_reads(
    env: Any, session_id: str, page: Any, target: dict[str, Any], *, by_selector: bool = True
) -> dict[str, Any]:
    """Every read a click on the target (_targets) is gated by, each as the controller makes it, at the middle of the
    box observed; with ``by_selector`` False, those made by element_id and by a point only."""
    ids = env.manager.element_ids
    locator = page.locator(target["selector"]).first
    box = target["box"]
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    element_id = target["element_id"]
    element = ids.element(page, element_id)
    bounds = {"timeout_ms": READ_TIMEOUT_MS, "setup_timeout_ms": READ_TIMEOUT_MS, "keep_session": True}
    reads: dict[str, Any] = {}
    if by_selector:
        reads["description by selector"] = await _answer(_read_selected(page, target["selector"]))
    reads |= {
        "description by element_id": await _answer(
            ids.evaluate(page, CLICK_TARGET_SCRIPT, {"elementId": element_id, "limit": _LABEL_CUT}, **bounds)
        ),
        "description by point": await _answer(
            ids.evaluate(page, CLICK_TARGET_SCRIPT, {"x": x, "y": y, "limit": _LABEL_CUT}, **bounds)
        ),
        "same element at two points": await _answer(same_element_at(ids, page, (x, y), (x + 2, y + 1))),
    }
    if by_selector:
        reads |= {
            "enabled by selector": await _answer(locator.evaluate(ELEMENT_ENABLED_SCRIPT)),
            "pointer check": await _answer(locator.evaluate(_RECEIVES_POINTER_SCRIPT, [x, y, None, False, False])),
            "pointer check of a placed click": await _answer(
                locator.evaluate(_RECEIVES_POINTER_SCRIPT, [x, y, None, False, True])
            ),
            "press's place": await _answer(locator.evaluate(_PRESS_PLACE_SCRIPT, [box, None])),
        }
    reads |= {
        "ready by element_id": await _answer(
            element.wait_until_actionable(verb="clicked", enabled=True, pointer=True, timeout_ms=CLICK_BUDGET_MS)
        ),
        "cover by element_id": await _answer(element.cover_at(x, y)),
    }

    async def press_read() -> str:
        with reading_presses("click", "read", None):
            try:
                await refuse_press_not_asked_about(ids, page, x, y, session_id=session_id)
            except PressNeedsApproval as stopped:
                return f"stopped ({stopped.kind})"
        return "pressed"

    reads["press read"] = await _answer(press_read())
    if by_selector:
        reads["pointer watch"] = await _answer(_pointer_watch(page, locator, x, y))
    return reads


async def _pointer_watch(page: Any, locator: Any, x: float, y: float) -> Any:
    """Where the target's document saw the pointer arrive (_POINTER_WATCH_SCRIPT), and the end of its watch."""
    target = await locator.element_handle()
    watch = await target.evaluate_handle(_POINTER_WATCH_SCRIPT)
    try:
        await page.mouse.move(x + 1, y + 1)
        return await target.evaluate(_POINTER_SEEN_SCRIPT, watch)
    finally:
        await watch.evaluate(_POINTER_WATCH_STOP_SCRIPT)
        await watch.dispose()
        await target.dispose()


def _without_the_added(read: Any, added: Collection[str]) -> Any:
    """An approval's read of fields (NAMED_FIELDS_SCRIPT) without the selects the test added: each is idle (hidden),
    with its name and no other attribute."""
    if not isinstance(read, dict):
        return read
    added_select = {"type": "", "id": "", "placeholder": "", "aria-label": "", "autocomplete": ""}
    idle = [field for field in read["idle"] if not (field["name"] in added and field | added_select == field)]
    return read | {"idle": idle}


async def _type_reads(
    env: Any, page: Any, element_id: str, field_selector: str, added: Collection[str]
) -> dict[str, Any]:
    """Every read a type into #target is gated by, past those a click's press is (_click_reads), each as the
    controller makes it, with focus in the field the text goes to."""
    ids = env.manager.element_ids
    locator = page.locator("#target").first
    element = ids.element(page, element_id)
    field = page.locator(field_selector).first
    await page.evaluate(FOCUSES, field_selector)
    by_selector = BrowserActionDecision(action="type", reason="r", selector="#target", text=TEXT)
    by_element_id = BrowserActionDecision(action="type", reason="r", element_id=element_id, text=TEXT)
    named_fields = env.manager.actions._named_fields
    reads: dict[str, Any] = {
        "fields by selector": _without_the_added(await _answer(named_fields(page, by_selector)), added),
        "fields by element_id": _without_the_added(await _answer(named_fields(page, by_element_id)), added),
        "focus place by selector": await _answer(locator.evaluate(ELEMENT_FOCUS_PLACE_SCRIPT)),
        "focus by selector": await _answer(locator.evaluate(ELEMENT_HAS_FOCUS_SCRIPT)),
        "focus by element_id": await _answer(element.has_focus()),
        "hold by selector": await _answer(_hold_by_selector(locator)),
        "hold by element_id": await _answer(_hold_by_element_id(element)),
        "field attributes": await _answer(field.evaluate(FIELD_ATTRIBUTES_SCRIPT)),
        "ready to type by element_id": await _answer(
            element.wait_until_actionable(verb="typed", enabled=True, pointer=True, timeout_ms=READ_TIMEOUT_MS)
        ),
    }
    return reads


async def _hold_by_selector(locator: Any) -> list[Any]:
    """The hold's take, read and release by selector, on the handle of the element found (_TypedField)."""
    found = await locator.element_handle()
    hold = await found.evaluate_handle(FIELD_HOLD_TAKE_SCRIPT)
    try:
        return [await hold.evaluate(FIELD_HOLD_READ_SCRIPT), await hold.evaluate(FIELD_HOLD_RELEASE_SCRIPT)]
    finally:
        await hold.dispose()
        await found.dispose()


async def _hold_by_element_id(element: Any) -> list[Any]:
    """The hold's take (with its first read), read and release by element_id, under one nonce (_TypedField)."""
    nonce = "named-controls"
    within = {"within_ms": FIELD_RECHECK_TIMEOUT_MS}
    return [
        await element.hold_field(nonce),
        await element.hold_field(nonce, **within),
        await element.release_field(nonce, **within),
    ]


async def _ended(env: Any, session_id: str, decision: BrowserActionDecision) -> str:
    """How execute_decision ended: held for approval (its kind, and for a type how its field read), or not."""
    try:
        await env.manager.execute_decision(session_id, decision)
    except ApprovalRequiredError as asked:
        approval = asked.payload["approval"]
        read = f", its field {approval['field_read']}" if decision.action == "type" else ""
        ended = f"held for approval ({approval['kind']}{read})"
    except BrowserActionError as refused:
        ended = f"refused ({refused.code})"
    except Exception as exc:  # noqa: BLE001 - every failure is an outcome to compare
        ended = f"raised {_first_line(exc)}"
    else:
        ended = "went ahead"
    return ended


async def _outcome(env: Any, session_id: str, decision: BrowserActionDecision) -> str:
    """How execute_decision ended (_ended), and how many presses reached the page."""
    ended = await _ended(env, session_id, decision)
    page = env.manager.sessions[session_id].page
    return f"{ended}; presses {await page.evaluate('() => window.presses')}"


async def _what_happens(
    tmp_path: Path, action: str, placement: str, control: str | None, *, by_selector: bool = True
) -> dict[str, Any]:
    markup = (CLICK_PAGES if action == "click" else TYPE_PAGES)[placement]
    async with browser_env(tmp_path, {"/page": COUNTS_PRESSES + markup + NAMED_ELEMENTS}) as env:
        session_id, page, targets, added = await _session(env, placement, control)
        reads = {"observe": await _observed(env, session_id, page, added)}
        for target in targets:
            of = f"{target['selector']}: " if len(targets) > 1 else ""
            element_id = target["element_id"]
            answers = await _click_reads(env, session_id, page, target, by_selector=by_selector)
            if action == "type":
                answers |= await _type_reads(env, page, element_id, FIELD.get(placement, "#target"), added)
            ways: dict[str, dict[str, Any]] = {"selector": {"selector": target["selector"]}} if by_selector else {}
            ways["element_id"] = {"element_id": element_id}
            if action == "click":
                box = target["box"]
                ways["point"] = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}
                env.manager.settings.action_timeout_ms = CLICK_BUDGET_MS
                for way, named in ways.items():
                    click = BrowserActionDecision(action="click", reason="look", risk_category="read", **named)
                    answers[f"click labelled read by {way}"] = await _outcome(env, session_id, click)
            else:
                for way, named in ways.items():
                    post = BrowserActionDecision(action="type", reason="r", text=TEXT, risk_category="post", **named)
                    answers[f"type labelled post by {way}"] = await _outcome(env, session_id, post)
            reads |= {of + read: answer for read, answer in answers.items()}
        return reads


def _differences(control: dict[str, Any], hostile: dict[str, Any]) -> str:
    return "\n".join(
        f"{read}:\n  control: {control[read]}\n  hostile: {hostile[read]}"
        for read in control
        if control[read] != hostile[read]
    )


@pytest.mark.parametrize(
    ("action", "placement"),
    [*(("click", placement) for placement in CLICK_PAGES), *(("type", placement) for placement in TYPE_PAGES)],
)
async def test_each_read_answers_on_a_page_whose_form_and_document_name_every_property_as_without_them(
    tmp_path: Path, action: str, placement: str
) -> None:
    control = await _what_happens(tmp_path, action, placement, None)
    hostile = {kind: await _what_happens(tmp_path, action, placement, made) for kind, made in CONTROLS.items()}
    if action == "click":
        hostile[TWO_OF_EACH_NAME_KIND] = await _what_happens(
            tmp_path, action, placement, TWO_OF_EACH_NAME, by_selector=False
        )

    # Each hostile page against the control's same reads (the third kind's are by element_id and by a point only).
    differing = {
        kind: _differences({read: control[read] for read in answer}, answer)
        for kind, answer in hostile.items()
        if answer != {read: control[read] for read in answer}
    }
    assert not differing, "\n".join(f"{kind}:\n{text}" for kind, text in differing.items())


async def _observed_with(
    env: Any, session_id: str, form_names: list[str], document_names: list[str], control: str
) -> Any:
    """What observe answers on the session's page loaded again and given these names, each as ``control``."""
    page = env.manager.sessions[session_id].page
    await page.reload()
    await page.evaluate(ADDS_THE_NAMES, [form_names, document_names, control])
    return await _observed(env, session_id, page, form_names)


@pytest.mark.parametrize("observed", list(OBSERVED_PAGES))
async def test_observe_answers_on_a_page_whose_form_names_every_property_or_one_that_ends_its_work_as_without_them(
    tmp_path: Path, observed: str
) -> None:
    markup = OBSERVED_PAGES[observed]
    async with browser_env(tmp_path, {"/page": COUNTS_PRESSES + markup + NAMED_ELEMENTS}) as env:
        session_id = await env.create("/page")
        names = await env.manager.sessions[session_id].page.evaluate(PROPERTY_NAMES)
        control = await _observed_with(env, session_id, [], [], "")
        rest = [name for name in names["form"] if name not in ENDS_THE_WORK_EARLY]
        given = {
            "every name": (names["form"], names["document"]),
            **{name: ([name], []) for name in ENDS_THE_WORK_EARLY},
            "the rest": (rest, names["document"]),
        }
        hostile = {
            f"{group}, as {kind}": await _observed_with(env, session_id, *named, made)
            for group, named in given.items()
            for kind, made in CONTROLS.items()
        }

    if markup.startswith('<form id="target"'):
        assert ["FORM", "target"] in [item["names"] for item in control["listed"]], control
    if observed == FOCUSED_FORM:
        assert control["focused"]["element_id"] == "listed 0", control
    if observed == LABELLED_BY_THE_FORM:
        (field,) = control["forms"][0]["fields"]
        assert control["listed"][0]["label"] == control["focused"]["label"] == field["label"] == "Delete account"
    differing = {group: answer for group, answer in hostile.items() if answer != control}
    assert not differing, "\n".join(
        f"{group}:\n  control: {control}\n  hostile: {answer}" for group, answer in differing.items()
    )


async def _observed_and_typed(tmp_path: Path, control: str) -> dict[str, Any]:
    """What observe answers on the plain form, and a type labelled write into its field by selector and by the
    element_id observe gave, each into the field emptied first, with what the field then holds."""
    async with browser_env(tmp_path, {"/page": PLAIN_FORM.format(control=control)}) as env:
        session_id = await env.create("/page")
        page = env.manager.sessions[session_id].page
        seen = await _answer(env.manager.observe(session_id, preset="text"))
        answers = {"observe": await _what_observe_answers(env, page, seen, ())}
        fields = (
            [item["element_id"] for item in seen["interactables"] if item["label"] == "Note"]
            if isinstance(seen, dict)
            else []
        )
        ways = {"selector": {"selector": "#f"}, "element_id": {"element_id": fields[0]} if fields else None}
        for way, named in ways.items():
            if named is None:
                answers[f"type labelled write by {way}"] = "not made: observe gave no element_id"
                continue
            await page.evaluate(EMPTIES_THE_FIELD)
            write = BrowserActionDecision(action="type", reason="r", text=WRITTEN, risk_category="write", **named)
            ended = await _ended(env, session_id, write)
            answers[f"type labelled write by {way}"] = f"{ended}; the field holds {await page.evaluate(FIELD_HOLDS)!r}"
        return answers


@pytest.mark.parametrize("name", ["getAttribute", "querySelectorAll"])
async def test_a_form_with_a_control_named_getattribute_or_queryselectorall_is_observed_and_typed_into_as_without_it(
    tmp_path: Path, name: str
) -> None:
    control = await _observed_and_typed(tmp_path, "")
    named = await _observed_and_typed(tmp_path, f'<input type="hidden" name="{name}">')

    for way in ("selector", "element_id"):
        assert control[f"type labelled write by {way}"] == f"went ahead; the field holds {WRITTEN!r}", control
    # The same, and in the form's fields the control after the plain field.
    hidden = {"tag": "input", "type": "hidden", "name": name, "label": name, "disabled": False}
    (form,) = control["observe"]["forms"]
    expected = control | {"observe": control["observe"] | {"forms": [form | {"fields": [*form["fields"], hidden]}]}}
    assert named == expected, _differences(expected, named)


# A filter chip drawn as "Remove", a form that is the button by its role; and a control named labels, a select whose
# option goes on from the chip's words harmlessly (CONTROLS).
CHIP = '<form id="chip" role="button" tabindex="0" style="width: 240px; height: 60px">Remove{control}</form>'
NAMED_LABELS = CONTROLS["selects"].format(name="labels")


async def _chip_clicked(tmp_path: Path, control: str) -> dict[str, Any]:
    """The chip's names as a click by selector reads them, and how a click labelled read on it by selector ends."""
    async with browser_env(tmp_path, {"/page": COUNTS_PRESSES + CHIP.format(control=control)}) as env:
        session_id = await env.create("/page")
        page = env.manager.sessions[session_id].page
        named = await _answer(_read_selected(page, "#chip"))
        click = BrowserActionDecision(action="click", reason="look", risk_category="read", selector="#chip")
        return {"names": named["labels"], "click labelled read by selector": await _outcome(env, session_id, click)}


async def test_a_form_drawn_as_a_button_is_named_by_its_own_words_whatever_a_control_named_labels_holds(
    tmp_path: Path,
) -> None:
    control = await _chip_clicked(tmp_path, "")
    named = await _chip_clicked(tmp_path, NAMED_LABELS)

    assert control["click labelled read by selector"] == "held for approval (destructive); presses 0", control
    assert named == control, _differences(control, named)


# A form a type names, focusable by script alone, around a password field that fills it, so that a press at the form's
# middle lands on the field; a control, where there is one, first in it.
FORM_AROUND_A_FIELD = (
    '<form id="t" tabindex="-1" style="width: 300px">{control}'
    '<input id="f" type="password" aria-label="Pin" style="display: block; box-sizing: border-box; width: 300px">'
    "</form>"
)
# Where a type's text went: each field's value, by its id, and the id of the element with focus, and where there is an
# editable element, the text each shows, through the prototypes.
WHERE_THE_TEXT_IS = """() => {
  const name = (el) => (el ? Element.prototype.getAttribute.call(el, 'id') : null);
  const value = (el) => Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').get.call(el);
  const shown = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'innerText').get;
  const fields = Document.prototype.querySelectorAll.call(document, 'textarea, input:not([type=hidden])');
  const editors = Document.prototype.querySelectorAll.call(document, '[contenteditable]');
  const focused = Object.getOwnPropertyDescriptor(Document.prototype, 'activeElement').get.call(document);
  const where = { fields: Array.from(fields, (el) => [name(el), value(el)]), focused: name(focused) };
  return editors.length ? { ...where, editors: Array.from(editors, (el) => [name(el), shown.call(el)]) } : where;
}"""


async def _typed(env: Any, session_id: str, decision: BrowserActionDecision) -> str:
    """How a type that is let go ahead ended, what its receipt shows of the text, and where the text went."""
    page = env.manager.sessions[session_id].page
    try:
        typed = (await env.manager.execute_decision(session_id, decision))["target"]
    except Exception as exc:  # noqa: BLE001 - every failure is an outcome to compare
        ended = f"raised {_first_line(exc)}"
    else:
        shows = typed.get("text_preview") == decision.text and "text_redacted" not in typed
        ended = f"went ahead, the receipt {'shows' if shows else 'withholds'} the text"
    return f"{ended}; {await page.evaluate(WHERE_THE_TEXT_IS)}"


async def _typed_into_the_form(tmp_path: Path, control: str) -> str:
    async with browser_env(tmp_path, {"/page": FORM_AROUND_A_FIELD.format(control=control)}) as env:
        session_id = await env.create("/page")
        write = BrowserActionDecision(action="type", reason="r", text=TEXT, risk_category="write", selector="#t")
        return await _typed(env, session_id, write)


async def test_a_type_into_a_form_around_a_field_reaches_the_field_whatever_a_control_named_hasattribute(
    tmp_path: Path,
) -> None:
    control = await _typed_into_the_form(tmp_path, "")
    named = await _typed_into_the_form(tmp_path, '<input type="hidden" name="hasAttribute">')

    assert control == f"went ahead, the receipt withholds the text; {{'fields': [['f', {TEXT!r}]], 'focused': 'f'}}"
    assert named == control


# Left out of the page a type by selector goes ahead on: the names at which, each alone where the form is what the
# selector names or holds it, Playwright's own steps end before any of the controller's: nodeName (READ_BY_THE_LOCATOR),
# checkVisibility and getBoundingClientRect (it finds no element), closest and matches (its scroll into view fails), and
# parentNode (its walk up from the element runs on until the type's time is up). Each ends the type refused.
STOPS_PLAYWRIGHT = ("nodeName", "checkVisibility", "getBoundingClientRect", "closest", "matches", "parentNode")
# The names at which, each alone, one of Playwright's own checks of the element (is_enabled, a hover, its focus)
# answered otherwise than without it, the type's steps before them made: each is on a page of its own, where another
# name could end the type first. Hidden inputs and selects alike there.
GOES_ANOTHER_WAY = (
    "getAttribute",
    "hasAttribute",
    "parentElement",
    "dispatchEvent",
    "focus",
    "nodeType",
    "getRootNode",
)


async def _type_ahead(
    env: Any, session_id: str, form_names: list[str], document_names: list[str], control: str, way: str
) -> dict[str, Any]:
    """A type labelled write into #target by ``way``, on the session's page loaded again and given these names, each as
    ``control``: how it ended and where its text went (_typed), and how many keys reached the page."""
    page = env.manager.sessions[session_id].page
    await page.reload()
    named: dict[str, Any] = {"selector": "#target"}
    if way == "element_id":
        listed = [
            item["element_id"] for item in (await env.manager.observe(session_id, preset="text"))["interactables"]
        ]
        names = [await env.manager.element_ids.element(page, one).evaluate(RESOLVES_TO, effect=READ) for one in listed]
        (target,) = [one for one, (_tag, id_) in zip(listed, names, strict=True) if id_ == "target"]
        named = {"element_id": target}
    await page.evaluate(ADDS_THE_NAMES, [form_names, document_names, control])
    await page.evaluate(EMPTIES_THE_FIELDS)
    write = BrowserActionDecision(action="type", reason="r", text=TEXT, risk_category="write", **named)
    typed = await _typed(env, session_id, write)
    return {"typed": typed, "keys": await page.evaluate("() => window.keys")}


@pytest.mark.parametrize("placement", list(TYPE_PAGES))
async def test_a_type_that_goes_ahead_puts_its_text_where_it_goes_without_the_names_or_sends_no_key(
    tmp_path: Path, placement: str
) -> None:
    async with browser_env(tmp_path, {"/page": COUNTS_PRESSES + TYPE_PAGES[placement] + NAMED_ELEMENTS}) as env:
        session_id = await env.create("/page")
        names = await env.manager.sessions[session_id].page.evaluate(PROPERTY_NAMES)
        control = {way: await _type_ahead(env, session_id, [], [], "", way) for way in ("selector", "element_id")}
        rest = [name for name in names["form"] if name not in (*STOPS_PLAYWRIGHT, *GOES_ANOTHER_WAY)]
        given = {
            **{(f"every name, as {kind}", "element_id"): (names["form"], made) for kind, made in CONTROLS.items()},
            **{(f"{name} alone", "selector"): ([name], CONTROLS["hidden inputs"]) for name in GOES_ANOTHER_WAY},
            **{(f"the rest, as {kind}", "selector"): (rest, made) for kind, made in CONTROLS.items()},
        }
        typed = {
            (group, way): await _type_ahead(env, session_id, form_names, names["document"], made, way)
            for (group, way), (form_names, made) in given.items()
        }

    for way in control:
        assert control[way]["typed"].startswith("went ahead"), control
    # As without the names; or refused, with no key sent and the text in no field, which is no type that went on
    # elsewhere: one that did ended as having gone ahead, or sent keys.
    differing = {
        group: answer
        for group, answer in typed.items()
        if answer["typed"] != control[group[1]]["typed"]
        and not (answer["typed"].startswith("raised ") and answer["keys"] == 0 and TEXT not in answer["typed"])
    }
    assert not differing, "\n".join(
        f"{group}:\n  control: {control[group[1]]}\n  hostile: {answer}" for group, answer in differing.items()
    )


# A button named by a form's text (aria-labelledby); and in the form a control named after a property that text is
# read by (CONTROLS).
LABELLED_BY_A_FORM = '<button id="b" aria-labelledby="f">x</button><form id="f"{style}>{control}{text}</form>'
# The form shown, the form hidden (its text is its text content), and a form shown whose text is hidden (it shows none).
LABELLING_FORMS = {
    "a shown form, a hidden input named innerText": ("", "Delete account", "hidden inputs", "innerText"),
    "a shown form, a select named innerText": ("", "Delete account", "selects", "innerText"),
    "a hidden form, a hidden input named innerText": (
        ' style="display: none"',
        "Delete account",
        "hidden inputs",
        "innerText",
    ),
    "a hidden form, a hidden input named textContent": (
        ' style="display: none"',
        "Delete account",
        "hidden inputs",
        "textContent",
    ),
    "a form whose text is hidden, a hidden input named textContent": (
        "",
        "<span hidden>Delete account</span>",
        "hidden inputs",
        "textContent",
    ),
}


async def _button_label(tmp_path: Path, style: str, text: str, control: str) -> Any:
    """The label observe gives the button."""
    page = LABELLED_BY_A_FORM.format(style=style, control=control, text=text)
    async with browser_env(tmp_path, {"/page": page}) as env:
        session_id = await env.create("/page")
        items = (await env.manager.observe(session_id, preset="text"))["interactables"]
        return [item["label"] for item in items if item["tag"] == "button"]


@pytest.mark.parametrize("case", list(LABELLING_FORMS))
async def test_a_button_named_by_a_form_is_named_by_its_text_whatever_a_control_of_the_form_is_named(
    tmp_path: Path, case: str
) -> None:
    style, text, kind, name = LABELLING_FORMS[case]
    named = await _button_label(tmp_path, style, text, CONTROLS[kind].format(name=name))

    assert named == await _button_label(tmp_path, style, text, "") == ["Delete account"]


async def _enabled_and_clicked(tmp_path: Path, names: list[str], control: str) -> dict[str, Any]:
    """On the page of forms under something disabled, ``names`` given to each form as ``control``: each form's enabled
    read by element_id, as a click's wait asks it, and by selector, as actions._is_enabled asks it, in the page's own
    world; and how a click labelled read on it ends by each way, within CLICK_BUDGET_MS, with the presses counted."""
    async with browser_env(tmp_path, {"/page": COUNTS_PRESSES + UNDER_SOMETHING_DISABLED + NAMED_ELEMENTS}) as env:
        session_id = await env.create("/page")
        page = env.manager.sessions[session_id].page
        targets = await _targets(env, session_id, page, DISABLED_TARGETS)
        await page.evaluate(ADDS_THE_NAMES, [names, [], control])
        env.manager.settings.action_timeout_ms = CLICK_BUDGET_MS
        answers: dict[str, Any] = {}
        for target in targets:
            selector, element_id = target["selector"], target["element_id"]
            element = env.manager.element_ids.element(page, element_id)
            answers[f"{selector} enabled by element_id"] = await _answer(
                element.wait_until_actionable(verb="clicked", enabled=True, pointer=True, timeout_ms=CLICK_BUDGET_MS)
            )
            answers[f"{selector} enabled by selector"] = await _answer(
                page.locator(selector).first.evaluate(ELEMENT_ENABLED_SCRIPT)
            )
            for way, named in {"selector": {"selector": selector}, "element_id": {"element_id": element_id}}.items():
                click = BrowserActionDecision(action="click", reason="look", risk_category="read", **named)
                answers[f"{selector} click labelled read by {way}"] = await _outcome(env, session_id, click)
        return answers


async def test_a_form_under_something_disabled_is_not_enabled_whatever_a_control_named_iscontenteditable(
    tmp_path: Path,
) -> None:
    # The enabled read follows a form to the button-like element it is in, or to the control of the label it is in,
    # unless the form is editable, which it took off the form as a property: a control named isContentEditable answered
    # for it, the form was taken for editable and read as itself, enabled, and a click on it went on where it waits.
    control = await _enabled_and_clicked(tmp_path, [], "")
    named = {kind: await _enabled_and_clicked(tmp_path, ["isContentEditable"], made) for kind, made in CONTROLS.items()}

    for target in DISABLED_TARGETS:
        assert control[f"{target} enabled by element_id"] == "refused (element_disabled)", control
        assert control[f"{target} enabled by selector"] is False, control
        assert control[f"{target} click labelled read by selector"] == "refused (browser_action_failed); presses 0"
        assert control[f"{target} click labelled read by element_id"] == "refused (element_disabled); presses 0"
    differing = {kind: _differences(control, answer) for kind, answer in named.items() if answer != control}
    assert not differing, "\n".join(f"{kind}:\n{text}" for kind, text in differing.items())


# A form under a fixed header, the page scrolled (UNDER_THE_HEADER) so that the form is under it: a click by element_id
# finds the form covered, scrolls it to the end of the view, out from under the header, and presses it there.
UNDER_A_HEADER = (
    '<div style="position: fixed; top: 0; left: 0; width: 100%; height: 120px; z-index: 1; background: white"></div>'
    '<form id="target" data-form tabindex="0" style="width: 240px; height: 60px; margin-top: 400px">Open</form>'
    '<div style="height: 2000px"></div>'
)
UNDER_THE_HEADER = "() => window.scrollTo(0, 360)"


async def _clicked_from_under_a_header(tmp_path: Path, names: list[str], control: str) -> str:
    """How a click labelled read by element_id on the form under the header ends, ``names`` given to the form as
    ``control``, and the presses counted."""
    async with browser_env(tmp_path, {"/page": COUNTS_PRESSES + UNDER_A_HEADER + NAMED_ELEMENTS}) as env:
        session_id = await env.create("/page")
        page = env.manager.sessions[session_id].page
        (target,) = await _targets(env, session_id, page, ("#target",))
        await page.evaluate(ADDS_THE_NAMES, [names, [], control])
        await page.evaluate(UNDER_THE_HEADER)
        click = BrowserActionDecision(
            action="click", reason="look", risk_category="read", element_id=target["element_id"]
        )
        return await _outcome(env, session_id, click)


async def test_a_click_by_element_id_scrolls_a_covered_form_out_from_under_its_cover_whatever_its_controls_are_named(
    tmp_path: Path,
) -> None:
    # The wait for an element_id target to be ready scrolls a covered target with the next alignment. It took the
    # form's scrollIntoView as a property: a control so named answered for it, the script threw, and the click was
    # refused where it presses.
    control = await _clicked_from_under_a_header(tmp_path, [], "")
    named = {
        kind: await _clicked_from_under_a_header(tmp_path, ["scrollIntoView"], made) for kind, made in CONTROLS.items()
    }

    assert control == "went ahead; presses 1"
    assert named == dict.fromkeys(CONTROLS, control)


# A word in an editable block (editable by what it is in), an SVG image (which has no such property) and a word in a
# block that is not editable, each in a role=button that is aria-disabled.
FOLLOWED_OR_NOT = (
    '<div role="button" aria-disabled="true"><div contenteditable="true"><span id="in-an-editor">word</span></div></div>'
    '<div role="button" aria-disabled="true"><svg id="image" width="20" height="20"><rect width="20" height="20"/></svg>'
    '</div><div role="button" aria-disabled="true"><span id="word">word</span></div>'
)


async def test_the_enabled_read_follows_a_target_where_playwrights_own_check_does(tmp_path: Path) -> None:
    # Playwright's check reads isContentEditable, which an element has by what it is in, and an SVG element has not:
    # what is editable is checked as itself, and what is not goes to the button it is in.
    async with browser_env(tmp_path, {"/page": FOLLOWED_OR_NOT}) as env:
        session_id = await env.create("/page")
        page = env.manager.sessions[session_id].page
        targets = ("#in-an-editor", "#image", "#word")
        ours = {target: await page.locator(target).evaluate(ELEMENT_ENABLED_SCRIPT) for target in targets}
        theirs = {target: await page.locator(target).is_enabled() for target in targets}

    assert ours == theirs == {"#in-an-editor": True, "#image": False, "#word": False}

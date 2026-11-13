"""What a type into a secret field keeps of its text, in Chromium, through the real BrowserManager, pipeline and
witness store.

A type decides whether its field is secret from the field's attributes as it runs. Attributes it could not read were
taken to say "not secret", so a password field that came in after the read had timed out kept the typed text as the
action's preview: in the result, actions.jsonl, the audit log and the receipt; and a type into a field that never
came kept the text it was to type in its failure record. The field read was the one the type named, where the text
can go to another node (an Enter that moves focus on, a label or a component around the field). The hold a type
takes on that node waits a bounded time for the page, and leaves no listener on it. And what an
approval asked for a type shows of its text, by how its field read. Only the page server is local
(test_allowed_hosts_navigation's harness).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from app.action_errors import BrowserActionError
from app.approvals import ApprovalRequiredError
from app.browser.element_scripts import ELEMENT_FIELD_RELEASE_SCRIPT
from app.browser.element_world import READ
from app.models import BrowserActionDecision
from tests._chromium import requires_chromium
from tests.test_allowed_hosts_navigation import (  # noqa: F401 - _shared_chromium: that module's autouse browser
    _shared_chromium,
    audit_spy,
    browser_env,
)

pytestmark = [requires_chromium, pytest.mark.asyncio(loop_scope="module")]

TEXT = "hunter2-secret"
LOGIN = '<title>login</title><input id="user" name="user"><input id="pw" type="password" aria-label="Password">'
# The password field is put in when the test says so (addPw), so its timing is the test's.
LATE = (
    "<title>login</title><script>window.addPw = (ms) => setTimeout(() => document.body.insertAdjacentHTML("
    "'beforeend', '<input id=\"pw\" type=\"password\">'), ms);</script>"
)


def _type_lines(session: Any) -> list[dict[str, Any]]:
    path = Path(session.artifact_dir) / "actions.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [line for line in lines if line.get("action") == "type"]


async def _type_receipt(env: Any, session_id: str) -> Any:
    receipts = [r for r in await env.manager.witness.list(session_id) if r.event_type == "browser_action"]
    (receipt,) = [r for r in receipts if r.action == "type"]
    return receipt


def _type_events(spy: Any) -> list[dict[str, Any]]:
    return [call.kwargs for call in spy.call_args_list if call.kwargs.get("event_type") == "browser_action"]


async def _late_field_env(env: Any, appears_after_ms: int | None) -> tuple[str, Any, Any]:
    """A session on LATE whose password field comes in ``appears_after_ms`` into the type's operation (never with
    None), with ACTION_TIMEOUT_MS at 1 s; the audit log spied on."""
    # The context's default timeout is set from this when the session is made.
    env.manager.settings.action_timeout_ms = 1000
    env.manager.settings.witness_enabled = True
    session_id = await env.create("/late")
    spy = audit_spy(env)
    real_ready = env.manager._ensure_witness_remote_ready

    async def field_comes_later(the_session: Any, **kwargs: Any) -> None:
        # The pipeline's last await before the operation.
        await real_ready(the_session, **kwargs)
        if appears_after_ms is not None:
            await the_session.page.evaluate("(ms) => window.addPw(ms)", appears_after_ms)

    env.manager._ensure_witness_remote_ready = field_comes_later
    return session_id, env.manager.sessions[session_id], spy


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_type_into_a_password_field_not_marked_sensitive_restricts_its_receipt(tmp_path: Path, by: str) -> None:
    # The type finds the field secret as it runs and withholds its preview; the policy was asked before it ran, when
    # only the caller's mark could say so, and the receipt kept standard evidence with no sensitive_input concern.
    async with browser_env(tmp_path, {"/login": LOGIN}) as env:
        env.manager.settings.witness_enabled = True
        session_id = await env.create("/login")
        if by == "selector":
            target = {"selector": "#pw"}
        else:
            items = (await env.manager.observe(session_id, preset="text"))["interactables"]
            (field,) = [item for item in items if item.get("label") == "Password"]
            target = {"element_id": field["element_id"]}

        result = await env.manager.type(session_id, text=TEXT, **target)

        assert await env.manager.sessions[session_id].page.input_value("#pw") == TEXT
        assert result["target"].get("text_redacted") is True and "text_preview" not in result["target"]
        receipt = await _type_receipt(env, session_id)
        assert receipt.evidence_mode == "restricted"
        assert "sensitive_input" in [concern.code for concern in receipt.concerns]
        assert (receipt.evidence.before, receipt.evidence.after) == (None, None)


async def test_a_password_field_that_comes_in_late_keeps_no_preview(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/late": LATE}) as env:
        # 0.4 s in: within the scroll's 1 s wait for it. The attributes were read first, and one that came in after
        # that read had timed out kept the preview; they are now read once the field is focused, just before the text.
        session_id, session, spy = await _late_field_env(env, 400)

        result = await env.manager.type(session_id, selector="#pw", text=TEXT)

        assert await session.page.input_value("#pw") == TEXT
        assert await session.page.get_attribute("#pw", "type") == "password"
        (line,) = _type_lines(session)
        (event,) = _type_events(spy)
        receipt = await _type_receipt(env, session_id)
        for kept in (result["target"], line["target"], event["details"]["target"], receipt.target):
            assert "text_preview" not in kept and kept.get("text_redacted") is True, kept


async def test_a_type_into_a_field_that_never_comes_records_no_preview(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/late": LATE}) as env:
        session_id, session, spy = await _late_field_env(env, None)

        with pytest.raises(BrowserActionError):
            await env.manager.type(session_id, selector="#pw", text=TEXT)

        (line,) = _type_lines(session)
        (event,) = _type_events(spy)
        receipt = await _type_receipt(env, session_id)
        assert line["status"] == "failed"
        for kept in (line["target"], event["details"]["target"], receipt.target):
            assert "text_preview" not in kept and kept.get("text_redacted") is True, kept


# A login field that a page makes a password field once it is focused (an honest pattern), or once text is typed into
# it; and, the control, one that stays a plain field.
TURNS_SECRET = {
    "on-focus": '<title>login</title><input id="login" aria-label="Login" onfocus="this.type = `password`">',
    "as-it-is-typed-into": '<title>login</title><input id="login" aria-label="Login" oninput="this.type = `password`">',
    "never": '<title>login</title><input id="login" aria-label="Login">',
}


@pytest.mark.parametrize("when", list(TURNS_SECRET))
@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_field_that_turns_secret_after_it_was_first_read_keeps_no_preview(
    tmp_path: Path, by: str, when: str
) -> None:
    # The preview came back at the first read of the field's attributes, before the scroll, the focus click and the
    # clear, so the text typed into a field the page had made a password field kept its preview. The field is read
    # just before the first keystroke and again once the text is in.
    async with browser_env(tmp_path, {"/login": TURNS_SECRET[when]}) as env:
        env.manager.settings.witness_enabled = True
        session_id = await env.create("/login")
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)
        if by == "selector":
            target = {"selector": "#login"}
        else:
            items = (await env.manager.observe(session_id, preset="text"))["interactables"]
            (field,) = [item for item in items if item.get("label") == "Login"]
            target = {"element_id": field["element_id"]}

        result = await env.manager.type(session_id, text=TEXT, **target)

        assert await session.page.input_value("#login") == TEXT
        secret = await session.page.get_attribute("#login", "type") == "password"
        assert secret is (when != "never")
        (line,) = _type_lines(session)
        (event,) = _type_events(spy)
        receipt = await _type_receipt(env, session_id)
        for kept in (result["target"], line["target"], event["details"]["target"], receipt.target):
            if secret:
                assert "text_preview" not in kept and kept.get("text_redacted") is True, kept
            else:
                assert kept.get("text_preview") == TEXT and "text_redacted" not in kept, kept
        assert receipt.evidence_mode == ("restricted" if secret else "standard")


async def test_a_type_by_element_id_whose_field_left_the_page_records_no_preview(tmp_path: Path) -> None:
    # By element_id the field's attributes are read first, and a read that failed (the element gone) raised before
    # the preview was withheld: the failure record kept the text it was to type, under a standard receipt.
    async with browser_env(tmp_path, {"/login": LOGIN}) as env:
        env.manager.settings.witness_enabled = True
        session_id = await env.create("/login")
        session = env.manager.sessions[session_id]
        spy = audit_spy(env)
        items = (await env.manager.observe(session_id, preset="text"))["interactables"]
        (field,) = [item for item in items if item.get("label") == "Password"]
        await session.page.evaluate("() => document.getElementById('pw').remove()")

        with pytest.raises(BrowserActionError):
            await env.manager.type(session_id, element_id=field["element_id"], text=TEXT)

        (line,) = _type_lines(session)
        (event,) = _type_events(spy)
        receipt = await _type_receipt(env, session_id)
        assert line["status"] == "failed"
        for kept in (line["target"], event["details"]["target"], receipt.target):
            assert "text_preview" not in kept and kept.get("text_redacted") is True, kept
        assert receipt.evidence_mode == "restricted"


async def _typed_into(env: Any, path: str, by: str, label: str, selector: str, text: str) -> tuple[Any, list[Any]]:
    """Type ``text`` into the field ``selector`` names, or the element_id of the one observe labels ``label``; the
    session, and every record of the type's target (its answer, actions.jsonl line, audit entry and receipt) with the
    receipt last."""
    env.manager.settings.witness_enabled = True
    session_id = await env.create(path)
    session = env.manager.sessions[session_id]
    spy = audit_spy(env)
    if by == "selector":
        target = {"selector": selector}
    else:
        items = (await env.manager.observe(session_id, preset="text"))["interactables"]
        (field,) = [item for item in items if item.get("label") == label]
        target = {"element_id": field["element_id"]}
    result = await env.manager.type(session_id, text=text, **target)
    (line,) = _type_lines(session)
    (event,) = _type_events(spy)
    receipt = await _type_receipt(env, session_id)
    return session, [result["target"], line["target"], event["details"]["target"], receipt.target, receipt]


def _assert_withheld(records: list[Any]) -> None:
    *targets, receipt = records
    for kept in targets:
        assert "text_preview" not in kept and kept.get("text_redacted") is True, kept
    assert receipt.evidence_mode == "restricted"
    assert "sensitive_input" in [concern.code for concern in receipt.concerns]


# A user field whose Enter sends focus on to the password field, which takes the rest of the text (a login in two
# steps); one whose password field's own Enter sends focus back, so the user field has focus again once the text is
# in; and one whose page stops every focus event before it reaches a node, so nothing on the user field hears that
# focus left it.
TO_PASSWORD = "document.getElementById(`pw`).focus()"
TO_USER = "document.getElementById(`user`).focus()"
NO_FOCUS_EVENTS = (
    "<script>for (const type of [`blur`, `focus`, `focusin`, `focusout`]) "
    "window.addEventListener(type, (event) => event.stopImmediatePropagation(), true);</script>"
)
MOVES_FOCUS = {
    "and-stays": (
        f'<title>login</title><input id="user" aria-label="User" onkeydown="if (event.key === `Enter`) {TO_PASSWORD}">'
        '<input id="pw" type="password">'
    ),
    "and-comes-back": (
        f'<title>login</title><input id="user" aria-label="User" onkeydown="if (event.key === `Enter`) {TO_PASSWORD}">'
        f'<input id="pw" type="password" onkeydown="if (event.key === `Enter`) {TO_USER}">'
    ),
    "and-no-event-says-so": (
        f"<title>login</title>{NO_FOCUS_EVENTS}"
        f'<input id="user" aria-label="User" onkeydown="if (event.key === `Enter`) {TO_PASSWORD}">'
        '<input id="pw" type="password">'
    ),
}


@pytest.mark.parametrize("moves", list(MOVES_FOCUS))
@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_text_whose_enter_moves_focus_to_a_password_field_keeps_no_preview(
    tmp_path: Path, by: str, moves: str
) -> None:
    # Both reads were of the field the type named, and the password went into another: the named field read plain
    # before and after, so the preview kept "alice" and the password, under a standard receipt. The type holds the
    # node that has focus at the first keystroke, and its preview comes back only if that node kept focus throughout.
    text = f"alice\n{TEXT}" + ("\n" if moves == "and-comes-back" else "")
    async with browser_env(tmp_path, {"/login": MOVES_FOCUS[moves]}) as env:
        session, records = await _typed_into(env, "/login", by, "User", "#user", text)

        assert await session.page.input_value("#user") == "alice"
        assert await session.page.input_value("#pw") == TEXT
        focused = await session.page.evaluate("() => document.activeElement.id")
        assert focused == ("user" if moves == "and-comes-back" else "pw")
        _assert_withheld(records)
        if by == "element_id":
            assert await _holds_kept(env, session, records[0]["element_id"]) == 0


# A field the page makes a password field as the text goes in, and replaces with a plain input of the same id once
# the whole text is in.
REPLACED = (
    '<title>login</title><input id="login" aria-label="Login" oninput="this.type = `password`; '
    f"if (this.value.length === {len(TEXT)}) {{ const plain = document.createElement(`input`); plain.id = `login`; "
    'this.replaceWith(plain); }">'
)


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_field_turned_secret_then_replaced_by_a_plain_one_keeps_no_preview(tmp_path: Path, by: str) -> None:
    # By selector the read once the text was in found the selector's field anew: the plain replacement. By element_id
    # the id's element was gone by then, which already said secret.
    async with browser_env(tmp_path, {"/login": REPLACED}) as env:
        session, records = await _typed_into(env, "/login", by, "Login", "#login", TEXT)

        assert await session.page.get_attribute("#login", "type") is None
        assert await session.page.input_value("#login") == ""
        _assert_withheld(records)


# What a selector can name that takes focus for a password field and is not it: a component whose open shadow root
# holds the field, a label around it, and a frame whose document holds it. Each with how to read the field's value.
AROUND_THE_FIELD = {
    "component": (
        '<title>login</title><div id="named"></div><script>document.getElementById("named")'
        '.attachShadow({mode: "open", delegatesFocus: true}).innerHTML = "<input type=password>";</script>',
        "() => document.getElementById('named').shadowRoot.firstChild.value",
    ),
    "label": (
        '<title>login</title><label id="named">Secret <input type="password"></label>',
        "() => document.querySelector('input').value",
    ),
    "frame": (
        '<title>login</title><iframe id="named" srcdoc="<input type=password '
        "style='position: fixed; left: 0; top: 0; width: 100%; height: 100%'>\"></iframe>",
        "() => document.getElementById('named').contentDocument.querySelector('input').value",
    ),
}


@pytest.mark.parametrize("named", list(AROUND_THE_FIELD))
async def test_a_type_by_a_selector_that_names_what_is_around_a_password_field_keeps_no_preview(
    tmp_path: Path, named: str
) -> None:
    # The read was of what the selector names, whose own attributes say nothing, and the text went to the password
    # field. The node the type holds and reads is the innermost focused element in what it names: the field in the
    # shadow root, or in the label. In a frame that node is the frame's document's, which the type cannot hold from
    # the page: nothing read, so nothing shown.
    page, value = AROUND_THE_FIELD[named]
    async with browser_env(tmp_path, {"/login": page}) as env:
        session, records = await _typed_into(env, "/login", "selector", "", "#named", TEXT)

        assert await session.page.evaluate(value) == TEXT
        _assert_withheld(records)


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_text_with_an_enter_that_moves_nothing_shows_its_preview(tmp_path: Path, by: str) -> None:
    # The control: an Enter in the text withholds nothing by itself. The textarea keeps focus and reads not secret.
    page = '<title>note</title><textarea id="note" aria-label="Note"></textarea>'
    async with browser_env(tmp_path, {"/note": page}) as env:
        session, records = await _typed_into(env, "/note", by, "Note", "#note", "two\nlines")

        assert await session.page.input_value("#note") == "two\nlines"
        *targets, receipt = records
        for kept in targets:
            assert kept.get("text_preview") == "two\nlines" and "text_redacted" not in kept, kept
        assert receipt.evidence_mode == "standard"
        if by == "element_id":
            assert await _holds_kept(env, session, targets[0]["element_id"]) == 0


async def _holds_kept(env: Any, session: Any, element_id: str) -> int:
    """How many holds on a typed field the element ids' world still keeps: a type gives its own back."""
    element = env.manager.element_ids.element(session.page, element_id)
    return await element.evaluate("(el, arg, registry) => registry.fieldHolds.size", effect=READ)


# A page that counts the capture listeners for focus events its own world has on its nodes (window.listening), as a
# hold by selector puts them there, and can be told what else to do when one is put on (window.onHold).
COUNTS_LISTENERS = (
    "<script>window.listening = 0; window.onHold = () => {};"
    "for (const [name, by] of [[`addEventListener`, 1], [`removeEventListener`, -1]]) {"
    " const real = EventTarget.prototype[name];"
    " EventTarget.prototype[name] = function (type, listener, options) {"
    "  const counted = options === true && [`blur`, `focus`, `focusin`].includes(type);"
    "  if (counted) { window.listening += by; if (by > 0) window.onHold(); }"
    "  return real.call(this, type, listener, options); }; }</script>"
)
# How long the page below keeps its thread once a hold's first listener is put on: past FIELD_RECHECK_TIMEOUT_MS.
BUSY_MS = 2000
BUSY_AS_THE_HOLD_IS_MADE = (
    f"<title>note</title>{COUNTS_LISTENERS}<script>window.onHold = () => {{ window.onHold = () => {{}}; "
    f"const until = performance.now() + {BUSY_MS}; while (performance.now() < until); }};</script>"
    '<textarea id="note" aria-label="Note"></textarea>'
)
# A page whose next read of an attribute, once text has gone into the field, throws: the hold's read as it is released.
A_READ_THROWS_ONCE_TYPED = (
    f'<title>note</title>{COUNTS_LISTENERS}<textarea id="note" aria-label="Note"></textarea><script>'
    "document.getElementById(`note`).addEventListener(`input`, () => { const real = Element.prototype.getAttribute;"
    " Element.prototype.getAttribute = function () { Element.prototype.getAttribute = real; throw new Error(`no`); };"
    "}, { once: true });</script>"
)


async def _listening(session: Any) -> int:
    return await session.page.evaluate("() => window.listening")


async def test_a_take_by_selector_the_page_holds_up_past_its_bound_keeps_no_preview(tmp_path: Path) -> None:
    # Locator.evaluate_handle(timeout=) bounds the wait for the element, not the evaluation on it: the type waited
    # for the page however long it took, where its docstring said a second. The take is given up at its bound, the
    # text is typed, and the hold, which the page made all the same, is released once its handle arrives.
    async with browser_env(tmp_path, {"/note": BUSY_AS_THE_HOLD_IS_MADE}) as env:
        session, records = await _typed_into(env, "/note", "selector", "Note", "#note", "ordinary note text")

        assert await session.page.input_value("#note") == "ordinary note text"
        _assert_withheld(records)
        for _ in range(100):
            if await _listening(session) == 0:
                break
            await session.page.wait_for_timeout(50)
        assert await _listening(session) == 0


async def test_a_release_by_selector_whose_read_throws_still_takes_its_listeners_off(tmp_path: Path) -> None:
    # The release script read before it released, and a read that threw skipped the release: the node kept the
    # hold's listeners until its document went. It can throw in the page's own world, where the page may have
    # replaced what it calls.
    async with browser_env(tmp_path, {"/note": A_READ_THROWS_ONCE_TYPED}) as env:
        session, records = await _typed_into(env, "/note", "selector", "Note", "#note", "ordinary note text")

        assert await session.page.input_value("#note") == "ordinary note text"
        _assert_withheld(records)
        assert await _listening(session) == 0


async def test_a_release_by_element_id_whose_read_throws_still_gives_the_hold_back(tmp_path: Path) -> None:
    # The world's script took the hold out of the registry, read, and then released: a read that threw left the
    # listeners on the node with nothing to find them by. No page can make a read throw in the element ids' world,
    # so the script is run as it is on a hold whose node is no node of a document.
    async with browser_env(tmp_path, {"/blank": "<title>blank</title>"}) as env:
        session_id = await env.create("/blank")

        released = await env.manager.sessions[session_id].page.evaluate(
            f"""() => {{
              const removed = [];
              const node = {{ removeEventListener: (type) => removed.push(type) }};
              const registry = {{ fieldHolds: new Map([['nonce', {{ node, left: false, listener: () => {{}} }}]]) }};
              let threw = false;
              try {{
                ({ELEMENT_FIELD_RELEASE_SCRIPT})(null, 'nonce', registry);
              }} catch (error) {{
                threw = true;
              }}
              return {{ threw, kept: registry.fieldHolds.size, removed }};
            }}"""
        )

        assert released == {"threw": True, "kept": 0, "removed": ["blur", "focus"]}


# A note field the page puts in when the test says so (addNote), so its timing is the test's.
LATE_NOTE = (
    "<title>post</title><script>window.addNote = () => document.body.insertAdjacentHTML("
    "'beforeend', '<textarea id=\"note\" aria-label=\"Note\"></textarea>');</script>"
)


async def _asked_for(env: Any, session_id: str, decision: BrowserActionDecision) -> dict[str, Any]:
    with pytest.raises(ApprovalRequiredError) as asked:
        await env.manager.execute_decision(session_id, decision)
    return asked.value.payload["approval"]


async def test_an_approval_asked_before_its_field_is_on_the_page_shows_its_text_once_asked_again(
    tmp_path: Path,
) -> None:
    # The approval's read of the field waited ACTION_TIMEOUT_MS (15 s by default) for one not on the page, and the
    # one flag it then set hid the text for good: a post's approver stayed blind though the field came and read
    # plain. The read is given up on at FIELD_RECHECK_TIMEOUT_MS and leaves the field unread; asked for again once
    # the field is there, the same approval shows its text.
    async with browser_env(tmp_path, {"/post": LATE_NOTE}) as env:
        session_id = await env.create("/post")
        session = env.manager.sessions[session_id]
        post = BrowserActionDecision(
            action="type", reason="post it", selector="#note", text="hello all", risk_category="post"
        )

        began = time.monotonic()
        unread = await _asked_for(env, session_id, post)
        took = time.monotonic() - began
        await session.page.evaluate("() => window.addNote()")
        plain = await _asked_for(env, session_id, post)

        assert took < 5, took
        assert unread["id"] == plain["id"]
        assert (unread["action"]["text"], unread["field_read"]) == (
            "text hidden: its field has not been read",
            "unread",
        )
        assert (plain["action"]["text"], plain["field_read"]) == ("hello all", "plain")


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_an_approval_asked_for_a_type_into_a_password_field_hides_its_text(tmp_path: Path, by: str) -> None:
    async with browser_env(tmp_path, {"/login": LOGIN}) as env:
        session_id = await env.create("/login")
        named = {"selector": "#pw"}
        if by == "element_id":
            items = (await env.manager.observe(session_id, preset="text"))["interactables"]
            (field,) = [item for item in items if item.get("label") == "Password"]
            named = {"element_id": field["element_id"]}
        change = BrowserActionDecision(
            action="type", reason="change it", text=TEXT, risk_category="account_change", **named
        )

        approval = await _asked_for(env, session_id, change)

        assert (approval["field_read"], approval["action"]["text"]) == (
            "secret",
            "text hidden: its field reads as secret",
        )
        assert TEXT not in json.dumps(await env.manager.list_approvals())


# What stands for a type's text in its approval where its field did not read plain.
HIDDEN = {"secret": "text hidden: its field reads as secret", "unread": "text hidden: its field has not been read"}
PASSWORD_IN = "attachShadow({mode: `open`}).innerHTML = `<input type=password>`"
# What the selector of an approval's type can name, and how the approval then reads: by every field the text could
# go to from there (the element itself, a label's control, the fields inside it through open shadow roots), all of
# them plain for the text to show. A frame's document and a closed shadow root are not read.
NAMED_BY_AN_APPROVAL = {
    "a component whose open shadow root holds a password field": (AROUND_THE_FIELD["component"][0], "secret"),
    "a label around a password field": (AROUND_THE_FIELD["label"][0], "secret"),
    "a frame whose document holds a password field": (AROUND_THE_FIELD["frame"][0], "unread"),
    "a label around a plain field": ('<label id="named">Note <textarea></textarea></label>', "plain"),
    "a form with a user field and a password field": (
        '<form id="named"><input name="user"><input type="password"></form>',
        "secret",
    ),
    "a label for a password field elsewhere": (
        '<label id="named" for="pw">Secret</label><input id="pw" type="password">',
        "secret",
    ),
    "a component with a password field two open shadow roots down": (
        '<div id="named"></div><script>const outer = document.getElementById(`named`).attachShadow({mode: `open`});'
        f"outer.innerHTML = `<span></span>`; outer.firstChild.{PASSWORD_IN};</script>",
        "secret",
    ),
    "a form whose hidden input is named as a secret": (
        '<form id="named"><input type="hidden" name="csrf_token"><textarea name="note"></textarea></form>',
        "plain",
    ),
    "an element that holds no field": ('<div id="named" tabindex="0" aria-label="Note">note</div>', "unread"),
    "a plain field beside a frame": (
        '<div id="named"><textarea></textarea><iframe srcdoc="<input>"></iframe></div>',
        "unread",
    ),
    "a password field beside a frame": (
        '<div id="named"><input type="password"><iframe srcdoc="<input>"></iframe></div>',
        "secret",
    ),
    "a component whose shadow root is closed": (
        '<div id="named"></div><script>document.getElementById(`named`)'
        ".attachShadow({mode: `closed`}).innerHTML = `<input type=password>`;</script>",
        "unread",
    ),
    "an element the page made editable": ('<div id="named" contenteditable aria-label="Note"></div>', "plain"),
    "a paragraph of an editable element": ('<div contenteditable><p id="named">note</p></div>', "plain"),
    "an element around an editable one": ('<section id="named"><div contenteditable></div></section>', "plain"),
    "an element around one the page made not editable": (
        '<section id="named"><span contenteditable="false">fixed</span></section>',
        "unread",
    ),
    "a label around a select": ('<label id="named">Pick <select><option>a</option></select></label>', "plain"),
}


async def test_an_approval_reads_every_field_its_types_text_could_go_to_from_what_is_named(tmp_path: Path) -> None:
    # DeepSeek-V4.1-Flash-1017's read of FX9c4 (2): the approval read the attributes of what the selector named, and
    # the wrappers a type's own receipt withholds its text for (AROUND_THE_FIELD) say nothing of the password field
    # inside, so the approval said plain and showed the password, as its listing, webhook and SSE summary did.
    cases = list(NAMED_BY_AN_APPROVAL.items())
    pages = {f"/{index}": f"<title>named</title>{page}" for index, (_name, (page, _read)) in enumerate(cases)}
    async with browser_env(tmp_path, pages) as env:
        session_id = await env.create("/0")
        reads = {}
        for index, (name, _case) in enumerate(cases):
            if index:
                await env.manager.navigate(session_id, f"{env.allowed}/{index}")
            # A text of its own each, so no approval is reused for the next page.
            change = BrowserActionDecision(
                action="type", reason="change it", selector="#named", text=f"{index:02d} {TEXT}", risk_category="post"
            )
            approval = await _asked_for(env, session_id, change)
            reads[name] = (approval["field_read"], approval["action"]["text"])
        listed = json.dumps(await env.manager.list_approvals())

    assert reads == {
        name: (read, f"{index:02d} {TEXT}" if read == "plain" else HIDDEN[read])
        for index, (name, (_page, read)) in enumerate(cases)
    }
    for index, (name, (_page, read)) in enumerate(cases):
        assert (f"{index:02d} {TEXT}" in listed) is (read == "plain"), name


# A focusable component whose open shadow root holds a password field: observe gives the component an element_id.
HOLDS_A_PASSWORD_FIELD = (
    '<title>login</title><div id="box" tabindex="0" role="textbox" aria-label="User"></div>'
    f"<script>document.getElementById(`box`).{PASSWORD_IN};</script>"
)


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_an_approval_for_a_type_into_a_component_reads_the_password_field_inside_it(
    tmp_path: Path, by: str
) -> None:
    # The same read in the element ids' world: the component's own attributes say it is a text box called User.
    async with browser_env(tmp_path, {"/login": HOLDS_A_PASSWORD_FIELD}) as env:
        session_id = await env.create("/login")
        named = {"selector": "#box"}
        if by == "element_id":
            items = (await env.manager.observe(session_id, preset="text"))["interactables"]
            (field,) = [item for item in items if item.get("label") == "User"]
            named = {"element_id": field["element_id"]}
        change = BrowserActionDecision(
            action="type", reason="change it", text=TEXT, risk_category="account_change", **named
        )

        approval = await _asked_for(env, session_id, change)

        assert (approval["field_read"], approval["action"]["text"]) == ("secret", HIDDEN["secret"])
        assert TEXT not in json.dumps(await env.manager.list_approvals())

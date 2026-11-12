"""What a type into a secret field keeps of its text, in Chromium, through the real BrowserManager, pipeline and
witness store.

A type decides whether its field is secret from the field's attributes as it runs. Attributes it could not read were
taken to say "not secret", so a password field that came in after the read had timed out kept the typed text as the
action's preview: in the result, actions.jsonl, the audit log and the receipt; and a type into a field that never
came kept the text it was to type in its failure record. Only the page server is local
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

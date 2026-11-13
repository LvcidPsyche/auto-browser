"""An approval for a type shows its text only where a read of its field says the field is not secret.

A type labelled post, payment, account_change or destructive asks for approval before it runs, so before any read of
its field: the approval's summary (the webhook's and the SSE event's), what GET /approvals answers and the webhook
carried the text whole when the caller did not mark it sensitive, a password typed into a password field included,
with no read of the field. Through the real manager, approval store, witness store and webhook dispatch.

The approval records how the field read (ApprovalRecord.field_read): plain, secret, or unread. One flag that only
latched on hid a post's text for good once a read had failed, and a record stored before the flag read as shown.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app import events, webhooks
from app.approvals import ApprovalRequiredError
from app.audit import reset_current_operator, set_current_operator
from app.browser.services import actions
from app.models import BrowserActionDecision
from app.utils import utc_now
from tests.test_browser_manager_actions import FakeLocator
from tests.test_witness_evidence_mode import ELEMENT_ID, NOT_SECRET, _typing_manager

TEXT = "hunter2-secret"
HOOK = "https://hooks.example.com/approval"
# What stands for a type's text where its field did not read plain (app.approval_display.FIELD_TEXT_HIDDEN).
SECRET = "text hidden: its field reads as secret"
UNREAD = "text hidden: its field has not been read"


async def _ask(tmp_path: Path, *, by: str, secret: bool, kind: str) -> SimpleNamespace:
    """Ask for the approval of a type of TEXT into a field that is a password field or a plain one, by ``by``, with
    the webhook on; what each of the approval's outlets carried, and the manager."""
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    manager.settings.approval_webhook_url = HOOK
    await manager.approvals.startup()
    await manager.witness.startup()
    attributes = {**NOT_SECRET, "type": "password" if secret else "text"}
    if by == "selector":
        field = FakeLocator("#f", attributes=attributes)
        manager.sessions["s1"].page.locators["#f"] = field
        reads = field.element.evaluate
        named: dict[str, Any] = {"selector": "#f"}
    else:
        reads = AsyncMock(return_value=_named(attributes))
        manager.element_ids.element = Mock(return_value=SimpleNamespace(evaluate=reads))
        named = {"element_id": ELEMENT_ID}
    decision = BrowserActionDecision(action="type", reason="change it", text=TEXT, risk_category=kind, **named)
    client = Mock(post=AsyncMock(return_value=Mock(status_code=200)))
    sent: list[Any] = []
    stream = events.subscribe("s1")
    token = set_current_operator("alice", name="Alice")
    try:
        with (
            patch.object(webhooks, "_resolve_host", AsyncMock(return_value=["93.184.216.34"])),
            patch.object(webhooks, "_env_proxy_applies", Mock(return_value=False)),
            patch.object(webhooks, "get_client", return_value=client),
            patch("app.browser.services.actions.spawn_background_task", side_effect=sent.append),
        ):
            with pytest.raises(ApprovalRequiredError) as asked:
                await manager.execute_decision("s1", decision)
            await asyncio.gather(*sent)
    finally:
        reset_current_operator(token)
        events.unsubscribe("s1", stream)
    approval_id = asked.value.approval.id
    streamed = []
    while not stream.empty():
        streamed.append(json.loads(stream.get_nowait()))
    return SimpleNamespace(
        manager=manager,
        decision=decision,
        approval_id=approval_id,
        reads=reads.await_count,
        answer=asked.value.payload,
        listed=await manager.list_approvals(),
        got=await manager.get_approval(approval_id),
        webhook=json.loads(client.post.await_args.kwargs["content"]),
        streamed=[event for event in streamed if event.get("event") == "approval"],
        receipts=[r for r in await manager.witness.list("s1") if r.event_type == "approval"],
    )


@pytest.mark.parametrize("kind", ["post", "account_change"])
@pytest.mark.parametrize("by", ["selector", "element_id"])
@pytest.mark.parametrize("secret", [True, False], ids=["password-field", "plain-field"])
async def test_an_approval_shows_a_types_text_only_where_its_field_reads_not_secret(
    tmp_path: Path, by: str, secret: bool, kind: str
) -> None:
    asked = await _ask(tmp_path, by=by, secret=secret, kind=kind)

    target = 'selector="#f"' if by == "selector" else f'element_id="{ELEMENT_ID}"'
    text = SECRET if secret else f'text="{TEXT}"'
    summary = f"type · {target} · {text} · clear_first=true"
    assert asked.webhook["action_summary"] == summary
    assert [event["action_summary"] for event in asked.streamed] == [summary]
    shown_text = SECRET if secret else TEXT
    for answer in (asked.answer["approval"], *asked.listed, asked.got):
        assert answer["action"]["text"] == shown_text
        assert answer["field_read"] == ("secret" if secret else "plain")
    outlets = json.dumps([asked.webhook, asked.streamed, asked.answer, asked.listed, asked.got])
    assert (TEXT in outlets) is not secret
    (pending,) = asked.receipts
    assert TEXT not in pending.model_dump_json()
    # What the approval binds is unchanged: it keeps the text, which the retried action is matched against.
    assert (await asked.manager.approvals.get(asked.approval_id)).action.text == TEXT
    assert asked.reads > 0  # read before the approval was asked for, as the type's operation reads it


async def test_the_type_retried_with_the_approval_runs_and_its_answers_keep_the_text_hidden(tmp_path: Path) -> None:
    asked = await _ask(tmp_path, by="selector", secret=True, kind="account_change")
    manager, page = asked.manager, asked.manager.sessions["s1"].page

    approved = await manager.approve(asked.approval_id)
    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.execute_decision("s1", asked.decision, approval_id=asked.approval_id)

    assert approved["action"]["text"] == SECRET
    assert "".join(call.args[0] for call in page.keyboard.type.await_args_list) == TEXT
    assert result["target"].get("text_redacted") is True and "text_preview" not in result["target"]
    assert (await manager.get_approval(asked.approval_id))["status"] == "executed"


def _post(by: str) -> BrowserActionDecision:
    named = {"selector": "#f"} if by == "selector" else {"element_id": ELEMENT_ID}
    return BrowserActionDecision(action="type", reason="r", text=TEXT, risk_category="post", **named)


def _named(*fields: dict[str, str], frame: bool = False) -> dict[str, Any]:
    """What the approval's one read answers of what a type names (element_scripts.NAMED_FIELDS_SCRIPT): the fields
    its text could go to from there, and whether a frame is among them."""
    return {"fields": list(fields), "frame": frame}


def _field(manager: Any, by: str, attributes: dict[str, str]) -> AsyncMock:
    """A field on the fake page, by selector or by element_id, whose attributes are ``attributes`` as they are when
    read (a test changes them); the mock the approval's read of it goes through, a test's to fail or to hang: asked
    of the element the selector finds, or of the element_id's."""
    if by == "selector":
        field = FakeLocator("#f", attributes=attributes)
        manager.sessions["s1"].page.locators["#f"] = field
        return field.element.evaluate
    reads = AsyncMock(side_effect=lambda *_, **__: _named({**NOT_SECRET, **attributes}))
    manager.element_ids.element = Mock(return_value=SimpleNamespace(evaluate=reads))
    return reads


async def _asked(manager: Any, decision: BrowserActionDecision) -> dict[str, Any]:
    """The approval the type is answered with when it is asked for: what the approval_required error carries."""
    with pytest.raises(ApprovalRequiredError) as asked:
        await manager.execute_decision("s1", decision)
    return asked.value.payload["approval"]


@pytest.mark.parametrize("later", ["plain", "secret"])
@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_an_approval_whose_field_could_not_be_read_is_settled_by_the_next_read_that_answers(
    tmp_path: Path, by: str, later: str
) -> None:
    # GLM's and Codex's read of FX9c3: one flag latched on at a read that failed (the field not on the page yet, the
    # page busy), and the same action asked for again reuses the record, so a post's approver stayed blind though a
    # later read found the field plain. A read that fails leaves the field unread, which hides the text, and the
    # next read that answers says which it is.
    manager = _typing_manager(tmp_path)
    await manager.approvals.startup()
    attributes = {"type": "text", "name": "note"}
    reads = _field(manager, by, attributes)
    answers = reads.side_effect
    reads.side_effect = PlaywrightTimeoutError("Timeout 1000ms exceeded.")

    first = await _asked(manager, _post(by))
    listed_unread = json.dumps(await manager.list_approvals())
    reads.side_effect = answers
    attributes["type"] = "text" if later == "plain" else "password"
    second = await _asked(manager, _post(by))

    assert first["id"] == second["id"]
    assert (first["field_read"], first["action"]["text"]) == ("unread", UNREAD)
    assert TEXT not in listed_unread
    assert (second["field_read"], second["action"]["text"]) == ((later, TEXT) if later == "plain" else (later, SECRET))
    for answer in (*await manager.list_approvals(), await manager.get_approval(first["id"])):
        assert answer["field_read"] == later
        assert (TEXT in json.dumps(answer)) is (later == "plain")
    # What the approval binds never changed: the text is kept, whatever is shown of it.
    assert (await manager.approvals.get(first["id"])).action.text == TEXT


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_field_once_read_as_secret_stays_hidden_whatever_a_later_read_says(tmp_path: Path, by: str) -> None:
    # The same action asked for again reuses its pending approval: one read as plain at first, then as a password
    # field (the page changed it), is hidden from then on, though a later read finds it plain again (a password
    # field whose "show password" is on) or fails.
    manager = _typing_manager(tmp_path)
    await manager.approvals.startup()
    attributes = {"type": "text", "name": "note"}
    reads = _field(manager, by, attributes)
    answers = reads.side_effect

    first = await _asked(manager, _post(by))
    attributes["type"] = "password"
    second = await _asked(manager, _post(by))
    attributes["type"] = "text"
    third = await _asked(manager, _post(by))
    reads.side_effect = PlaywrightTimeoutError("Timeout 1000ms exceeded.")
    fourth = await _asked(manager, _post(by))
    reads.side_effect = answers

    assert first["id"] == second["id"] == third["id"] == fourth["id"]
    assert (first["field_read"], first["action"]["text"]) == ("plain", TEXT)
    for later in (second, third, fourth):
        assert (later["field_read"], later["action"]["text"]) == ("secret", SECRET)
    assert TEXT not in json.dumps(await manager.list_approvals())


async def test_a_read_that_fails_leaves_a_field_read_as_plain_shown(tmp_path: Path) -> None:
    # A read that does not answer says nothing of the field: it neither hides a text read as plain nor shows one.
    manager = _typing_manager(tmp_path)
    await manager.approvals.startup()
    reads = _field(manager, "selector", {"type": "text", "name": "note"})

    first = await _asked(manager, _post("selector"))
    reads.side_effect = PlaywrightTimeoutError("Timeout 1000ms exceeded.")
    second = await _asked(manager, _post("selector"))

    assert first["id"] == second["id"]
    assert (second["field_read"], second["action"]["text"]) == ("plain", TEXT)


# An approval's record as 1.17.0 stored it: no word of how its field read.
LEGACY_ID = "0123456789ab"


def _stored_before_1_18_0(manager: Any, decision: BrowserActionDecision) -> None:
    now = utc_now()
    record = {
        "id": LEGACY_ID,
        "session_id": "s1",
        "kind": "post",
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "reason": "r",
        "action": decision.model_dump(mode="json"),
        "observation": None,
        "owner": None,
        "origin": "action",
    }
    root = Path(manager.approvals.file_store.root)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{LEGACY_ID}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


@pytest.mark.parametrize("decided", ["approve", "reject"])
async def test_a_pending_approval_stored_before_1_18_0_shows_no_text_until_its_field_is_read(
    tmp_path: Path, decided: str
) -> None:
    # Codex's read of FX9c3 (2): a record stored before the flag had none, so it read as shown. After the upgrade
    # the listing, the get and the approve answered a password typed into a password field, with no read of it: the
    # field is read only when a request creates or reuses an approval. A record that does not say how its field
    # read is unread, and its text hidden, until a request for the same action reads it.
    manager = _typing_manager(tmp_path)
    reads = _field(manager, "selector", {"type": "password"})
    _stored_before_1_18_0(manager, _post("selector"))
    await manager.approvals.startup()

    listed = await manager.list_approvals()
    got = await manager.get_approval(LEGACY_ID)

    assert [approval["id"] for approval in listed] == [LEGACY_ID]
    for answer in (*listed, got):
        assert TEXT not in json.dumps(answer), answer
        assert (answer["field_read"], answer["action"]["text"]) == ("unread", UNREAD)
    reads.assert_not_awaited()
    # A request for the same action reuses the record, and reads its field: a password field.
    again = await _asked(manager, _post("selector"))
    assert (again["id"], again["field_read"], again["action"]["text"]) == (LEGACY_ID, "secret", SECRET)
    answer = await (manager.approve if decided == "approve" else manager.reject)(LEGACY_ID)
    assert TEXT not in json.dumps(answer), answer


async def test_a_post_stored_before_1_18_0_shows_its_text_once_a_request_reads_its_field_plain(tmp_path: Path) -> None:
    manager = _typing_manager(tmp_path)
    _field(manager, "selector", {"type": "text", "name": "note"})
    _stored_before_1_18_0(manager, _post("selector"))
    await manager.approvals.startup()

    unread = await manager.get_approval(LEGACY_ID)
    again = await _asked(manager, _post("selector"))

    assert unread["action"]["text"] == UNREAD
    assert (again["id"], again["field_read"], again["action"]["text"]) == (LEGACY_ID, "plain", TEXT)


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_an_approvals_read_of_a_field_that_does_not_answer_is_bounded(tmp_path: Path, by: str) -> None:
    # GLM's read of FX9c3: by selector the approval's read waited ACTION_TIMEOUT_MS (15 s by default) for a field
    # not on the page, before the approval was even asked for. The read asks within FIELD_RECHECK_TIMEOUT_MS, and
    # is given up on at that bound whatever the page does with the question: the field is unread.
    manager = _typing_manager(tmp_path)
    await manager.approvals.startup()
    reads = _field(manager, by, {"type": "text", "name": "note"})
    given: list[dict[str, Any]] = []

    async def never(*_args: Any, **kwargs: Any) -> None:
        given.append(kwargs)
        await asyncio.Event().wait()

    reads.side_effect = never
    bound_ms = 200

    began = time.monotonic()
    with patch.object(actions, "FIELD_RECHECK_TIMEOUT_MS", bound_ms):
        approval = await asyncio.wait_for(_asked(manager, _post(by)), 10)
    took = time.monotonic() - began

    assert (approval["field_read"], approval["action"]["text"]) == ("unread", UNREAD)
    assert took < 5, took
    if by == "selector":
        # The wait for the selector's element has the bound, and the question asked of it is given up on at it.
        assert manager.sessions["s1"].page.locators["#f"].element_handle.await_args.kwargs == {"timeout": bound_ms}
        assert len(given) == 1
    else:
        assert given[0].get("within_ms") == bound_ms, given


PLAIN = {**NOT_SECRET}
PASSWORD = {**NOT_SECRET, "type": "password"}


@pytest.mark.parametrize(
    ("answer", "read"),
    [
        (_named(PLAIN), "plain"),
        (_named(PLAIN, PLAIN), "plain"),
        (_named(PLAIN, PASSWORD), "secret"),
        (_named(), "unread"),
        (_named(PLAIN, frame=True), "unread"),
        (_named(PASSWORD, frame=True), "secret"),
        (_named(frame=True), "unread"),
        (None, "unread"),
        ({"fields": [None], "frame": False}, "unread"),
    ],
    ids=[
        "one-plain-field",
        "two-plain-fields",
        "a-password-field-among-them",
        "no-field",
        "a-plain-field-and-a-frame",
        "a-password-field-and-a-frame",
        "a-frame-alone",
        "no-answer",
        "an-answer-of-another-shape",
    ],
)
@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_an_approval_reads_plain_only_where_every_field_the_text_could_go_to_does(
    tmp_path: Path, by: str, answer: Any, read: str
) -> None:
    # DeepSeek-V4.1-Flash-1017's read of FX9c4 (2): the approval read what the type names, where its text can go to
    # a field inside that (a component, a label, a form). The read answers every field the text could go to from
    # there: secret if one of them is, plain only if there is one and all are, and unread where there is none, a
    # frame is among them (its document is not read), or the answer is none of that shape.
    manager = _typing_manager(tmp_path)
    await manager.approvals.startup()
    reads = _field(manager, by, {})
    reads.side_effect = None
    reads.return_value = answer

    approval = await _asked(manager, _post(by))

    shown = {"plain": TEXT, "secret": SECRET, "unread": UNREAD}[read]
    assert (approval["field_read"], approval["action"]["text"]) == (read, shown)
    assert (TEXT in json.dumps(await manager.list_approvals())) is (read == "plain")


async def _given_back(disposed: AsyncMock) -> None:
    """Wait for what nothing awaits: an element's handle given back in the background."""
    for _ in range(200):
        if disposed.await_count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("not given back within 2 s")


async def test_an_approvals_read_by_selector_is_one_question_asked_of_the_element_the_selector_finds(
    tmp_path: Path,
) -> None:
    # The staged review of FX9c4 (16): by selector the read was six get_attribute calls, each of which found the
    # selector's field anew, so a field the page replaced between them read plain while it was a password field.
    # The element is found once, asked once for all its fields' attributes, and given back.
    manager = _typing_manager(tmp_path)
    await manager.approvals.startup()
    _field(manager, "selector", {"type": "text", "name": "note"})
    field = manager.sessions["s1"].page.locators["#f"]

    approval = await _asked(manager, _post("selector"))
    await _given_back(field.element.dispose)

    assert approval["field_read"] == "plain"
    assert (field.element_handle.await_count, field.element.evaluate.await_count) == (1, 1)
    assert field.element_handle.await_args.kwargs == {"timeout": actions.FIELD_RECHECK_TIMEOUT_MS}
    field.get_attribute.assert_not_awaited()
    field.evaluate.assert_not_awaited()  # not through the locator, which would find the field again
    field.element.dispose.assert_awaited_once()


@pytest.mark.parametrize("ends", ["the-read-fails", "the-read-does-not-answer", "the-element-comes-too-late"])
async def test_the_element_an_approvals_read_found_by_selector_is_given_back_however_the_read_ends(
    tmp_path: Path, ends: str
) -> None:
    # The handle of the selector's element is the controller's to give back: also where the question asked of it
    # fails or is given up on at the bound, and where the element arrives once the read was given up on.
    manager = _typing_manager(tmp_path)
    await manager.approvals.startup()
    reads = _field(manager, "selector", {"type": "text", "name": "note"})
    field = manager.sessions["s1"].page.locators["#f"]
    arrives = asyncio.Event()

    async def never(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.Event().wait()

    async def late(**_kwargs: Any) -> Any:
        await arrives.wait()
        return field.element

    if ends == "the-read-fails":
        reads.side_effect = PlaywrightTimeoutError("Execution context was destroyed")
    elif ends == "the-read-does-not-answer":
        reads.side_effect = never
    else:
        field.element_handle = AsyncMock(side_effect=late)

    with patch.object(actions, "FIELD_RECHECK_TIMEOUT_MS", 200):
        approval = await asyncio.wait_for(_asked(manager, _post("selector")), 10)
        arrives.set()
        await _given_back(field.element.dispose)

    assert approval["field_read"] == "unread"
    field.element.dispose.assert_awaited_once()
    assert reads.await_count == (0 if ends == "the-element-comes-too-late" else 1)


async def test_a_text_the_caller_marked_sensitive_is_hidden_as_the_callers_and_not_read_for(tmp_path: Path) -> None:
    manager = _typing_manager(tmp_path)
    await manager.approvals.startup()
    page = manager.sessions["s1"].page
    decision = BrowserActionDecision(
        action="type", reason="r", selector="#pw", text=TEXT, sensitive=True, risk_category="payment"
    )

    with pytest.raises(ApprovalRequiredError) as asked:
        await manager.execute_decision("s1", decision)

    page.locators["#pw"].element_handle.assert_not_awaited()
    # Not read, and nothing rests on it: the caller's mark hides the text, and the record keeps only its digest.
    assert asked.value.payload["approval"]["field_read"] == "unread"
    assert asked.value.payload["approval"]["action"]["text"] not in (TEXT, SECRET, UNREAD)
    assert TEXT not in json.dumps(asked.value.payload)

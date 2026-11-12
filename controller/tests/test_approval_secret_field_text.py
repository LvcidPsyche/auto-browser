"""An approval for a type shows its text only where a read of its field says the field is not secret.

A type labelled post, payment, account_change or destructive asks for approval before it runs, so before any read of
its field: the approval's summary (the webhook's and the SSE event's), what GET /approvals answers and the webhook
carried the text whole when the caller did not mark it sensitive, a password typed into a password field included,
with no read of the field. Through the real manager, approval store, witness store and webhook dispatch.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app import events, webhooks
from app.approval_display import FIELD_TEXT_HIDDEN
from app.approvals import ApprovalRequiredError
from app.audit import reset_current_operator, set_current_operator
from app.models import BrowserActionDecision
from tests.test_browser_manager_actions import FakeLocator
from tests.test_witness_evidence_mode import ELEMENT_ID, NOT_SECRET, _typing_manager

TEXT = "hunter2-secret"
HOOK = "https://hooks.example.com/approval"


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
        reads = field.get_attribute
        named: dict[str, Any] = {"selector": "#f"}
    else:
        reads = AsyncMock(return_value=attributes)
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
    text = FIELD_TEXT_HIDDEN if secret else f'text="{TEXT}"'
    summary = f"type · {target} · {text} · clear_first=true"
    assert asked.webhook["action_summary"] == summary
    assert [event["action_summary"] for event in asked.streamed] == [summary]
    shown_text = FIELD_TEXT_HIDDEN if secret else TEXT
    for answer in (asked.answer["approval"], *asked.listed, asked.got):
        assert answer["action"]["text"] == shown_text
        assert answer["text_hidden"] is secret
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

    assert approved["action"]["text"] == FIELD_TEXT_HIDDEN
    assert "".join(call.args[0] for call in page.keyboard.type.await_args_list) == TEXT
    assert result["target"].get("text_redacted") is True and "text_preview" not in result["target"]
    assert (await manager.get_approval(asked.approval_id))["status"] == "executed"


async def test_a_field_that_cannot_be_read_hides_the_text(tmp_path: Path) -> None:
    manager = _typing_manager(tmp_path)
    await manager.approvals.startup()
    gone = AsyncMock(side_effect=RuntimeError("the element is gone"))
    manager.element_ids.element = Mock(return_value=SimpleNamespace(evaluate=gone))
    decision = BrowserActionDecision(
        action="type", reason="r", element_id=ELEMENT_ID, text=TEXT, risk_category="payment"
    )

    with pytest.raises(ApprovalRequiredError) as asked:
        await manager.execute_decision("s1", decision)

    assert asked.value.payload["approval"]["action"]["text"] == FIELD_TEXT_HIDDEN
    assert TEXT not in json.dumps(await manager.list_approvals())


async def test_a_later_request_that_reads_the_field_secret_hides_the_text_of_the_approval_it_reuses(
    tmp_path: Path,
) -> None:
    # The same action asked for again reuses its pending approval: one read as plain at first, then as a password
    # field (the page changed it), is hidden from then on.
    manager = _typing_manager(tmp_path)
    await manager.approvals.startup()
    field = FakeLocator("#f", attributes={"type": "text", "name": "note"})
    manager.sessions["s1"].page.locators["#f"] = field
    decision = BrowserActionDecision(action="type", reason="r", selector="#f", text=TEXT, risk_category="post")

    with pytest.raises(ApprovalRequiredError) as first:
        await manager.execute_decision("s1", decision)
    field.attributes["type"] = "password"
    with pytest.raises(ApprovalRequiredError) as second:
        await manager.execute_decision("s1", decision)

    assert first.value.approval.id == second.value.approval.id
    assert first.value.payload["approval"]["action"]["text"] == TEXT
    assert second.value.payload["approval"]["action"]["text"] == FIELD_TEXT_HIDDEN
    assert TEXT not in json.dumps(await manager.list_approvals())


async def test_a_text_the_caller_marked_sensitive_is_hidden_as_the_callers_and_not_read_for(tmp_path: Path) -> None:
    manager = _typing_manager(tmp_path)
    await manager.approvals.startup()
    page = manager.sessions["s1"].page
    decision = BrowserActionDecision(
        action="type", reason="r", selector="#pw", text=TEXT, sensitive=True, risk_category="payment"
    )

    with pytest.raises(ApprovalRequiredError) as asked:
        await manager.execute_decision("s1", decision)

    page.locators["#pw"].get_attribute.assert_not_awaited()
    assert asked.value.payload["approval"]["text_hidden"] is False
    assert TEXT not in json.dumps(asked.value.payload)

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
from pathlib import Path
from typing import Any

import pytest

from app.action_errors import BrowserActionError
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


async def test_a_password_field_that_appears_after_the_attribute_read_keeps_no_preview(tmp_path: Path) -> None:
    async with browser_env(tmp_path, {"/late": LATE}) as env:
        # 1.4 s in: after the attribute read's 1 s timeout, and within the scroll's.
        session_id, session, spy = await _late_field_env(env, 1400)

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

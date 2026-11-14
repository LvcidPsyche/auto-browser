"""A confidential session's actions kept standard evidence, so receipts held the typed text."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.action_errors import BrowserActionError
from app.action_records import wait_for_pending_record
from app.approvals import ApprovalRequiredError
from app.audit import reset_current_operator, set_current_operator
from app.browser.element_scripts import ELEMENT_HAS_FOCUS_SCRIPT
from app.browser.services.actions import FIELD_RECHECK_TIMEOUT_MS
from app.browser.services.witness import BrowserWitnessService
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import BrowserActionDecision, WitnessRemoteState
from app.pii_scrub import PiiScrubber
from app.utils import UTC
from app.witness import (
    WitnessActionContext,
    WitnessApproval,
    WitnessPolicyEngine,
    WitnessPolicyOutcome,
    WitnessRecorder,
    WitnessSessionContext,
)
from tests.test_actions_verification import PAGE as VERIFIED_PAGE
from tests.test_actions_verification import URL as VERIFIED_URL
from tests.test_actions_verification import _session as _actions_session
from tests.test_browser_manager_actions import FakeContext, FakeLocator, FakePage
from tests.test_browser_manager_actions import _settings as _actions_settings

NAMED_OPERATOR = {"id": "alice", "source": "header"}
TYPED = {"selector": "#note", "clear_first": True, "text_preview": "ordinary note text"}


def _session(profile: str) -> WitnessSessionContext:
    return WitnessSessionContext(session_id="s1", profile=profile, operator=NAMED_OPERATOR)


def _type_action() -> WitnessActionContext:
    return WitnessActionContext(action="type", action_class="write", target=TYPED)


def _receipt_target(profile: str) -> dict:
    engine = WitnessPolicyEngine()
    outcome = engine.evaluate_action(session=_session(profile), action=_type_action())
    return engine.redact_target(TYPED, evidence_mode=outcome.evidence_mode)


def test_a_confidential_session_restricts_the_evidence_of_an_ordinary_action() -> None:
    outcome = WitnessPolicyEngine().evaluate_action(session=_session("confidential"), action=_type_action())

    assert outcome.evidence_mode == "restricted"


def test_a_confidential_receipt_target_redacts_the_typed_text() -> None:
    target = _receipt_target("confidential")

    assert target["text_preview"] == "[REDACTED]"
    assert target["selector"] == "#note"


def test_a_standard_session_is_unchanged() -> None:
    outcome = WitnessPolicyEngine().evaluate_action(session=_session("normal"), action=_type_action())

    assert outcome.evidence_mode == "standard"
    assert _receipt_target("normal") == TYPED


@pytest.mark.parametrize(("profile", "mode"), [("confidential", "restricted"), ("normal", "standard")])
def test_evaluate_session_and_evaluate_action_agree_on_the_mode(profile: str, mode: str) -> None:
    engine = WitnessPolicyEngine()

    assert engine.evaluate_session(_session(profile)).evidence_mode == mode
    assert engine.evaluate_action(session=_session(profile), action=_type_action()).evidence_mode == mode


def test_a_sensitive_action_in_a_normal_session_is_still_restricted() -> None:
    action = WitnessActionContext(action="type", action_class="write", sensitive_input=True)

    outcome = WitnessPolicyEngine().evaluate_action(session=_session("normal"), action=action)

    assert outcome.evidence_mode == "restricted"


def test_redact_target_only_touches_the_secret_keys_when_restricted() -> None:
    target = {"selector": "#f", "text": "t", "file_path": "/p", "password": "pw", "totp_secret": "s"}
    engine = WitnessPolicyEngine()

    restricted = engine.redact_target(target, evidence_mode="restricted")

    assert restricted == {
        "selector": "#f",
        "text": "[REDACTED]",
        "file_path": "[REDACTED]",
        "password": "[REDACTED]",
        "totp_secret": "[REDACTED]",
    }
    assert engine.redact_target(target, evidence_mode="standard") == target


def test_a_restricted_receipt_does_not_keep_what_a_select_option_chose() -> None:
    # The select_option target is {**target, value, label, index}; value and label are what it chose.
    target = {"selector": "#plan", "value": "enterprise", "label": None, "index": None}
    engine = WitnessPolicyEngine()

    restricted = engine.redact_target(target, evidence_mode="restricted")
    by_label = engine.redact_target(
        {"selector": "#plan", "value": None, "label": "Enterprise"}, evidence_mode="restricted"
    )

    assert restricted == {"selector": "#plan", "value": "[REDACTED]", "label": None, "index": None}
    assert by_label == {"selector": "#plan", "value": None, "label": "[REDACTED]"}
    assert engine.redact_target(target, evidence_mode="standard") == target


def test_a_restricted_receipt_does_not_keep_the_index_a_select_option_chose_by() -> None:
    target = {"selector": "#plan", "value": None, "label": None, "index": 0}
    engine = WitnessPolicyEngine()

    restricted = engine.redact_target(target, evidence_mode="restricted")

    assert restricted == {"selector": "#plan", "value": None, "label": None, "index": "[REDACTED]"}
    assert engine.redact_target(target, evidence_mode="standard") == target


# ACT-14: the targets real callers record (grep `target={` and `_run_action(`), which a restricted receipt keeps whole.
KEPT_TARGETS = [
    pytest.param({"url": "https://example.com/a"}, id="navigate"),
    pytest.param({"key": "Enter"}, id="press"),
    pytest.param({"delta_x": 0, "delta_y": 400}, id="scroll"),
    pytest.param({"wait_ms": 500}, id="wait"),
    pytest.param({"mode": "coordinates", "x": 10.0, "y": 20.0}, id="click-coordinates"),
    pytest.param({"mode": "selector", "element_id": "e1", "selector": "[data-ab='e1']"}, id="click-element"),
    pytest.param(
        {"mode": "selector", "selector": "#pw", "clear_first": True, "text_redacted": True}, id="type-sensitive"
    ),
    pytest.param({"key": "Enter", "totp": {"selector": "#otp", "code_length": 6}}, id="press-after-totp"),
    pytest.param({"profile_name": "shop"}, id="auth-profile"),
    pytest.param({"path": "/data/auth/s1/state.json"}, id="storage-state"),
    pytest.param(
        {"kind": "write", "action": "click", "selector": "#pay", "element_id": None, "origin": "action"},
        id="approval-requested",
    ),
    pytest.param(
        {"kind": "write", "action": "request_human_takeover", "origin": "tool_call", "tool": "browser.eval_js"},
        id="approval-decided",
    ),
]


@pytest.mark.parametrize("target", KEPT_TARGETS)
def test_a_restricted_receipt_keeps_the_targets_real_callers_record(target: dict) -> None:
    assert WitnessPolicyEngine.redact_target(target, evidence_mode="restricted") == target


def test_a_restricted_target_redacts_secret_named_keys_at_any_depth() -> None:
    target = {
        "selector": "#f",
        "csrf_token": "c",
        "headers": {"Authorization": "Bearer t", "Accept": "text/html", "Set-Cookie": "sid=1"},
        "fields": [{"name": "q", "Password": "pw"}, {"name": "k", "client_secret": "s", "x-api-key": "k"}],
        "nested": {"deeper": {"session_cookie": "c", "api_key": "k", "text": "typed"}},
    }

    restricted = WitnessPolicyEngine.redact_target(target, evidence_mode="restricted")

    assert restricted == {
        "selector": "#f",
        "csrf_token": "[REDACTED]",
        "headers": {"Authorization": "[REDACTED]", "Accept": "text/html", "Set-Cookie": "[REDACTED]"},
        "fields": [
            {"name": "q", "Password": "[REDACTED]"},
            {"name": "k", "client_secret": "[REDACTED]", "x-api-key": "[REDACTED]"},
        ],
        "nested": {"deeper": {"session_cookie": "[REDACTED]", "api_key": "[REDACTED]", "text": "[REDACTED]"}},
    }
    assert target["headers"]["Authorization"] == "Bearer t"  # the caller's nested dicts are not mutated
    assert WitnessPolicyEngine.redact_target(target, evidence_mode="standard") == target


def test_a_restricted_upload_target_keeps_its_delivery_outcome_but_not_its_file() -> None:
    target = {
        "mode": "selector",
        "selector": "#file",
        "file_path": "/uploads/s1/payslip.pdf",
        "approved": True,
        "approval_id": "a1",
        "delivery": {"outcome": "not_confirmed", "reason": "the input holds other.pdf, not payslip.pdf"},
    }

    restricted = WitnessPolicyEngine.redact_target(target, evidence_mode="restricted")

    assert restricted == {
        "mode": "selector",
        "selector": "#file",
        "file_path": "[REDACTED]",
        "approved": True,
        "approval_id": "a1",
        "delivery": {"outcome": "not_confirmed", "reason": "[REDACTED]"},
    }


def _recording_service(tmp_path: Path) -> tuple[BrowserWitnessService, SimpleNamespace]:
    manager = SimpleNamespace(
        settings=SimpleNamespace(witness_enabled=True),
        witness=WitnessRecorder(tmp_path / "witness"),
        witness_policy=WitnessPolicyEngine(),
        witness_remote=SimpleNamespace(enabled=False),
        auth_state=SimpleNamespace(require_encryption=False, encryption_enabled=False),
        pii_scrubber=PiiScrubber.from_settings(Settings(_env_file=None)),
    )
    return BrowserWitnessService(manager), manager


def _browser_session(profile: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="s1",
        protection_mode=profile,
        isolation_mode="shared_browser_node",
        shared_takeover_surface=True,
        shared_browser_process=True,
        witness_remote_state=WitnessRemoteState(),
    )


REASON = 'Approve browser.eval_js with {"expression": "fill(\'4111 1111 1111 1111\')"}'
WRITE_CLICK = BrowserActionDecision(action="click", reason=REASON, selector="#pay", risk_category="write")


async def _record_approval_request(tmp_path: Path, profile: str, decision: BrowserActionDecision = WRITE_CLICK):
    service, manager = _recording_service(tmp_path)
    # As actions.py records an approval request: no policy outcome, the reason in the approval and the metadata, and
    # the action the approval is for.
    await service.record_receipt(
        _browser_session(profile),
        event_type="approval",
        status="pending",
        action="approval_requested",
        action_class="control",
        risk_category=decision.risk_category,
        approval=WitnessApproval(required=True, approval_id="a1", status="pending", reason=REASON),
        target={"kind": "write", "action": "click", "selector": "#pay", "element_id": None, "origin": "action"},
        metadata={"reason": REASON},
        approved_action=decision,
    )
    receipts = await manager.witness.list("s1")
    assert len(receipts) == 1
    return receipts[0]


async def test_a_confidential_sessions_approval_request_receipt_withholds_the_reason(tmp_path: Path) -> None:
    receipt = await _record_approval_request(tmp_path, "confidential")

    assert receipt.evidence_mode == "restricted"
    assert receipt.approval.reason == "[REDACTED]"
    assert receipt.approval.approval_id == "a1"
    assert receipt.metadata == {"reason": "[REDACTED]"}
    assert receipt.target == {
        "kind": "write",
        "action": "click",
        "selector": "#pay",
        "element_id": None,
        "origin": "action",
    }


async def test_a_normal_sessions_approval_request_receipt_keeps_the_reason(tmp_path: Path) -> None:
    receipt = await _record_approval_request(tmp_path, "normal")

    assert receipt.evidence_mode == "standard"
    assert receipt.approval.reason == REASON
    assert receipt.metadata == {"reason": REASON}
    assert receipt.concerns == []  # the evidence mode only: the concerns are the session's and the action's receipts'


# Approvals for actions whose own receipts are restricted in a normal session: high-risk, sensitive, an upload, and
# a governed tool call that moves auth material (its stand-in carries the tool's kind).
RESTRICTED_ACTIONS = [
    pytest.param(
        BrowserActionDecision(action="click", reason=REASON, selector="#pay", risk_category="payment"), id="payment"
    ),
    pytest.param(
        BrowserActionDecision(action="type", reason=REASON, selector="#card", text="4111", sensitive=True),
        id="sensitive-type",
    ),
    pytest.param(
        BrowserActionDecision(action="upload", reason=REASON, selector="#file", file_path="payslip.pdf"), id="upload"
    ),
    pytest.param(
        BrowserActionDecision(
            action="request_human_takeover",
            reason=REASON,
            risk_category="account_change",
            text="browser.save_auth_profile sha256:0",
        ),
        id="auth-tool-call",
    ),
]


@pytest.mark.parametrize("decision", RESTRICTED_ACTIONS)
async def test_a_normal_sessions_approval_request_for_a_restricted_action_withholds_the_reason(
    tmp_path: Path, decision: BrowserActionDecision
) -> None:
    receipt = await _record_approval_request(tmp_path, "normal", decision)

    assert receipt.evidence_mode == "restricted"
    assert receipt.approval.reason == "[REDACTED]"
    assert receipt.metadata == {"reason": "[REDACTED]"}
    assert receipt.concerns == []


async def _manager_with_session(tmp_path: Path) -> tuple[BrowserManager, BrowserSession]:
    """A real manager (approvals and witness started) holding a normal session whose click is a stub."""
    settings = Settings(
        _env_file=None,
        ARTIFACT_ROOT=str(tmp_path / "artifacts"),
        AUTH_ROOT=str(tmp_path / "auth"),
        UPLOAD_ROOT=str(tmp_path / "uploads"),
        APPROVAL_ROOT=str(tmp_path / "approvals"),
        AUDIT_ROOT=str(tmp_path / "audit"),
        WITNESS_ROOT=str(tmp_path / "witness"),
        SESSION_STORE_ROOT=str(tmp_path / "sessions"),
    )
    manager = BrowserManager(settings)
    await manager.approvals.startup()
    await manager.witness.startup()

    class _Page:
        url = "https://shop.example/checkout"

        async def title(self) -> str:
            return "Checkout"

    artifact_dir = tmp_path / "artifacts" / "s1"
    artifact_dir.mkdir(parents=True)
    session = BrowserSession(
        id="s1",
        name="s1",
        created_at=datetime.now(UTC),
        context=object(),  # type: ignore[arg-type]
        page=_Page(),  # type: ignore[arg-type]
        artifact_dir=artifact_dir,
        auth_dir=tmp_path / "auth" / "s1",
        upload_dir=tmp_path / "uploads" / "s1",
        takeover_url="http://127.0.0.1:6080/vnc.html",
        trace_path=artifact_dir / "trace.zip",
        protection_mode="normal",
    )
    session.auth_dir.mkdir(parents=True)
    session.upload_dir.mkdir(parents=True)
    manager.sessions[session.id] = session
    manager.click = AsyncMock(return_value={"action": "click"})  # type: ignore[method-assign]
    return manager, session


async def test_a_normal_sessions_approval_receipts_take_the_restricted_mode_of_the_action_they_approve(
    tmp_path: Path,
) -> None:
    # GLM review of c1 (H1): an approval's receipts took the session's mode only, so in a normal session the request,
    # the decision and the execution of a payment click's approval kept the reason (which previews the call) that the
    # click's own receipt withholds as a high-risk action's.
    manager, session = await _manager_with_session(tmp_path)
    decision = BrowserActionDecision(action="click", reason=REASON, element_id="pay", risk_category="payment")
    token = set_current_operator("alice", name="Alice")
    try:
        with pytest.raises(ApprovalRequiredError):
            await manager.execute_decision(session.id, decision)
        approval_id = (await manager.approvals.list(session_id=session.id))[0].id
        await manager.approve(approval_id)
        await manager.execute_approval(approval_id)
    finally:
        reset_current_operator(token)

    receipts = {item.status: item for item in await manager.witness.list(session.id) if item.event_type == "approval"}
    assert set(receipts) == {"pending", "approved", "executed"}
    for receipt in receipts.values():
        assert receipt.evidence_mode == "restricted"
        assert receipt.approval.reason == "[REDACTED]"
        assert receipt.approval.approval_id == approval_id
        assert receipt.concerns == []  # the action's concerns are its own receipt's
    assert receipts["pending"].metadata == {"reason": "[REDACTED]"}
    assert receipts["pending"].target["element_id"] == "pay"


@pytest.mark.parametrize("decide", ["approve", "reject"])
async def test_a_restricted_decision_receipt_withholds_the_operators_comment(tmp_path: Path, decide: str) -> None:
    # A4: the decision's comment is an operator's free text, kept in full beside a reason the receipt withheld.
    manager, session = await _manager_with_session(tmp_path)
    decision = BrowserActionDecision(action="click", reason=REASON, element_id="pay", risk_category="payment")
    token = set_current_operator("alice", name="Alice")
    try:
        with pytest.raises(ApprovalRequiredError):
            await manager.execute_decision(session.id, decision)
        approval_id = (await manager.approvals.list(session_id=session.id))[0].id
        await getattr(manager, decide)(approval_id, comment="card 4111 1111 1111 1111 is fine")
    finally:
        reset_current_operator(token)

    receipt = (await manager.witness.list(session.id))[0]  # newest first
    assert (receipt.event_type, receipt.action, receipt.evidence_mode) == ("approval", decide, "restricted")
    # Who decided is kept: no named credential proved the operator here, so it is None.
    assert receipt.metadata == {"comment": "[REDACTED]", "decided_via": "operator", "decided_by": None}
    assert (await manager.approvals.get(approval_id)).decision_comment == "card 4111 1111 1111 1111 is fine"


async def test_a_restricted_receipt_redacts_its_metadata_as_its_target(tmp_path: Path) -> None:
    service, manager = _recording_service(tmp_path)
    metadata = {
        "saved_to": "/data/auth/shop.json",
        "encrypted": True,
        "api_token": "t",
        "pages": [{"title": "Inbox", "session_cookie": "c"}],
    }

    await service.record_receipt(
        _browser_session("normal"),
        event_type="auth_profile",
        status="ok",
        action="save_auth_profile",
        action_class="auth",
        target={"profile_name": "shop"},
        outcome=WitnessPolicyOutcome(profile="normal", evidence_mode="restricted"),
        approval=WitnessApproval(required=True, approval_id="a2", status="approved", reason=None),
        metadata=metadata,
    )

    receipt = (await manager.witness.list("s1"))[0]
    assert receipt.metadata == {
        "saved_to": "/data/auth/shop.json",
        "encrypted": True,
        "api_token": "[REDACTED]",
        "pages": [{"title": "Inbox", "session_cookie": "[REDACTED]"}],
    }
    assert receipt.approval.reason is None  # no reason given stays None, so the receipt shows there was none
    assert metadata["api_token"] == "t"


# A navigate's URL can carry an OAuth code or a token in its query or fragment.
CODE_URL = "https://example.test/cb?code=sentinel-0123456789&state=x"
TOKEN_FRAGMENT_URL = "https://example.test/next#access_token=sentinel&x=1"
TOKEN_QUERY_URL = "https://example.test/p?token=sentinel"


async def _record_navigate(tmp_path: Path, profile: str):
    service, manager = _recording_service(tmp_path)
    await service.record_receipt(
        _browser_session(profile),
        event_type="browser_action",
        status="failed",
        action="navigate",
        action_class="read",
        target={
            "url": CODE_URL,
            "blocked_navigations": [{"URL": TOKEN_FRAGMENT_URL, "origin": "https://example.test"}],
        },
        metadata={"error": {"code": "browser_action_failed", "url": TOKEN_QUERY_URL}},
    )
    return (await manager.witness.list("s1"))[0]


async def test_a_restricted_receipt_scrubs_its_urls_as_the_run_trace_scrubs_a_urls_query(tmp_path: Path) -> None:
    receipt = await _record_navigate(tmp_path, "confidential")

    assert receipt.evidence_mode == "restricted"
    assert receipt.target == {
        "url": "https://example.test/cb?[REDACTED]&state=x",
        "blocked_navigations": [
            {"URL": "https://example.test/next#access_token=%5BREDACTED%5D&x=1", "origin": "https://example.test"}
        ],
    }
    assert receipt.metadata == {
        "error": {"code": "browser_action_failed", "url": "https://example.test/p?token=%5BREDACTED%5D"}
    }


async def test_a_standard_receipt_keeps_its_urls_whole(tmp_path: Path) -> None:
    receipt = await _record_navigate(tmp_path, "normal")

    assert receipt.evidence_mode == "standard"
    assert receipt.target["url"] == CODE_URL
    assert receipt.target["blocked_navigations"][0]["URL"] == TOKEN_FRAGMENT_URL
    assert receipt.metadata["error"]["url"] == TOKEN_QUERY_URL


def _typing_manager(tmp_path: Path) -> BrowserManager:
    """The real type through the real pipeline, on a fake page whose ``#pw`` is a password field."""
    manager = BrowserManager(_actions_settings(tmp_path))
    manager.audit.append = AsyncMock()
    manager._append_jsonl = AsyncMock()
    manager._persist_session = AsyncMock()
    manager._settle = AsyncMock()
    manager._ensure_witness_remote_ready = AsyncMock()
    manager._check_bot_challenge = AsyncMock(return_value=None)
    manager._light_snapshot = AsyncMock(return_value={**VERIFIED_PAGE, "tab_count": 1})
    manager._observation_payload = AsyncMock(return_value=VERIFIED_PAGE)
    page = FakePage(VERIFIED_URL)
    page.locators["#pw"] = FakeLocator("#pw", attributes={"type": "password"})
    manager.sessions["s1"] = _actions_session(tmp_path, page, FakeContext(page))
    return manager


@pytest.mark.parametrize("profile", ["normal", "confidential"])
@pytest.mark.parametrize("marked", [False, True], ids=["found-secret-as-it-runs", "marked-sensitive-by-the-caller"])
async def test_a_type_into_a_password_field_restricts_its_receipt(tmp_path: Path, marked: bool, profile: str) -> None:
    # The policy was asked once, before the type ran, when only the caller's mark said the text was secret: a field
    # the type found secret withheld its preview, and its receipt stayed standard, the full observations kept, with no
    # sensitive_input concern, where 1.17.0's CHANGELOG says such a field restricts its action's own receipt. A
    # confidential session's receipts are restricted whatever the action, so there the concern tells the two apart.
    manager = _typing_manager(tmp_path)
    manager.sessions["s1"].protection_mode = profile
    manager.settings.witness_enabled = True

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.type("s1", selector="#pw", text="hunter2-secret", sensitive=marked)

    assert result["target"]["text_redacted"] is True and "text_preview" not in result["target"]
    (receipt,) = [r for r in await manager.witness.list("s1") if r.event_type == "browser_action"]
    assert receipt.evidence_mode == "restricted"
    assert "sensitive_input" in [concern.code for concern in receipt.concerns]
    assert (receipt.evidence.before, receipt.evidence.after) == (None, None)


ELEMENT_ID = "op-" + "a" * 24
NOT_SECRET = {"type": "text", "name": "note", "id": "note", "placeholder": "", "aria-label": "", "autocomplete": ""}


def _fake_element(read: AsyncMock, *, released: AsyncMock | None = None) -> SimpleNamespace:
    """An ElementTarget on the fake page. ``read`` answers a type's reads of its field's attributes, through its hold
    of the node its text goes to, which is the field; unless ``released`` answers when the hold is given back (a text
    whose key has moved focus on by then). An approval's read is tests/test_approval_secret_field_text.py's."""
    return SimpleNamespace(hold_field=read, release_field=released or read)


async def _type_targets_and_receipt(manager: BrowserManager) -> tuple[list[dict[str, Any]], Any]:
    """The type's target in its actions.jsonl line and its audit entry, and its receipt."""
    lines = [call.args[1]["target"] for call in manager._append_jsonl.await_args_list]
    events = [
        call.kwargs["details"]["target"]
        for call in manager.audit.append.await_args_list
        if call.kwargs.get("event_type") == "browser_action"
    ]
    assert lines and events
    (receipt,) = [r for r in await manager.witness.list("s1") if r.event_type == "browser_action"]
    return [*lines, *events], receipt


def _assert_withheld_and_restricted(targets: list[dict[str, Any]], receipt: Any) -> None:
    for target in (*targets, receipt.target):
        assert "text_preview" not in target and target.get("text_redacted") is True, target
    assert receipt.evidence_mode == "restricted"
    assert "sensitive_input" in [concern.code for concern in receipt.concerns]


@pytest.mark.parametrize("read", ["fails", "says-not-secret"])
async def test_a_type_by_element_id_keeps_its_preview_only_once_its_field_is_read_not_secret(
    tmp_path: Path, read: str
) -> None:
    # By element_id the preview was withheld only where the field's attributes said secret, and a read of them that
    # failed (the element gone from the page, a page that did not answer) raised before that: the failure record
    # kept the text's first 80 characters, under a standard receipt. The preview is withheld first, and comes back
    # once a read has said the field is not secret.
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    if read == "fails":
        gone = BrowserActionError("named an element that is no longer on the page", code="stale_element_id")
        answer = AsyncMock(side_effect=gone)
    else:
        answer = AsyncMock(return_value=NOT_SECRET)
    manager.element_ids.element = Mock(return_value=_fake_element(answer))
    manager.actions.elements.focus = AsyncMock()
    manager.actions.elements.require_focus = AsyncMock()

    with patch("asyncio.sleep", new=AsyncMock()):
        if read == "fails":
            with pytest.raises(BrowserActionError, match="no longer on the page"):
                await manager.type("s1", element_id=ELEMENT_ID, text="hunter2-secret")
        else:
            await manager.type("s1", element_id=ELEMENT_ID, text="hunter2-secret")

    targets, receipt = await _type_targets_and_receipt(manager)
    if read == "fails":
        _assert_withheld_and_restricted(targets, receipt)
        return
    for target in (*targets, receipt.target):
        assert target.get("text_preview") == "hunter2-secret" and "text_redacted" not in target, target
    assert receipt.evidence_mode == "standard"


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_text_the_caller_marked_sensitive_stays_withheld_in_a_field_read_not_secret(
    tmp_path: Path, by: str
) -> None:
    # The preview withheld as the type begins comes back once the field reads not secret. A text the caller marked
    # sensitive has none to come back, and its mark stays.
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    manager.sessions["s1"].page.locators["#note"] = FakeLocator("#note", attributes={"type": "text", "name": "note"})
    manager.element_ids.element = Mock(return_value=_fake_element(AsyncMock(return_value=NOT_SECRET)))
    manager.actions.elements.focus = AsyncMock()
    manager.actions.elements.require_focus = AsyncMock()
    named = {"selector": "#note"} if by == "selector" else {"element_id": ELEMENT_ID}

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.type("s1", text="hunter2-secret", sensitive=True, **named)

    targets, receipt = await _type_targets_and_receipt(manager)
    _assert_withheld_and_restricted([result["target"], *targets], receipt)


async def test_a_type_cancelled_while_it_reads_its_fields_attributes_keeps_no_preview(tmp_path: Path) -> None:
    # A cancellation is no Exception: it went past the selector read's "what cannot be read is secret", and the record
    # the pipeline writes for an action cancelled once begun kept the preview, under a standard receipt.
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    session = manager.sessions["s1"]
    reading = asyncio.Event()

    async def never_answers(_script: str, **_given: Any) -> None:
        reading.set()
        await asyncio.Event().wait()

    field = FakeLocator("#field")
    field.element.evaluate_handle = AsyncMock(side_effect=never_answers)
    session.page.locators["#field"] = field

    typing = asyncio.create_task(manager.type("s1", selector="#field", text="hunter2-secret"))
    await asyncio.wait_for(reading.wait(), 5)
    typing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await typing
    await wait_for_pending_record(session)

    _assert_withheld_and_restricted(*await _type_targets_and_receipt(manager))


def _field_that_turns_secret(manager: BrowserManager, by: str, when: str) -> dict[str, str]:
    """A plain login field on the fake page that becomes a password field ``when`` (on its focus, or at the first
    keystroke typed into it), by selector or by element_id; the type's target naming it."""
    page = manager.sessions["s1"].page
    if by == "selector":
        field = FakeLocator("#login", attributes={"type": "text", "name": "login"})
        page.locators["#login"] = field

        def turn_secret() -> None:
            field.attributes["type"] = "password"

        named = {"selector": "#login"}
    else:
        answer = AsyncMock(return_value=NOT_SECRET)
        manager.element_ids.element = Mock(return_value=_fake_element(answer))
        manager.actions.elements.require_focus = AsyncMock()

        def turn_secret() -> None:
            answer.return_value = {**NOT_SECRET, "type": "password"}

        named = {"element_id": ELEMENT_ID}
    if when == "on-focus" and by == "selector":
        focus = manager.actions.focus_verified

        async def focus_then_secret(*args: Any, **kwargs: Any) -> None:
            await focus(*args, **kwargs)
            turn_secret()

        manager.actions.focus_verified = focus_then_secret  # type: ignore[method-assign]
    elif when == "on-focus":
        manager.actions.elements.focus = AsyncMock(side_effect=lambda *_: turn_secret())
    else:
        manager.actions.elements.focus = AsyncMock()
        page.keyboard.type = AsyncMock(side_effect=lambda *_: turn_secret())
    return named


@pytest.mark.parametrize("when", ["on-focus", "as-it-is-typed-into"])
@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_field_that_turns_secret_after_the_first_read_keeps_no_preview(
    tmp_path: Path, by: str, when: str
) -> None:
    # The preview came back at the first read of the field, before the scroll, the focus click and the clear: a login
    # form that makes its field a password field once it is focused had the text typed into a password field with its
    # preview kept, under a standard receipt. The field is read just before the first keystroke and again once the
    # text is in, and the preview comes back only if both say it is not secret.
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    named = _field_that_turns_secret(manager, by, when)
    page = manager.sessions["s1"].page

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.type("s1", text="hunter2-secret", **named)

    assert "".join(call.args[0] for call in page.keyboard.type.await_args_list) == "hunter2-secret"
    targets, receipt = await _type_targets_and_receipt(manager)
    _assert_withheld_and_restricted([result["target"], *targets], receipt)


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_secret_field_turned_plain_as_the_text_goes_in_keeps_no_preview(tmp_path: Path, by: str) -> None:
    # The other way round, which held before the type took hold of its node and must hold through it: a password
    # field at the first keystroke that the page makes a plain one as the text goes in (a "show password" switch a
    # key throws). The read once the text is in says not secret; the one before the first keystroke did not.
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    page = manager.sessions["s1"].page
    if by == "selector":
        field = FakeLocator("#login", attributes={"type": "password", "name": "login"})
        page.locators["#login"] = field
        page.keyboard.type = AsyncMock(side_effect=lambda *_: field.attributes.update(type="text"))
        named = {"selector": "#login"}
    else:
        answer = AsyncMock(return_value={**NOT_SECRET, "type": "password"})
        manager.element_ids.element = Mock(return_value=_fake_element(answer))
        manager.actions.elements.focus = AsyncMock()
        manager.actions.elements.require_focus = AsyncMock()
        page.keyboard.type = AsyncMock(side_effect=lambda *_: setattr(answer, "return_value", NOT_SECRET))
        named = {"element_id": ELEMENT_ID}

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.type("s1", text="hunter2-secret", **named)

    assert "".join(call.args[0] for call in page.keyboard.type.await_args_list) == "hunter2-secret"
    targets, receipt = await _type_targets_and_receipt(manager)
    _assert_withheld_and_restricted([result["target"], *targets], receipt)


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_read_once_the_text_is_in_that_fails_keeps_no_preview(tmp_path: Path, by: str) -> None:
    # The field read not secret before the first keystroke, and is gone once the text is in (the text's Enter
    # submitted its form, or the page took it out): what it was as the text went in is not known, and the type ran.
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    page = manager.sessions["s1"].page
    typed: list[str] = []
    page.keyboard.type = AsyncMock(side_effect=typed.append)
    if by == "selector":
        field = FakeLocator("#login", attributes={"type": "text", "name": "login"})
        hold = field._hold("")
        gone = PlaywrightError("Execution context was destroyed, most likely because of a navigation")
        read = hold.evaluate.side_effect
        hold.evaluate = AsyncMock(side_effect=lambda script: _raise(gone) if typed else read(script))
        field.element.evaluate_handle = AsyncMock(return_value=hold)
        page.locators["#login"] = field
        named = {"selector": "#login"}
    else:
        stale = BrowserActionError("named an element that is no longer on the page", code="stale_element_id")
        answer = AsyncMock(side_effect=lambda *_, **__: _raise(stale) if typed else NOT_SECRET)
        manager.element_ids.element = Mock(return_value=_fake_element(answer))
        manager.actions.elements.focus = AsyncMock()
        manager.actions.elements.require_focus = AsyncMock()
        named = {"element_id": ELEMENT_ID}

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.type("s1", text="hunter2-secret", **named)

    assert "".join(typed) == "hunter2-secret"
    targets, receipt = await _type_targets_and_receipt(manager)
    _assert_withheld_and_restricted([result["target"], *targets], receipt)
    if by == "selector":
        # No wait for the field remains: the read is of the node held, which fails at once where the page took it
        # away, and no read finds the selector's field anew (each waited a second for one gone). The hold is given
        # back all the same, and the element it was made on.
        field.get_attribute.assert_not_awaited()
        hold.dispose.assert_awaited_once()
        field.element.dispose.assert_awaited_once()


def _raise(error: Exception) -> Any:
    raise error


@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_text_whose_enter_moves_focus_to_another_field_keeps_no_preview(tmp_path: Path, by: str) -> None:
    # Codex's read of FX9c3 (1): both reads were of the field the type named, found anew each time, and the node its
    # text goes to can be another by then. A user field whose Enter focuses the password field took "alice", and the
    # password field took the rest, unread: the user field read plain before and after, so the preview kept the
    # password, under a standard receipt. The type takes hold of the node that has focus at the first keystroke, and
    # the preview comes back only if that node kept focus until the text was in.
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    page = manager.sessions["s1"].page
    user = FakeLocator("#user", attributes={"type": "text", "name": "user"})
    page.locators["#user"] = user
    moved: list[str] = []

    def key(char: str) -> None:
        if char == "\n":
            moved.append(char)
            for hold in user.holds:
                hold.focused = False

    page.keyboard.type = AsyncMock(side_effect=key)
    named = {"selector": "#user"}
    if by == "element_id":
        released = AsyncMock(side_effect=lambda *_, **__: None if moved else NOT_SECRET)
        element = _fake_element(AsyncMock(return_value=NOT_SECRET), released=released)
        manager.element_ids.element = Mock(return_value=element)
        manager.actions.elements.focus = AsyncMock()
        manager.actions.elements.require_focus = AsyncMock()
        named = {"element_id": ELEMENT_ID}

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.type("s1", text="alice\nhunter2-secret", **named)

    assert moved
    targets, receipt = await _type_targets_and_receipt(manager)
    _assert_withheld_and_restricted([result["target"], *targets], receipt)


async def test_a_field_turned_secret_then_replaced_by_a_plain_one_keeps_no_preview(tmp_path: Path) -> None:
    # Codex's read of FX9c3 (1): by selector each read found the selector's field anew. A field the page makes a
    # password field as the text goes in, and replaces with a plain input the same selector matches once it is in,
    # read plain at the second read, which was of the replacement. The reads are of the node the type holds.
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    page = manager.sessions["s1"].page
    original = {"type": "text", "name": "login"}
    field = FakeLocator("#login", attributes=original)
    page.locators["#login"] = field
    typed: list[str] = []

    def key(char: str) -> None:
        typed.append(char)
        original["type"] = "password"
        if len(typed) == len("hunter2-secret"):
            field.attributes = {"type": "text", "name": "login"}  # the selector's field from now on: another node

    page.keyboard.type = AsyncMock(side_effect=key)

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.type("s1", selector="#login", text="hunter2-secret")

    assert field.attributes is not original and original["type"] == "password"
    targets, receipt = await _type_targets_and_receipt(manager)
    _assert_withheld_and_restricted([result["target"], *targets], receipt)


def _plain_field(manager: BrowserManager, by: str) -> tuple[dict[str, str], Any]:
    """A plain field on the fake page that keeps focus, by selector or by element_id: the type's target naming it,
    and what the type's hold of it is given back through (the locator, whose holds it lists; the element's mock)."""
    if by == "selector":
        note = FakeLocator("#note", attributes={"type": "text", "name": "note"})
        manager.sessions["s1"].page.locators["#note"] = note
        return {"selector": "#note"}, note
    element = _fake_element(AsyncMock(return_value=NOT_SECRET), released=AsyncMock(return_value=NOT_SECRET))
    manager.element_ids.element = Mock(return_value=element)
    manager.actions.elements.focus = AsyncMock()
    manager.actions.elements.require_focus = AsyncMock()
    return {"element_id": ELEMENT_ID}, element


@pytest.mark.parametrize(
    "text", ["ordinary note text", "two\nlines"], ids=["plain-text", "an-enter-that-moves-nothing"]
)
@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_text_into_a_plain_field_that_keeps_focus_shows_its_preview(tmp_path: Path, by: str, text: str) -> None:
    # The control: the node held at the first keystroke kept focus and reads not secret, before and after.
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    named, _ = _plain_field(manager, by)

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.type("s1", text=text, **named)

    targets, receipt = await _type_targets_and_receipt(manager)
    for target in (result["target"], *targets, receipt.target):
        assert target.get("text_preview") == text and "text_redacted" not in target, target
    assert receipt.evidence_mode == "standard"


@pytest.mark.parametrize("ends", ["typed", "typing-fails", "cancelled"])
@pytest.mark.parametrize("by", ["selector", "element_id"])
async def test_a_type_gives_its_hold_of_the_field_back_however_it_ends(tmp_path: Path, by: str, ends: str) -> None:
    # The hold is a handle of the page's, made on the handle of the selector's element (by selector), or a record in
    # the element ids' world (by element_id), which the page keeps until it is given back: once, whether the text
    # went in, a keystroke failed or the type was cancelled as it typed.
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    session = manager.sessions["s1"]
    named, field = _plain_field(manager, by)
    typing_began = asyncio.Event()

    async def key(_char: str) -> None:
        typing_began.set()
        if ends == "typing-fails":
            raise PlaywrightError("Target page, context or browser has been closed")
        if ends == "cancelled":
            await asyncio.Event().wait()

    session.page.keyboard.type = AsyncMock(side_effect=key)

    typing = asyncio.create_task(manager.type("s1", text="ordinary note text", **named))
    await asyncio.wait_for(typing_began.wait(), 5)
    if ends == "typed":
        await typing
    elif ends == "typing-fails":
        with pytest.raises(BrowserActionError):
            await typing
    else:
        typing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await typing
        await wait_for_pending_record(session)

    if by == "selector":
        (hold,) = field.holds
        hold.dispose.assert_awaited_once()
        field.element.dispose.assert_awaited_once()
        field.get_attribute.assert_not_awaited()  # no read finds the selector's field anew
    else:
        field.release_field.assert_awaited_once()


@pytest.mark.parametrize("fails", ["finding-the-field", "taking-the-hold", "the-read-before-the-text"])
async def test_a_field_by_selector_that_cannot_be_held_or_read_keeps_no_preview_and_is_typed_into(
    tmp_path: Path, fails: str
) -> None:
    # By selector a hold that cannot be taken, or read, says nothing of the node the text goes to: the text is typed,
    # as before, and its preview stays withheld. The wait for the field is FIELD_RECHECK_TIMEOUT_MS at most: it had
    # focus a moment ago. What was taken before the step that failed is given back. A field that cannot be found is
    # typed into only where the type does not clear it first: one that does is refused (the test below).
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    page = manager.sessions["s1"].page
    field = FakeLocator("#note", attributes={"type": "text", "name": "note"})
    page.locators["#note"] = field
    gone = PlaywrightTimeoutError("Timeout 1000ms exceeded.")
    if fails == "finding-the-field":
        field.element_handle = AsyncMock(side_effect=gone)
    elif fails == "taking-the-hold":
        field.element.evaluate_handle = AsyncMock(side_effect=PlaywrightError("Execution context was destroyed"))
    else:
        hold = field._hold("")
        hold.evaluate = AsyncMock(side_effect=gone)
        field.element.evaluate_handle = AsyncMock(return_value=hold)

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await manager.type(
            "s1", selector="#note", text="hunter2-secret", clear_first=fails != "finding-the-field"
        )

    assert "".join(call.args[0] for call in page.keyboard.type.await_args_list) == "hunter2-secret"
    assert field.element_handle.await_args.kwargs["timeout"] == FIELD_RECHECK_TIMEOUT_MS
    assert field.element.dispose.await_count == (0 if fails == "finding-the-field" else 1)
    for hold in field.holds:
        hold.dispose.assert_awaited_once()
    targets, receipt = await _type_targets_and_receipt(manager)
    _assert_withheld_and_restricted([result["target"], *targets], receipt)


@pytest.mark.parametrize("lost", ["after-select-all", "through-the-clear", "not-found-again", "no-answer"])
async def test_a_type_by_selector_whose_field_does_not_keep_focus_through_its_clear_is_refused(
    tmp_path: Path, lost: str
) -> None:
    # FX9c4's staged review (15): the two focus checks through the clear ran for an element_id only. By selector a
    # select-all handler that moved focus to another field had that field emptied by the Delete, and the text typed
    # into it. The element the selector named as it was focused is found once and asked after each key of the
    # clear; one that is not found again, or does not answer, refuses the type as well.
    manager = _typing_manager(tmp_path)
    page = manager.sessions["s1"].page
    field = FakeLocator("#note", attributes={"type": "text", "name": "note"})
    page.locators["#note"] = field
    if lost == "not-found-again":
        field.element_handle = AsyncMock(side_effect=PlaywrightTimeoutError("Timeout 1000ms exceeded."))
    elif lost == "no-answer":
        field.element.evaluate = AsyncMock(side_effect=PlaywrightError("Execution context was destroyed"))
    else:
        field.element.evaluate = AsyncMock(side_effect=[False] if lost == "after-select-all" else [True, False])

    with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(BrowserActionError) as refused:
        await manager.type("s1", selector="#note", text="hunter2-secret")

    during = " through the clear" if lost == "through-the-clear" else " after select-all (nothing was cleared)"
    assert (refused.value.code, refused.value.retryable) == ("focus_lost", True)
    assert refused.value.message == f"Keyboard focus would not stay in the target field{during}, so nothing was typed."
    pressed = [call.args[0] for call in page.keyboard.press.await_args_list]
    assert pressed == (["Control+a", "Delete"] if lost == "through-the-clear" else ["Control+a"])
    page.keyboard.type.assert_not_awaited()
    # No hold was taken, and the element found is given back.
    assert field.holds == []
    assert field.element.dispose.await_count == (0 if lost == "not-found-again" else 1)


async def test_a_type_by_element_id_refused_at_its_clear_has_taken_no_hold_to_give_back(tmp_path: Path) -> None:
    # Kept: by element_id a type refused at its clear asked the element ids' world for nothing more. The give-back
    # now runs for a type refused there too (by selector it has the element it found to give back), and asks the
    # world only for a hold that was taken.
    manager = _typing_manager(tmp_path)
    hold = AsyncMock(return_value=NOT_SECRET)
    manager.element_ids.element = Mock(return_value=_fake_element(hold))
    manager.actions.elements.focus = AsyncMock()
    lost = BrowserActionError("Keyboard focus would not stay", code="focus_lost", action="type", retryable=True)
    manager.actions.elements.require_focus = AsyncMock(side_effect=lost)

    with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(BrowserActionError) as refused:
        await manager.type("s1", element_id=ELEMENT_ID, text="hunter2-secret")

    assert refused.value.code == "focus_lost"
    assert manager.actions.elements.require_focus.await_args.kwargs == {
        "during": " after select-all (nothing was cleared)"
    }
    hold.assert_not_awaited()  # neither taken nor given back: the fake's hold_field and release_field


async def test_the_focus_checks_through_a_clear_by_selector_ask_the_element_found_once(tmp_path: Path) -> None:
    # The control, and what is asked: the element the selector found as the type focused it, once after each key of
    # the clear, and the hold is made on the same element. The locator would find the selector's element anew.
    manager = _typing_manager(tmp_path)
    page = manager.sessions["s1"].page
    field = FakeLocator("#note", attributes={"type": "text", "name": "note"})
    page.locators["#note"] = field

    with patch("asyncio.sleep", new=AsyncMock()):
        await manager.type("s1", selector="#note", text="note")
    asked_of_the_locator = field.evaluate.await_count

    with patch("asyncio.sleep", new=AsyncMock()):
        await manager.type("s1", selector="#note", text="note", clear_first=False)

    assert [call.args[0] for call in page.keyboard.press.await_args_list] == ["Control+a", "Delete"]
    assert [call.args for call in field.element.evaluate.await_args_list] == [(ELEMENT_HAS_FOCUS_SCRIPT,)] * 2
    # One find for each type: the type that clears finds its element before the clear, and holds that one.
    assert field.element_handle.await_count == 2
    # The locator is asked as often with a clear as without: for the focus that precedes it, and no more.
    assert field.evaluate.await_count == 2 * asked_of_the_locator


@pytest.mark.parametrize("refusal", ["policy-block", "page-off-allowed-hosts"])
async def test_a_type_refused_before_its_operation_runs_keeps_no_preview(tmp_path: Path, refusal: str) -> None:
    # A policy block, or a page ALLOWED_HOSTS refuses, refuses a type before its operation runs, and the operation is
    # where the preview was withheld: the blocked record kept the text's first 80 characters in actions.jsonl, the
    # audit entry and a standard receipt, no attribute read and nothing typed. It is withheld before the pipeline.
    manager = _typing_manager(tmp_path)
    manager.settings.witness_enabled = True
    if refusal == "policy-block":
        decide = manager.witness_policy.evaluate_action

        def blocking(**given: Any) -> WitnessPolicyOutcome:
            return decide(**given).model_copy(update={"should_block": True, "block_reason": "not here"})

        manager.witness_policy.evaluate_action = blocking  # type: ignore[method-assign]
    else:
        manager.settings.allowed_hosts = "other.example"
    page = manager.sessions["s1"].page

    with pytest.raises(BrowserActionError) as refused:
        await manager.type("s1", selector="#pw", text="hunter2-secret")

    assert refused.value.code == "browser_action_blocked"
    page.locators["#pw"].get_attribute.assert_not_awaited()
    page.keyboard.type.assert_not_awaited()
    targets, receipt = await _type_targets_and_receipt(manager)
    assert receipt.status == "blocked"
    _assert_withheld_and_restricted(targets, receipt)

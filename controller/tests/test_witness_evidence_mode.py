"""A confidential session's actions kept standard evidence, so receipts held the typed text."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.approvals import ApprovalRequiredError
from app.audit import reset_current_operator, set_current_operator
from app.browser.services.witness import BrowserWitnessService
from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.models import BrowserActionDecision, WitnessRemoteState
from app.utils import UTC
from app.witness import (
    WitnessActionContext,
    WitnessApproval,
    WitnessPolicyEngine,
    WitnessPolicyOutcome,
    WitnessRecorder,
    WitnessSessionContext,
)

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

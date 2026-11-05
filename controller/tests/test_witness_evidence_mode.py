"""A confidential session's actions kept standard evidence, so receipts held the typed text."""

from __future__ import annotations

import pytest

from app.witness import WitnessActionContext, WitnessPolicyEngine, WitnessSessionContext

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

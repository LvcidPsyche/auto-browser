"""WitnessPolicyEngine.evaluate_session per profile, and redact_target per redacted key.

Coverage only: these describe what the engine does today and pass at once.
"""

from __future__ import annotations

import pytest

from app.witness import WitnessPolicyEngine, WitnessSessionContext

NAMED_OPERATOR = {"id": "alice", "source": "header"}
REDACTED_KEYS = ["text", "text_preview", "file_path", "password", "totp_secret"]


def _session(profile: str, **overrides) -> WitnessSessionContext:
    return WitnessSessionContext(session_id="s1", profile=profile, operator=NAMED_OPERATOR, **overrides)


def _codes(outcome) -> set[str]:
    """The concern codes as a set: the order the engine appends them in is not part of what is pinned."""
    return {concern.code for concern in outcome.concerns}


def test_normal_session_with_encrypted_auth_state_has_no_concerns() -> None:
    outcome = WitnessPolicyEngine().evaluate_session(_session("normal", auth_state_encrypted=True))

    assert (outcome.profile, outcome.evidence_mode, outcome.concerns) == ("normal", "standard", [])
    assert not outcome.should_block and not outcome.require_approval


def test_normal_session_with_plain_auth_state_warns_but_does_not_block() -> None:
    outcome = WitnessPolicyEngine().evaluate_session(_session("normal"))

    assert outcome.evidence_mode == "standard"
    assert _codes(outcome) == {"auth_state_unencrypted"}
    assert outcome.concerns[0].severity == "warn"
    assert not outcome.should_block and not outcome.require_approval


def test_confidential_session_on_the_shared_defaults_raises_both_isolation_concerns() -> None:
    outcome = WitnessPolicyEngine().evaluate_session(_session("confidential"))

    assert outcome.evidence_mode == "restricted"
    assert _codes(outcome) == {"shared_takeover_surface", "shared_browser_process"}
    assert {concern.severity for concern in outcome.concerns} == {"high"}
    # A session-level check informs; only an action can be blocked or need approval.
    assert not outcome.should_block and not outcome.require_approval


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"shared_takeover_surface": False}, {"shared_browser_process"}),
        ({"shared_browser_process": False}, {"shared_takeover_surface"}),
        ({"shared_takeover_surface": False, "shared_browser_process": False}, set()),
    ],
)
def test_confidential_session_reports_only_the_isolation_it_lacks(overrides: dict, expected: set[str]) -> None:
    outcome = WitnessPolicyEngine().evaluate_session(_session("confidential", **overrides))

    assert outcome.evidence_mode == "restricted"
    assert _codes(outcome) == expected


def test_characterization_confidential_session_currently_raises_no_unencrypted_auth_concern() -> None:
    # Pins what evaluate_session does today, not a policy: a normal session with plain auth state gets a warn, and the
    # confidential profile skips that branch. Whether it should report auth_state_unencrypted is undecided; if it
    # should, this test is the one to change.
    outcome = WitnessPolicyEngine().evaluate_session(
        _session(
            "confidential", shared_takeover_surface=False, shared_browser_process=False, auth_state_encrypted=False
        )
    )

    assert outcome.concerns == []


@pytest.mark.parametrize("key", REDACTED_KEYS)
def test_restricted_mode_redacts_each_secret_key_and_keeps_the_rest(key: str) -> None:
    target = {key: "secret-value", "selector": "#f"}

    redacted = WitnessPolicyEngine.redact_target(target, evidence_mode="restricted")

    assert redacted == {key: "[REDACTED]", "selector": "#f"}
    assert target[key] == "secret-value"  # the caller's dict is not mutated


@pytest.mark.parametrize("key", REDACTED_KEYS)
def test_standard_mode_keeps_each_secret_key(key: str) -> None:
    target = {key: "secret-value", "selector": "#f"}

    kept = WitnessPolicyEngine.redact_target(target, evidence_mode="standard")

    assert kept == target
    assert kept is not target  # a copy, so a later edit cannot reach the caller's dict


def test_restricted_mode_does_not_invent_keys_that_are_absent() -> None:
    assert WitnessPolicyEngine.redact_target({"selector": "#f"}, evidence_mode="restricted") == {"selector": "#f"}
    assert WitnessPolicyEngine.redact_target({}, evidence_mode="restricted") == {}

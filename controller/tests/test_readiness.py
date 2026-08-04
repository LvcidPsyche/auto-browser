from __future__ import annotations

from unittest.mock import MagicMock

from app.readiness import run_readiness_checks


def _settings(**kwargs) -> MagicMock:
    defaults = {
        "auth_state_encryption_key": None,
        "require_auth_state_encryption": False,
        "require_operator_id": False,
        "api_bearer_token": None,
        "session_isolation_mode": "shared_browser_node",
        "witness_enabled": True,
        "witness_remote_url": None,
        "allowed_hosts": "*",
        "pii_scrub_enabled": True,
        "require_approval_for_uploads": True,
    }
    defaults.update(kwargs)
    settings = MagicMock()
    for key, value in defaults.items():
        setattr(settings, key, value)
    return settings


def test_all_pass() -> None:
    settings = _settings(
        auth_state_encryption_key="somekey",
        require_operator_id=True,
        api_bearer_token="secret",
        session_isolation_mode="docker_ephemeral",
        witness_remote_url="https://witness.example",
        allowed_hosts="example.com",
    )

    report = run_readiness_checks(settings)

    assert report.overall == "pass"


def test_warn_no_bearer() -> None:
    report = run_readiness_checks(_settings(api_bearer_token=None))
    warned = {check.name for check in report.checks if check.status == "warn"}

    assert report.overall in {"warn", "fail"}
    assert "api_bearer_token" in warned


def test_fail_encryption_required_not_set() -> None:
    report = run_readiness_checks(_settings(auth_state_encryption_key=None, require_auth_state_encryption=True))

    assert report.overall == "fail"


def test_confidential_not_less_strict() -> None:
    settings = _settings()
    normal = run_readiness_checks(settings, mode="normal")
    confidential = run_readiness_checks(settings, mode="confidential")
    order = {"pass": 0, "warn": 1, "fail": 2}

    assert order[confidential.overall] >= order[normal.overall]

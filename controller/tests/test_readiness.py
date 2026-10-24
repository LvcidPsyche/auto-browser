from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.readiness import run_readiness_checks


@pytest.fixture(autouse=True)
def _tesseract_installed():
    """Whether the host has tesseract must not decide the other tests' outcome."""
    with patch("app.readiness.tesseract_available", return_value=True):
        yield


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
        "api_bearer_tokens": "",
        "force_governed_approvals": False,
        "pii_scrub_screenshot_strict": False,
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


def _governed_check(report):
    return next((check for check in report.checks if check.name == "governed_approvals"), None)


def test_governed_approvals_warn_in_multi_operator_mode_when_not_forced() -> None:
    report = run_readiness_checks(_settings(api_bearer_tokens="alice:token-a,bob:token-b"))

    check = _governed_check(report)
    assert check is not None and check.status == "warn"
    assert "FORCE_GOVERNED_APPROVALS" in (check.fix or "")
    assert report.overall == "warn"


def test_governed_approvals_pass_in_multi_operator_mode_when_forced() -> None:
    report = run_readiness_checks(
        _settings(api_bearer_tokens="alice:token-a,bob:token-b", force_governed_approvals=True)
    )

    check = _governed_check(report)
    assert check is not None and check.status == "pass"


def test_governed_approvals_are_not_flagged_with_one_credential() -> None:
    for settings in (_settings(api_bearer_token="shared"), _settings(api_bearer_tokens="alice:token-a")):
        assert _governed_check(run_readiness_checks(settings)) is None


# ── Screenshot scrubbing needs tesseract ──────────────────────────────────


def _check(report, name: str):
    return next((check for check in report.checks if check.name == name), None)


def test_warns_when_screenshot_scrubbing_is_on_but_tesseract_is_missing() -> None:
    with patch("app.readiness.tesseract_available", return_value=False):
        report = run_readiness_checks(_settings(pii_scrub_screenshot=True, ocr_enabled=True))

    check = _check(report, "screenshot_scrubbing")
    assert check is not None
    assert check.status == "warn"
    assert "tesseract" in check.message.lower()
    assert check.fix
    assert report.overall == "warn"


def test_no_warning_when_tesseract_is_present() -> None:
    with patch("app.readiness.tesseract_available", return_value=True):
        report = run_readiness_checks(_settings(pii_scrub_screenshot=True, ocr_enabled=True))

    assert _check(report, "screenshot_scrubbing").status == "pass"


def test_warns_when_ocr_is_disabled_while_screenshot_scrubbing_is_on() -> None:
    with patch("app.readiness.tesseract_available", return_value=True):
        report = run_readiness_checks(_settings(pii_scrub_screenshot=True, ocr_enabled=False))

    check = _check(report, "screenshot_scrubbing")
    assert check.status == "warn"
    assert "OCR_ENABLED" in check.message


def test_strict_mode_fails_when_tesseract_is_missing() -> None:
    with patch("app.readiness.tesseract_available", return_value=False):
        report = run_readiness_checks(
            _settings(pii_scrub_screenshot=True, pii_scrub_screenshot_strict=True, ocr_enabled=True)
        )

    check = _check(report, "screenshot_scrubbing")
    assert check.status == "fail"
    assert "PII_SCRUB_SCREENSHOT_STRICT" in check.message
    assert report.overall == "fail"


def test_strict_mode_fails_when_ocr_is_disabled() -> None:
    report = run_readiness_checks(
        _settings(pii_scrub_screenshot=True, pii_scrub_screenshot_strict=True, ocr_enabled=False)
    )

    assert _check(report, "screenshot_scrubbing").status == "fail"
    assert report.overall == "fail"


def test_strict_mode_passes_when_scrubbing_can_run() -> None:
    report = run_readiness_checks(
        _settings(pii_scrub_screenshot=True, pii_scrub_screenshot_strict=True, ocr_enabled=True)
    )

    assert _check(report, "screenshot_scrubbing").status == "pass"


@pytest.mark.parametrize("overrides", [{"pii_scrub_screenshot": False}, {"pii_scrub_enabled": False}])
def test_no_screenshot_check_when_screenshot_scrubbing_is_off(overrides: dict) -> None:
    with patch("app.readiness.tesseract_available", return_value=False):
        report = run_readiness_checks(_settings(ocr_enabled=True, **overrides))

    assert _check(report, "screenshot_scrubbing") is None

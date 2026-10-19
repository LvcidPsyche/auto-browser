"""Named tokens (API_BEARER_TOKENS) are authentication: three reports must say so.

L-3: readiness, the runtime policy and remote-access info checked API_BEARER_TOKEN
alone, so a deployment secured only by named tokens was reported as unauthenticated
while the middleware was in fact enforcing auth.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.browser.services.remote_access import BrowserRemoteAccessService
from app.config import Settings
from app.readiness import run_readiness_checks
from app.runtime_policy import validate_runtime_policy

NAMED = "alice:" + "a" * 40 + ",bob:" + "b" * 40


def _bearer_check(settings: Settings):
    return next(check for check in run_readiness_checks(settings).checks if check.name == "api_bearer_token")


def _api_auth_enabled(settings: Settings, tmp_path: str) -> bool:
    manager = SimpleNamespace(
        settings=SimpleNamespace(
            api_bearer_token=settings.api_bearer_token,
            api_bearer_tokens=settings.api_bearer_tokens,
            remote_access_info_path=tmp_path,
            takeover_url=None,
            remote_access_stale_after_seconds=60,
        )
    )
    return BrowserRemoteAccessService(manager).global_info()["api_auth_enabled"]


class NamedTokensCountAsAuthTests(unittest.TestCase):
    def named_only(self) -> Settings:
        return Settings(_env_file=None, API_BEARER_TOKENS=NAMED, API_BIND_SCOPE="loopback")

    def test_readiness_passes_with_only_named_tokens(self) -> None:
        self.assertEqual(_bearer_check(self.named_only()).status, "pass")

    def test_readiness_still_warns_with_no_credentials(self) -> None:
        self.assertEqual(_bearer_check(Settings(_env_file=None, API_BIND_SCOPE="loopback")).status, "warn")

    def test_runtime_policy_does_not_warn_with_only_named_tokens(self) -> None:
        warnings = validate_runtime_policy(self.named_only()).warnings

        self.assertFalse(any("API_BEARER_TOKEN is unset" in warning for warning in warnings), warnings)

    def test_runtime_policy_still_warns_with_no_credentials(self) -> None:
        warnings = validate_runtime_policy(Settings(_env_file=None, API_BIND_SCOPE="loopback")).warnings

        self.assertTrue(any("API_BEARER_TOKEN is unset" in warning for warning in warnings), warnings)

    def test_remote_access_reports_auth_enabled_with_only_named_tokens(self) -> None:
        self.assertTrue(_api_auth_enabled(self.named_only(), "does-not-exist.json"))

    def test_remote_access_reports_auth_disabled_with_no_credentials(self) -> None:
        self.assertFalse(_api_auth_enabled(Settings(_env_file=None, API_BIND_SCOPE="loopback"), "does-not-exist.json"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

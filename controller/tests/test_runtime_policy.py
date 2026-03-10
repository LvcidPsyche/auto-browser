from __future__ import annotations

import unittest

from app.config import Settings
from app.runtime_policy import validate_runtime_policy


class RuntimePolicyTests(unittest.TestCase):
    def test_development_allows_missing_prod_controls(self) -> None:
        settings = Settings(_env_file=None, APP_ENV="development")

        report = validate_runtime_policy(settings)

        self.assertTrue(report.ok)
        self.assertEqual(report.errors, [])

    def test_production_requires_security_basics(self) -> None:
        settings = Settings(_env_file=None, APP_ENV="production", REQUEST_RATE_LIMIT_ENABLED="false")

        report = validate_runtime_policy(settings)

        self.assertFalse(report.ok)
        self.assertIn("API_BEARER_TOKEN is required when APP_ENV=production", report.errors)
        self.assertIn("REQUIRE_OPERATOR_ID=true is required when APP_ENV=production", report.errors)
        self.assertIn("AUTH_STATE_ENCRYPTION_KEY is required when APP_ENV=production", report.errors)
        self.assertIn(
            "REQUIRE_AUTH_STATE_ENCRYPTION=true is required when APP_ENV=production",
            report.errors,
        )
        self.assertIn("REQUEST_RATE_LIMIT_ENABLED=true is required when APP_ENV=production", report.errors)

    def test_production_emits_operational_warnings(self) -> None:
        settings = Settings(
            _env_file=None,
            APP_ENV="production",
            API_BEARER_TOKEN="secret",
            REQUIRE_OPERATOR_ID="true",
            AUTH_STATE_ENCRYPTION_KEY="b" * 44,
            REQUIRE_AUTH_STATE_ENCRYPTION="true",
            ALLOWED_HOSTS="example.com",
            SESSION_ISOLATION_MODE="shared_browser_node",
            TAKEOVER_URL="http://127.0.0.1:6080/vnc.html",
            ISOLATED_TUNNEL_ENABLED="false",
            METRICS_ENABLED="false",
        )

        report = validate_runtime_policy(settings)

        self.assertTrue(report.ok)
        self.assertGreaterEqual(len(report.warnings), 3)
        self.assertTrue(any("ALLOWED_HOSTS" in warning for warning in report.warnings))
        self.assertTrue(any("docker_ephemeral" in warning for warning in report.warnings))
        self.assertTrue(any("TAKEOVER_URL" in warning for warning in report.warnings))
        self.assertTrue(any("METRICS_ENABLED" in warning for warning in report.warnings))


if __name__ == "__main__":
    unittest.main()

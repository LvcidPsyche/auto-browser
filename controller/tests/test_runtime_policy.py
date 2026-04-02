from __future__ import annotations

import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.runtime_policy import validate_runtime_policy


class HealthzUnixSocketServer:
    class _Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(4096)
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 15\r\n"
                b"Connection: close\r\n\r\n"
                b"{\"status\":\"ok\"}"
            )

    def __init__(self, socket_path: Path) -> None:
        class _Server(socketserver.UnixStreamServer):
            allow_reuse_address = True

        self.socket_path = socket_path
        self.server = _Server(str(socket_path), self._Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "HealthzUnixSocketServer":
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.socket_path.exists():
            self.socket_path.unlink()


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

    def test_confidential_default_emits_hardening_warnings(self) -> None:
        settings = Settings(
            _env_file=None,
            APP_ENV="production",
            API_BEARER_TOKEN="secret",
            REQUIRE_OPERATOR_ID="true",
            AUTH_STATE_ENCRYPTION_KEY="b" * 44,
            REQUIRE_AUTH_STATE_ENCRYPTION="false",
            SESSION_ISOLATION_MODE="shared_browser_node",
            WITNESS_PROTECTION_MODE_DEFAULT="confidential",
        )

        report = validate_runtime_policy(settings)

        self.assertTrue(any("WITNESS_PROTECTION_MODE_DEFAULT=confidential" in warning for warning in report.warnings))

    def test_confidential_remote_requirement_errors_when_default_is_confidential(self) -> None:
        settings = Settings(
            _env_file=None,
            APP_ENV="production",
            API_BEARER_TOKEN="secret",
            REQUIRE_OPERATOR_ID="true",
            AUTH_STATE_ENCRYPTION_KEY="b" * 44,
            REQUIRE_AUTH_STATE_ENCRYPTION="true",
            WITNESS_PROTECTION_MODE_DEFAULT="confidential",
            WITNESS_REMOTE_REQUIRED_FOR_CONFIDENTIAL="true",
        )

        report = validate_runtime_policy(settings)

        self.assertFalse(report.ok)
        self.assertTrue(any("WITNESS_REMOTE_REQUIRED_FOR_CONFIDENTIAL=true" in error for error in report.errors))

    def test_confidential_remote_requirement_warns_when_optional_by_default(self) -> None:
        settings = Settings(
            _env_file=None,
            APP_ENV="production",
            API_BEARER_TOKEN="secret",
            REQUIRE_OPERATOR_ID="true",
            AUTH_STATE_ENCRYPTION_KEY="b" * 44,
            REQUIRE_AUTH_STATE_ENCRYPTION="true",
            WITNESS_REMOTE_REQUIRED_FOR_CONFIDENTIAL="true",
        )

        report = validate_runtime_policy(settings)

        self.assertTrue(report.ok)
        self.assertTrue(any("hosted Witness delivery" in warning for warning in report.warnings))

    def test_production_rejects_invalid_provider_auth_modes(self) -> None:
        settings = Settings(
            _env_file=None,
            APP_ENV="production",
            API_BEARER_TOKEN="secret",
            REQUIRE_OPERATOR_ID="true",
            AUTH_STATE_ENCRYPTION_KEY="b" * 44,
            REQUIRE_AUTH_STATE_ENCRYPTION="true",
            OPENAI_AUTH_MODE="bogus",
        )

        report = validate_runtime_policy(settings)

        self.assertFalse(report.ok)
        self.assertIn("OPENAI_AUTH_MODE=bogus is invalid; expected one of: api, cli, host_bridge", report.errors)

    def test_production_rejects_cli_mode_without_expected_auth_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            settings = Settings(
                _env_file=None,
                APP_ENV="production",
                API_BEARER_TOKEN="secret",
                REQUIRE_OPERATOR_ID="true",
                AUTH_STATE_ENCRYPTION_KEY="b" * 44,
                REQUIRE_AUTH_STATE_ENCRYPTION="true",
                OPENAI_AUTH_MODE="cli",
                OPENAI_CLI_PATH="codex",
                CLI_HOME=tempdir,
            )

            with patch("app.runtime_policy.which", return_value="/usr/bin/codex"):
                report = validate_runtime_policy(settings)

        self.assertFalse(report.ok)
        self.assertIn(
            f"openai uses CLI auth but no auth state was found; expected one of: {Path(tempdir) / '.codex'}",
            report.errors,
        )

    def test_production_accepts_cli_mode_when_binary_and_auth_state_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            Path(tempdir, ".codex").mkdir()
            settings = Settings(
                _env_file=None,
                APP_ENV="production",
                API_BEARER_TOKEN="secret",
                REQUIRE_OPERATOR_ID="true",
                AUTH_STATE_ENCRYPTION_KEY="b" * 44,
                REQUIRE_AUTH_STATE_ENCRYPTION="true",
                OPENAI_AUTH_MODE="cli",
                OPENAI_CLI_PATH="codex",
                CLI_HOME=tempdir,
            )

            with patch("app.runtime_policy.which", return_value="/usr/bin/codex"):
                report = validate_runtime_policy(settings)

        self.assertTrue(report.ok)
        self.assertFalse(any("OPENAI_AUTH_MODE" in error for error in report.errors))

    def test_production_accepts_host_bridge_mode_when_socket_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            socket_path = Path(tempdir) / "codex.sock"
            settings = Settings(
                _env_file=None,
                APP_ENV="production",
                API_BEARER_TOKEN="secret",
                REQUIRE_OPERATOR_ID="true",
                AUTH_STATE_ENCRYPTION_KEY="b" * 44,
                REQUIRE_AUTH_STATE_ENCRYPTION="true",
                OPENAI_AUTH_MODE="host_bridge",
                OPENAI_HOST_BRIDGE_SOCKET=str(socket_path),
            )

            socket_path.write_text("", encoding="utf-8")
            bad_report = validate_runtime_policy(settings)
            self.assertFalse(bad_report.ok)
            self.assertTrue(any("not a Unix socket" in error for error in bad_report.errors))

            socket_path.unlink()
            with HealthzUnixSocketServer(socket_path):
                report = validate_runtime_policy(settings)

        self.assertTrue(report.ok)
        self.assertFalse(any("OPENAI_AUTH_MODE" in error for error in report.errors))


if __name__ == "__main__":
    unittest.main()

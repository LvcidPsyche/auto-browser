"""The controller's Host-header rules: DNS-rebinding defence and IPv6 parsing.

A tokenless controller is only safe on loopback if it also refuses requests
addressed to other names — otherwise a web page whose hostname re-resolves to
127.0.0.1 becomes a same-origin client of an API that asks for no credential.
Compose always set CONTROLLER_ALLOWED_HOSTS; running the controller directly
left it empty, and empty meant every Host was accepted.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.app_factory import install_controller_host_middleware
from app.config import Settings
from app.middleware.hosts import ControllerHostMiddleware, host_from_header, host_is_allowed
from app.middleware.http import install_controller_http_middleware

TOKEN = "t" * 40


def _client(**overrides) -> TestClient:
    application = FastAPI()

    @application.get("/sessions")
    def sessions() -> dict:
        return {"ok": True}

    @application.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    settings = Settings(_env_file=None, **overrides)
    install_controller_host_middleware(application, settings.controller_allowed_host_patterns)
    install_controller_http_middleware(
        application,
        settings=settings,
        rate_limiter=None,
        metrics=SimpleNamespace(enabled=False),
    )
    return TestClient(application)


class HostParsingTests(unittest.TestCase):
    def test_host_part_is_extracted_from_every_header_shape(self) -> None:
        self.assertEqual(host_from_header("[::1]:8000"), "::1")
        self.assertEqual(host_from_header("[::1]"), "::1")
        self.assertEqual(host_from_header("127.0.0.1:8000"), "127.0.0.1")
        self.assertEqual(host_from_header("Controller.Example.COM"), "controller.example.com")
        self.assertEqual(host_from_header(""), "")

    def test_ipv6_loopback_matches_the_entry_compose_ships(self) -> None:
        # Starlette split on the first colon, so "[::1]:8000" became "[" and
        # the ::1 entry in the shipped CONTROLLER_ALLOWED_HOSTS never matched.
        self.assertTrue(host_is_allowed("[::1]:8000", ["localhost", "127.0.0.1", "::1"]))
        self.assertTrue(host_is_allowed("[::1]:8000", ["[::1]"]))

    def test_wildcards_match_subdomains_only(self) -> None:
        self.assertTrue(host_is_allowed("a.example.com", ["*.example.com"]))
        self.assertFalse(host_is_allowed("example.com", ["*.example.com"]))
        self.assertFalse(host_is_allowed("evilexample.com", ["*.example.com"]))
        self.assertTrue(host_is_allowed("anything", ["*"]))

    def test_malformed_wildcards_are_rejected_at_startup(self) -> None:
        with self.assertRaises(ValueError):
            ControllerHostMiddleware(FastAPI(), allowed_hosts=["example.*"])
        with self.assertRaises(ValueError):
            ControllerHostMiddleware(FastAPI(), allowed_hosts=["*example.com"])


class TokenlessLoopbackHostGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _client(API_BIND_SCOPE="loopback", CONTROLLER_ALLOWED_HOSTS="")

    def test_a_rebound_hostname_is_refused(self) -> None:
        response = self.client.get("/sessions", headers={"host": "attacker.example:8000"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("CONTROLLER_ALLOWED_HOSTS", response.json()["detail"])

    def test_loopback_names_are_served(self) -> None:
        for host in ("127.0.0.1:8000", "localhost:8000", "localhost", "[::1]:8000"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/sessions", headers={"host": host}).status_code, 200)

    def test_healthz_stays_reachable_for_orchestrators(self) -> None:
        response = self.client.get("/healthz", headers={"host": "controller:8000"})

        self.assertEqual(response.status_code, 200)


class HostGuardScopeTests(unittest.TestCase):
    def test_a_bearer_credential_lifts_the_loopback_only_rule(self) -> None:
        client = _client(API_BIND_SCOPE="loopback", CONTROLLER_ALLOWED_HOSTS="", API_BEARER_TOKEN=TOKEN)

        refused = client.get("/sessions", headers={"host": "controller.internal"})
        allowed = client.get(
            "/sessions",
            headers={"host": "controller.internal", "authorization": f"Bearer {TOKEN}"},
        )

        self.assertEqual(refused.status_code, 401)
        self.assertEqual(allowed.status_code, 200)

    def test_configured_hosts_replace_the_loopback_default(self) -> None:
        client = _client(API_BIND_SCOPE="loopback", CONTROLLER_ALLOWED_HOSTS="controller.internal")

        self.assertEqual(client.get("/sessions", headers={"host": "controller.internal"}).status_code, 200)
        self.assertEqual(client.get("/sessions", headers={"host": "127.0.0.1"}).status_code, 400)
        self.assertEqual(client.get("/sessions", headers={"host": "attacker.example"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()

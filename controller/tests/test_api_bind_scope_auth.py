"""Authentication is required whenever the API is reachable off-box.

GHSA-xmh3-cw7j-9gp5: the controller shipped an unauthenticated control plane
because every layer that could have required a token failed open, and the one
hard check was gated on `APP_ENV=production` while compose defaulted `APP_ENV`
to `development`. The regression test that matters is therefore a *development*
configuration that is reachable: it must refuse to start.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth_policy import (
    BIND_SCOPE_EXPOSED,
    BIND_SCOPE_LOOPBACK,
    Credential,
    is_loopback_host,
    match_credential,
    resolve_auth_policy,
    resolve_bind_scope,
)
from app.config import Settings
from app.middleware.http import install_controller_http_middleware
from app.runtime_policy import MIN_PRODUCTION_BEARER_TOKEN_LENGTH, validate_runtime_policy

STRONG_TOKEN = "t" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH


def settings(**overrides) -> Settings:
    """A real Settings instance built the way the rest of the suite builds one.

    Fields carry aliases, so `Settings(...)` takes the UPPER_CASE alias — passing
    the snake_case field name is silently ignored, which is a trap worth failing
    loudly on rather than writing a test that quietly asserts the default.
    """
    return Settings(_env_file=None, **overrides)


def undeclared(scope: str = BIND_SCOPE_EXPOSED, **fields) -> SimpleNamespace:
    """A settings stand-in whose bind scope is the default, not a declaration.

    `Settings()` would inherit `API_BIND_SCOPE` from the ambient environment —
    conftest sets it — and that is exactly the distinction under test here, so
    these cases state `model_fields_set` directly instead.
    """
    return SimpleNamespace(api_bind_scope=scope, model_fields_set=frozenset(), **fields)


class BindScopeResolutionTests(unittest.TestCase):
    def test_explicit_scope_wins_over_the_environment(self) -> None:
        resolved = resolve_bind_scope(
            settings(API_BIND_SCOPE=BIND_SCOPE_EXPOSED),
            {"HOST": "127.0.0.1"},
        )
        self.assertEqual(resolved, BIND_SCOPE_EXPOSED)

    def test_an_env_provided_scope_counts_as_explicit(self) -> None:
        """conftest sets API_BIND_SCOPE, so this also pins the mechanism itself:
        pydantic-settings must record env-sourced values in model_fields_set."""
        self.assertIn("api_bind_scope", settings().model_fields_set)

    def test_loopback_bind_host_is_inferred_when_scope_is_not_declared(self) -> None:
        self.assertEqual(resolve_bind_scope(undeclared(), {"HOST": "127.0.0.1"}), BIND_SCOPE_LOOPBACK)
        self.assertEqual(resolve_bind_scope(undeclared(), {"UVICORN_HOST": "localhost"}), BIND_SCOPE_LOOPBACK)

    def test_undeclared_deployment_is_assumed_reachable(self) -> None:
        """The whole point of the default: silence must not mean 'safe'."""
        self.assertEqual(resolve_bind_scope(undeclared(), {}), BIND_SCOPE_EXPOSED)
        self.assertEqual(resolve_bind_scope(undeclared(), {"HOST": "0.0.0.0"}), BIND_SCOPE_EXPOSED)

    def test_an_undeclared_reachable_deployment_has_no_usable_credential(self) -> None:
        policy = resolve_auth_policy(undeclared(api_bearer_token=None), {})
        self.assertTrue(policy.required)
        self.assertFalse(policy.satisfiable)

    def test_loopback_host_forms(self) -> None:
        for host in ("127.0.0.1", "127.0.0.1:8000", "localhost", "localhost:8000", "::1", "[::1]", "[::1]:8000"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))
        for host in ("0.0.0.0", "10.0.0.5", "example.com", "", "  "):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))


class AuthPolicyTests(unittest.TestCase):
    def test_a_configured_token_is_always_enforced(self) -> None:
        policy = resolve_auth_policy(settings(API_BIND_SCOPE=BIND_SCOPE_LOOPBACK, API_BEARER_TOKEN=STRONG_TOKEN))
        self.assertTrue(policy.required)
        self.assertTrue(policy.satisfiable)

    def test_loopback_without_a_token_stays_open_with_the_escape_hatch(self) -> None:
        policy = resolve_auth_policy(settings(API_BIND_SCOPE=BIND_SCOPE_LOOPBACK, API_ALLOW_TOKENLESS_LOOPBACK="true"))
        self.assertFalse(policy.required)
        self.assertTrue(policy.satisfiable)

    def test_loopback_without_a_token_or_the_escape_hatch_is_required_and_unsatisfiable(self) -> None:
        # Startup generates a token before this is consulted (app/api_token.py); if none
        # was loaded, the answer is "no request is authorised", never "every request is".
        policy = resolve_auth_policy(settings(API_BIND_SCOPE=BIND_SCOPE_LOOPBACK, API_ALLOW_TOKENLESS_LOOPBACK="false"))
        self.assertTrue(policy.required)
        self.assertFalse(policy.satisfiable)

    def test_the_escape_hatch_never_opens_an_exposed_api(self) -> None:
        policy = resolve_auth_policy(settings(API_BIND_SCOPE=BIND_SCOPE_EXPOSED, API_ALLOW_TOKENLESS_LOOPBACK="true"))
        self.assertTrue(policy.required)
        self.assertFalse(policy.satisfiable)

    def test_exposed_without_a_token_is_required_and_unsatisfiable(self) -> None:
        policy = resolve_auth_policy(settings(API_BIND_SCOPE=BIND_SCOPE_EXPOSED))
        self.assertTrue(policy.required)
        self.assertFalse(policy.satisfiable)

    def test_only_spaces_and_tabs_around_the_presented_token_are_ignored(self) -> None:
        # HTTP's optional whitespace, so h11 and httptools agree; str.strip() would also drop
        # \xa0 and \x85, which only one of the parsers lets through.
        credentials = (Credential(token=STRONG_TOKEN),)
        for header in (f"Bearer \t{STRONG_TOKEN}", f"Bearer {STRONG_TOKEN}\t", f"\tBearer  {STRONG_TOKEN} "):
            with self.subTest(header=header):
                self.assertIsNotNone(match_credential(credentials, header))
        for header in (f"Bearer {STRONG_TOKEN}\xa0", f"Bearer \x85{STRONG_TOKEN}", f"Bearer\t{STRONG_TOKEN}"):
            with self.subTest(header=header):
                self.assertIsNone(match_credential(credentials, header))


class RuntimePolicyAuthTests(unittest.TestCase):
    def test_exposed_and_tokenless_is_a_startup_error_in_development(self) -> None:
        report = validate_runtime_policy(settings(APP_ENV="development", API_BIND_SCOPE=BIND_SCOPE_EXPOSED))
        self.assertFalse(report.ok)
        self.assertTrue(any("API_BEARER_TOKEN is required" in error for error in report.errors))

    def test_exposed_rejects_a_token_too_short_to_matter(self) -> None:
        report = validate_runtime_policy(
            settings(APP_ENV="development", API_BIND_SCOPE=BIND_SCOPE_EXPOSED, API_BEARER_TOKEN="x")
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("at least" in error for error in report.errors))

    def test_exposed_with_a_strong_token_raises_no_auth_error(self) -> None:
        report = validate_runtime_policy(
            settings(APP_ENV="development", API_BIND_SCOPE=BIND_SCOPE_EXPOSED, API_BEARER_TOKEN=STRONG_TOKEN)
        )
        self.assertEqual([e for e in report.errors if "API_BEARER_TOKEN" in e], [])

    def test_loopback_without_a_token_warns_but_starts_with_the_escape_hatch(self) -> None:
        report = validate_runtime_policy(
            settings(APP_ENV="development", API_BIND_SCOPE=BIND_SCOPE_LOOPBACK, API_ALLOW_TOKENLESS_LOOPBACK="true")
        )
        self.assertTrue(report.ok)
        self.assertTrue(any("API_BEARER_TOKEN is unset" in warning for warning in report.warnings))

    def test_loopback_without_a_token_or_the_escape_hatch_does_not_start(self) -> None:
        report = validate_runtime_policy(
            settings(APP_ENV="development", API_BIND_SCOPE=BIND_SCOPE_LOOPBACK, API_ALLOW_TOKENLESS_LOOPBACK="false")
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("API_TOKEN_FILE" in error for error in report.errors), report.errors)

    def test_production_still_requires_a_token_on_a_loopback_bind(self) -> None:
        """APP_ENV=production keeps its own floor; bind scope only widens it."""
        report = validate_runtime_policy(settings(APP_ENV="production", API_BIND_SCOPE=BIND_SCOPE_LOOPBACK))
        self.assertTrue(any("API_BEARER_TOKEN is required" in error for error in report.errors))

    def test_the_auth_error_is_reported_once_not_twice(self) -> None:
        report = validate_runtime_policy(settings(APP_ENV="production", API_BIND_SCOPE=BIND_SCOPE_EXPOSED))
        matching = [error for error in report.errors if "API_BEARER_TOKEN is required" in error]
        self.assertEqual(len(matching), 1)


class ServedRequestTests(unittest.TestCase):
    """What the gate does to real requests, not just what the policy object says.

    The original defect was a policy layer that reported the right thing while
    the request path did something else, so the effect is what gets asserted.
    """

    def _client(self, **overrides) -> TestClient:
        application = FastAPI()

        @application.get("/sessions")
        def sessions() -> dict:
            return {"ok": True}

        @application.get("/healthz")
        def healthz() -> dict:
            return {"ok": True}

        install_controller_http_middleware(
            application,
            settings=settings(**overrides),
            rate_limiter=None,
            metrics=SimpleNamespace(enabled=False),
        )
        return TestClient(application)

    def test_reachable_and_tokenless_refuses_every_protected_request(self) -> None:
        response = self._client(API_BIND_SCOPE=BIND_SCOPE_EXPOSED).get("/sessions")
        self.assertEqual(response.status_code, 401)

    def test_health_stays_reachable_so_orchestrators_still_work(self) -> None:
        response = self._client(API_BIND_SCOPE=BIND_SCOPE_EXPOSED).get("/healthz")
        self.assertEqual(response.status_code, 200)

    def test_loopback_and_tokenless_serves_local_development_with_the_escape_hatch(self) -> None:
        response = self._client(API_BIND_SCOPE=BIND_SCOPE_LOOPBACK, API_ALLOW_TOKENLESS_LOOPBACK="true").get(
            "/sessions"
        )
        self.assertEqual(response.status_code, 200)

    def test_loopback_and_tokenless_refuses_every_request_without_the_escape_hatch(self) -> None:
        client = self._client(API_BIND_SCOPE=BIND_SCOPE_LOOPBACK, API_ALLOW_TOKENLESS_LOOPBACK="false")
        self.assertEqual(client.get("/sessions").status_code, 401)
        # A container on the compose network forging a loopback name gets no further.
        self.assertEqual(client.get("/sessions", headers={"Host": "localhost"}).status_code, 401)

    def test_a_configured_token_is_demanded_and_accepted(self) -> None:
        client = self._client(API_BIND_SCOPE=BIND_SCOPE_EXPOSED, API_BEARER_TOKEN=STRONG_TOKEN)
        self.assertEqual(client.get("/sessions").status_code, 401)
        self.assertEqual(
            client.get("/sessions", headers={"Authorization": f"Bearer {STRONG_TOKEN}"}).status_code,
            200,
        )

    def test_a_token_with_trailing_whitespace_is_accepted_as_the_client_sends_it(self) -> None:
        # An unstripped "tok\n" in settings never matched a client that sent the bare token.
        client = self._client(API_BIND_SCOPE=BIND_SCOPE_EXPOSED, API_BEARER_TOKEN=f"{STRONG_TOKEN}\n")
        self.assertEqual(
            client.get("/sessions", headers={"Authorization": f"Bearer {STRONG_TOKEN}"}).status_code,
            200,
        )

    def test_whitespace_around_the_presented_token_is_not_part_of_it(self) -> None:
        # uvicorn's default parser (httptools) keeps trailing whitespace that h11 trims, and
        # RFC 7235 allows more than one space after the scheme: curl, the shell helpers and
        # third-party clients send whatever the operator pasted.
        client = self._client(API_BIND_SCOPE=BIND_SCOPE_EXPOSED, API_BEARER_TOKEN=f" {STRONG_TOKEN} ")
        for header in (
            f"Bearer {STRONG_TOKEN}",
            f"Bearer  {STRONG_TOKEN}",
            f"Bearer {STRONG_TOKEN} ",
            f" Bearer {STRONG_TOKEN}\t",
            # RFC 7235: the scheme is case-insensitive; the token is not, and stays an exact comparison.
            f"bearer {STRONG_TOKEN}",
        ):
            with self.subTest(header=header):
                self.assertEqual(client.get("/sessions", headers={"Authorization": header}).status_code, 200)
        for header in (
            f"Bearer {STRONG_TOKEN}x",
            f"Bearer {STRONG_TOKEN[:-1]}",
            f"Bearer {STRONG_TOKEN.upper()}",
            "Bearer",
            "Bearer  ",
            f"Basic {STRONG_TOKEN}",
            STRONG_TOKEN,
        ):
            with self.subTest(header=header):
                self.assertEqual(client.get("/sessions", headers={"Authorization": header}).status_code, 401)

    def test_a_whitespace_only_token_counts_as_unset(self) -> None:
        loopback = self._client(
            API_BIND_SCOPE=BIND_SCOPE_LOOPBACK, API_BEARER_TOKEN="  ", API_ALLOW_TOKENLESS_LOOPBACK="true"
        )
        self.assertEqual(loopback.get("/sessions").status_code, 200)
        closed = self._client(
            API_BIND_SCOPE=BIND_SCOPE_LOOPBACK, API_BEARER_TOKEN="  ", API_ALLOW_TOKENLESS_LOOPBACK="false"
        )
        self.assertEqual(closed.get("/sessions", headers={"Authorization": "Bearer   "}).status_code, 401)
        # Before, 40 spaces cleared the 32-character floor and started, and "Bearer" plus the
        # same spaces authenticated under httptools: a guessable credential.
        for blank in ("  ", " " * (MIN_PRODUCTION_BEARER_TOKEN_LENGTH + 8)):
            with self.subTest(length=len(blank)):
                report = validate_runtime_policy(
                    settings(APP_ENV="development", API_BIND_SCOPE=BIND_SCOPE_EXPOSED, API_BEARER_TOKEN=blank)
                )
                self.assertTrue(any("API_BEARER_TOKEN is required" in error for error in report.errors), report)


class ShippedComposeDeclarationTests(unittest.TestCase):
    """Each shipped compose file must declare the scope its publish mapping creates.

    The Codespaces overlay is the security-relevant direction: drop its
    declaration and it silently inherits `loopback` from the base file, so the
    controller-side guard disappears while the file still publishes on 0.0.0.0.
    """

    REPO_ROOT = Path(__file__).resolve().parents[2]

    def _compose(self, name: str) -> str:
        path = self.REPO_ROOT / name
        if not path.is_file():  # not shipped inside the controller image
            self.skipTest(f"{name} is not present in this checkout")
        return path.read_text(encoding="utf-8")

    def test_base_compose_declares_loopback_and_publishes_on_loopback(self) -> None:
        text = self._compose("docker-compose.yml")
        self.assertIn("API_BIND_SCOPE: ${API_BIND_SCOPE:-loopback}", text)
        self.assertIn('"127.0.0.1:${API_PORT:-8000}:8000"', text)

    def test_base_compose_pins_the_token_file_and_keeps_the_escape_hatch_off(self) -> None:
        # Pinned, so a host path in .env cannot move the token where clients do not look.
        text = self._compose("docker-compose.yml")
        self.assertIn("API_TOKEN_FILE: /data/api-token\n", text)
        self.assertIn("API_ALLOW_TOKENLESS_LOOPBACK: ${API_ALLOW_TOKENLESS_LOOPBACK:-false}", text)

    def test_base_compose_pins_the_takeover_token_file_and_passes_the_shell_token(self) -> None:
        # W2-1/W2-20: scripts/load_env.sh's takeover_token reads /data/takeover-token, so compose must pin it there
        # (a TAKEOVER_TOKEN_FILE in .env made the controller use another file and the scripts got a 403), and a
        # TAKEOVER_TOKEN exported in the shell must reach the controller as API_BEARER_TOKEN does.
        text = self._compose("docker-compose.yml")
        self.assertIn("      TAKEOVER_TOKEN_FILE: /data/takeover-token\n", text)
        self.assertIn("      TAKEOVER_TOKEN: ${TAKEOVER_TOKEN:-}\n", text)

    def test_codespaces_overlay_declares_exposed_because_it_publishes_widely(self) -> None:
        text = self._compose("docker-compose.codespaces.yml")
        self.assertIn('"0.0.0.0:${API_PORT:-8000}:8000"', text)
        self.assertIn("API_BIND_SCOPE: exposed", text)


class ApiDocsScopeTests(unittest.TestCase):
    """/docs, /redoc and /openapi.json need no token on loopback, where the browser is the operator's own."""

    DOCS = ("/openapi.json", "/docs", "/redoc")

    def client(self, scope: str, **fields) -> TestClient:
        application = FastAPI()
        install_controller_http_middleware(
            application,
            settings=settings(API_BIND_SCOPE=scope, API_BEARER_TOKEN=STRONG_TOKEN, **fields),
            rate_limiter=None,
            metrics=SimpleNamespace(enabled=False),
        )
        return TestClient(application)

    def test_an_exposed_deployment_answers_401_on_the_docs(self) -> None:
        for fields in ({}, {"APP_ENV": "production"}):
            client = self.client(BIND_SCOPE_EXPOSED, **fields)
            for path in self.DOCS:
                with self.subTest(path=path, fields=fields):
                    self.assertEqual(client.get(path).status_code, 401)
                    authed = client.get(path, headers={"Authorization": f"Bearer {STRONG_TOKEN}"})
                    self.assertEqual(authed.status_code, 200)

    def test_a_loopback_deployment_serves_the_docs_without_a_token(self) -> None:
        client = self.client(BIND_SCOPE_LOOPBACK)
        for path in self.DOCS:
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()

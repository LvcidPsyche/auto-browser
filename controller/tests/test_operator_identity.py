"""Operator identity must be provable, not merely asserted.

`X-Operator-Id` is a request header. Until 1.7.0 it was the only thing that ever
set the operator on an audit event, so the audit trail attributed actions to a
string the caller chose — which reads as identity while being a label. Reported
in GHSA-xmh3-cw7j-9gp5.

Named credentials (`API_BEARER_TOKENS`) make identity provable. The header is
kept for single-token and loopback deployments, where one shared credential
genuinely cannot distinguish operators, but it is then recorded as `source:
"header"` so nothing downstream can mistake it for authentication.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.audit import get_current_operator
from app.auth_policy import credentials_for, parse_operator_tokens
from app.config import Settings
from app.middleware.http import install_controller_http_middleware
from app.runtime_policy import MIN_PRODUCTION_BEARER_TOKEN_LENGTH, validate_runtime_policy

ALICE_TOKEN = "alice-" + "a" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH
BOB_TOKEN = "bob-" + "b" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH
SHARED_TOKEN = "shared-" + "s" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH
NAMED = f"alice:{ALICE_TOKEN},bob:{BOB_TOKEN}"


def client_for(**overrides) -> TestClient:
    application = FastAPI()

    @application.get("/whoami")
    def whoami() -> dict:
        return get_current_operator().model_dump()

    install_controller_http_middleware(
        application,
        settings=Settings(_env_file=None, **overrides),
        rate_limiter=None,
        metrics=SimpleNamespace(enabled=False),
    )
    return TestClient(application)


class TokenParsingTests(unittest.TestCase):
    def test_named_pairs_are_parsed(self) -> None:
        credentials = parse_operator_tokens(NAMED)
        self.assertEqual([c.operator_id for c in credentials], ["alice", "bob"])
        self.assertTrue(all(c.verifies_identity for c in credentials))

    def test_a_token_may_contain_colons(self) -> None:
        (credential,) = parse_operator_tokens("alice:abc:def:ghi")
        self.assertEqual(credential.operator_id, "alice")
        self.assertEqual(credential.token, "abc:def:ghi")

    def test_malformed_entries_are_skipped_not_guessed(self) -> None:
        """A pair with no colon must not silently become a shared token."""
        self.assertEqual(parse_operator_tokens("no-colon-here"), ())
        self.assertEqual(parse_operator_tokens("  ,  , :,  x: "), ())

    def test_the_shared_token_carries_no_identity(self) -> None:
        (credential,) = credentials_for(
            Settings(_env_file=None, API_BEARER_TOKEN=SHARED_TOKEN, API_BEARER_TOKENS="")
        )
        self.assertIsNone(credential.operator_id)
        self.assertFalse(credential.verifies_identity)


class VerifiedIdentityTests(unittest.TestCase):
    def test_a_named_token_proves_who_the_caller_is(self) -> None:
        response = client_for(API_BEARER_TOKENS=NAMED).get(
            "/whoami", headers={"Authorization": f"Bearer {ALICE_TOKEN}"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "alice")
        self.assertEqual(response.json()["source"], "token")

    def test_a_forged_header_cannot_override_the_credential(self) -> None:
        response = client_for(API_BEARER_TOKENS=NAMED).get(
            "/whoami",
            headers={"Authorization": f"Bearer {ALICE_TOKEN}", "X-Operator-Id": "bob"},
        )
        body = response.json()
        self.assertEqual(body["id"], "alice")
        self.assertEqual(body["source"], "token")
        self.assertEqual(body["asserted_id"], "bob", "the false claim must survive into the audit trail")

    def test_an_agreeing_header_records_no_conflict(self) -> None:
        body = client_for(API_BEARER_TOKENS=NAMED).get(
            "/whoami",
            headers={"Authorization": f"Bearer {ALICE_TOKEN}", "X-Operator-Id": "alice"},
        ).json()
        self.assertEqual(body["id"], "alice")
        self.assertIsNone(body["asserted_id"])

    def test_a_named_token_satisfies_require_operator_id_without_a_header(self) -> None:
        response = client_for(API_BEARER_TOKENS=NAMED, REQUIRE_OPERATOR_ID="true").get(
            "/whoami", headers={"Authorization": f"Bearer {ALICE_TOKEN}"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "alice")

    def test_a_wrong_token_is_refused_before_identity_is_considered(self) -> None:
        response = client_for(API_BEARER_TOKENS=NAMED).get(
            "/whoami", headers={"Authorization": "Bearer not-a-real-token", "X-Operator-Id": "alice"}
        )
        self.assertEqual(response.status_code, 401)


class UnverifiedIdentityTests(unittest.TestCase):
    def test_the_shared_token_leaves_identity_self_asserted(self) -> None:
        body = client_for(API_BEARER_TOKEN=SHARED_TOKEN).get(
            "/whoami",
            headers={"Authorization": f"Bearer {SHARED_TOKEN}", "X-Operator-Id": "whoever"},
        ).json()
        self.assertEqual(body["id"], "whoever")
        self.assertEqual(body["source"], "header", "one shared credential cannot distinguish operators")

    def test_an_unauthenticated_loopback_request_is_still_attributed_by_header(self) -> None:
        body = client_for(API_BIND_SCOPE="loopback").get(
            "/whoami", headers={"X-Operator-Id": "local-dev"}
        ).json()
        self.assertEqual(body["id"], "local-dev")
        self.assertEqual(body["source"], "header")

    def test_no_claim_at_all_is_anonymous(self) -> None:
        body = client_for(API_BIND_SCOPE="loopback").get("/whoami").json()
        self.assertEqual(body["id"], "anonymous")
        self.assertEqual(body["source"], "anonymous")


class NamedTokenPolicyTests(unittest.TestCase):
    def test_a_weak_named_token_is_a_startup_error(self) -> None:
        report = validate_runtime_policy(
            Settings(_env_file=None, API_BIND_SCOPE="exposed", API_BEARER_TOKENS="alice:short")
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("operator 'alice'" in error for error in report.errors), report.errors)

    def test_named_tokens_alone_satisfy_the_requirement(self) -> None:
        report = validate_runtime_policy(
            Settings(_env_file=None, API_BIND_SCOPE="exposed", API_BEARER_TOKENS=NAMED)
        )
        self.assertEqual([e for e in report.errors if "BEARER" in e or "operator" in e], [])


if __name__ == "__main__":
    unittest.main()

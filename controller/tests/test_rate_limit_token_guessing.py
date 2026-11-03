"""Exempt paths skip the limiter only for requests that need no token or hold a valid one.

/metrics and /artifacts are exempt from the rate limiter (the UI loads many artifact files),
but they still demand the bearer token. Exempting them for every caller left them an
unthrottled place to guess it.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth_policy import BIND_SCOPE_EXPOSED, BIND_SCOPE_LOOPBACK
from app.middleware.http import install_controller_http_middleware
from app.rate_limits import SlidingWindowRateLimiter

TOKEN = "t" * 40
LIMIT = 5
EXEMPT = ["/healthz", "/readyz", "/docs", "/openapi.json", "/redoc", "/artifacts", "/metrics"]
WRONG = {"Authorization": "Bearer " + "x" * 40}
RIGHT = {"Authorization": f"Bearer {TOKEN}"}


def make_client(scope: str = BIND_SCOPE_LOOPBACK) -> TestClient:
    app = FastAPI()
    settings = SimpleNamespace(
        api_bearer_token=TOKEN,
        api_bind_scope=scope,
        model_fields_set=frozenset({"api_bind_scope"}),
        request_rate_limit_exempt_path_list=EXEMPT,
        operator_id_header="X-Operator-Id",
        operator_name_header="X-Operator-Name",
        require_operator_id=False,
    )
    install_controller_http_middleware(
        app,
        settings=settings,
        rate_limiter=SlidingWindowRateLimiter(limit=LIMIT, window_seconds=60),
        metrics=SimpleNamespace(enabled=False),
    )

    for path in ("/metrics", "/healthz", "/docs", "/artifacts/{a}/{b}"):

        @app.get(path)
        async def ok(a: str = "", b: str = "") -> dict[str, bool]:
            return {"ok": True}

    return TestClient(app)


class ExemptPathTokenGuessingTests(unittest.TestCase):
    def statuses(self, client: TestClient, path: str, headers: dict, count: int = 12) -> list[int]:
        return [client.get(path, headers=headers).status_code for _ in range(count)]

    def test_wrong_token_guesses_on_exempt_paths_get_429_after_the_limit(self) -> None:
        for path in ("/metrics", "/artifacts/x/y.png"):
            with self.subTest(path=path):
                codes = self.statuses(make_client(), path, WRONG)
                self.assertEqual(codes[:LIMIT], [401] * LIMIT)
                self.assertEqual(codes[LIMIT:], [429] * (12 - LIMIT))

    def test_tokenless_requests_to_exempt_paths_are_throttled_too(self) -> None:
        codes = self.statuses(make_client(), "/metrics", {})
        self.assertEqual(codes[LIMIT:], [429] * (12 - LIMIT))

    def test_valid_token_requests_are_never_throttled(self) -> None:
        client = make_client()
        self.assertEqual(set(self.statuses(client, "/metrics", RIGHT, 30)), {200})
        self.assertEqual(set(self.statuses(client, "/artifacts/x/y.png", RIGHT, 30)), {200})

    def test_healthchecks_are_never_throttled(self) -> None:
        client = make_client()
        self.assertEqual(set(self.statuses(client, "/healthz", {}, 30)), {200})

    def test_docs_skip_the_limiter_only_where_they_need_no_token(self) -> None:
        self.assertEqual(set(self.statuses(make_client(), "/docs", {}, 30)), {200})
        codes = self.statuses(make_client(BIND_SCOPE_EXPOSED), "/docs", {})
        self.assertEqual(codes[LIMIT:], [429] * (12 - LIMIT))


if __name__ == "__main__":
    unittest.main()

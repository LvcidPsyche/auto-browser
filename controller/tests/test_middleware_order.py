from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app.main as main_module

BAD_HOST = {"Host": "evil.example"}
BEARER = {"Authorization": "Bearer secret"}


class MiddlewareOrderTests(unittest.TestCase):
    """Order is behavior here, so these go through the real app, not a hand-built one."""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        for service, method_name in (
            (main_module.manager, "startup"),
            (main_module.manager, "shutdown"),
            (main_module.job_queue, "startup"),
            (main_module.job_queue, "shutdown"),
            (main_module.cron_service, "startup"),
            (main_module.cron_service, "shutdown"),
            (main_module.maintenance, "startup"),
            (main_module.maintenance, "shutdown"),
        ):
            self.stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
        self.stack.enter_context(patch.object(main_module.settings, "api_bearer_token", "secret"))
        self.stack.enter_context(patch.object(main_module.settings, "require_operator_id", True))
        self.evaluate = self.stack.enter_context(
            patch.object(main_module.rate_limiter, "evaluate", wraps=main_module.rate_limiter.evaluate)
        )
        self.client = self.stack.enter_context(TestClient(main_module.app))
        self.operator = {main_module.settings.operator_id_header: "alice"}

    def tearDown(self) -> None:
        self.stack.close()

    def test_bad_host_without_a_token_is_a_host_error_not_a_401(self) -> None:
        response = self.client.get("/sessions", headers={**BAD_HOST, **self.operator})

        self.assertEqual((response.status_code, response.text), (400, "Invalid host header"))

    def test_bad_host_with_a_valid_token_is_a_host_error(self) -> None:
        response = self.client.get("/sessions", headers={**BAD_HOST, **self.operator, **BEARER})

        self.assertEqual((response.status_code, response.text), (400, "Invalid host header"))

    def test_bad_host_without_an_operator_header_is_still_a_host_error(self) -> None:
        # The operator-id layer used to answer first with its own 400.
        response = self.client.get("/sessions", headers=BAD_HOST)

        self.assertEqual((response.status_code, response.text), (400, "Invalid host header"))

    def test_bad_host_does_not_spend_rate_limit_budget(self) -> None:
        self.client.get("/sessions", headers={**BAD_HOST, **self.operator})

        self.evaluate.assert_not_awaited()

    def test_good_host_without_a_token_is_still_a_401(self) -> None:
        response = self.client.get("/sessions", headers=self.operator)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.evaluate.assert_awaited_once()

    def test_good_host_with_a_valid_token_is_served(self) -> None:
        with patch.object(main_module.manager, "list_sessions", new=AsyncMock(return_value=[])):
            response = self.client.get("/sessions", headers={**self.operator, **BEARER})

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()

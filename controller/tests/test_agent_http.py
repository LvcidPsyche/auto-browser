from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import app.main as main_module
from app.models import AgentStepResult, ProviderInfo


class AgentHttpTests(unittest.TestCase):
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
        self.client = self.stack.enter_context(TestClient(main_module.app))

    def tearDown(self) -> None:
        self.stack.close()

    def test_list_agent_providers_returns_readiness_snapshot(self) -> None:
        list_providers = Mock(
            return_value=[
                ProviderInfo(provider="openai", configured=True, model="gpt-4.1-mini", auth_mode="api"),
                ProviderInfo(
                    provider="claude",
                    configured=False,
                    model="claude-sonnet-4-20250514",
                    auth_mode="api",
                    detail="ANTHROPIC_API_KEY is not configured",
                ),
            ]
        )

        with patch.object(main_module.orchestrator, "list_providers", list_providers):
            response = self.client.get("/agent/providers")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {"provider": "openai", "configured": True, "model": "gpt-4.1-mini", "auth_mode": "api", "detail": None, "login_command": None},
                {
                    "provider": "claude",
                    "configured": False,
                    "model": "claude-sonnet-4-20250514",
                    "auth_mode": "api",
                    "detail": "ANTHROPIC_API_KEY is not configured",
                    "login_command": None,
                },
            ],
        )
        list_providers.assert_called_once_with()

    def test_agent_step_returns_success_payload_with_mock_provider(self) -> None:
        step = AsyncMock(
            return_value=AgentStepResult(
                provider="openai",
                model="gpt-4.1-mini",
                goal="Inspect the page",
                status="done",
                observation={"url": "https://example.com", "title": "Example Domain"},
                decision={"action": "done", "reason": "Already on the target page", "risk_category": "read"},
                usage={"transport": "fake-provider"},
                raw_text='{"action":"done"}',
            )
        )

        with patch.object(main_module.orchestrator, "step", step):
            response = self.client.post(
                "/sessions/session-1/agent/step",
                json={
                    "provider": "openai",
                    "goal": "Inspect the page",
                    "observation_limit": 12,
                    "provider_model": "gpt-4.1-mini",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["usage"], {"transport": "fake-provider"})
        self.assertEqual(body["decision"]["action"], "done")
        self.assertEqual(step.await_args.kwargs["session_id"], "session-1")
        self.assertEqual(step.await_args.kwargs["provider_name"], "openai")
        self.assertEqual(step.await_args.kwargs["provider_model"], "gpt-4.1-mini")
        self.assertEqual(step.await_args.kwargs["observation_limit"], 12)

    def test_agent_step_surfaces_provider_failure_status_code(self) -> None:
        step = AsyncMock(
            return_value=AgentStepResult(
                provider="openai",
                model="gpt-4.1-mini",
                goal="Inspect the page",
                status="error",
                observation={"url": "https://example.com", "title": "Example Domain"},
                decision={},
                error="Provider unavailable",
                error_code=503,
            )
        )

        with patch.object(main_module.orchestrator, "step", step):
            response = self.client.post(
                "/sessions/session-1/agent/step",
                json={"provider": "openai", "goal": "Inspect the page"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["error"], "Provider unavailable")


if __name__ == "__main__":
    unittest.main()

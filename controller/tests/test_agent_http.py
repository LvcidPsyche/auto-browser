from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="auto-browser-agent-http-"))
atexit.register(lambda: shutil.rmtree(_TEST_ROOT, ignore_errors=True))
for env_name, relative_path in {
    "ARTIFACT_ROOT": "artifacts",
    "UPLOAD_ROOT": "uploads",
    "AUTH_ROOT": "auth",
    "APPROVAL_ROOT": "approvals",
    "AUDIT_ROOT": "audit",
    "SESSION_STORE_ROOT": "sessions",
    "JOB_STORE_ROOT": "jobs",
    "MCP_SESSION_STORE_PATH": "mcp/sessions.json",
    "CRON_STORE_PATH": "crons/crons.json",
    "REMOTE_ACCESS_INFO_PATH": "tunnels/reverse-ssh.json",
}.items():
    os.environ.setdefault(env_name, str(_TEST_ROOT / relative_path))

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

    def test_readiness_endpoint_returns_503_for_failed_configuration(self) -> None:
        with (
            patch.object(main_module.settings, "require_auth_state_encryption", True),
            patch.object(main_module.settings, "auth_state_encryption_key", None),
        ):
            response = self.client.get("/readiness")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["overall"], "fail")

    def test_readiness_endpoint_rejects_invalid_mode(self) -> None:
        response = self.client.get("/readiness?mode=invalid")

        self.assertEqual(response.status_code, 400)

    def test_agent_step_returns_success_payload_with_mock_provider(self) -> None:
        step = AsyncMock(
            return_value=AgentStepResult(
                provider="openai",
                model="gpt-4.1-mini",
                goal="Inspect the page",
                workflow_profile="governed",
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
                    "workflow_profile": "governed",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["usage"], {"transport": "fake-provider"})
        self.assertEqual(body["workflow_profile"], "governed")
        self.assertEqual(body["decision"]["action"], "done")
        self.assertEqual(step.await_args.kwargs["session_id"], "session-1")
        self.assertEqual(step.await_args.kwargs["provider_name"], "openai")
        self.assertEqual(step.await_args.kwargs["provider_model"], "gpt-4.1-mini")
        self.assertEqual(step.await_args.kwargs["observation_limit"], 12)
        self.assertEqual(step.await_args.kwargs["workflow_profile"], "governed")

    def test_resume_agent_job_endpoint_returns_queued_job(self) -> None:
        resume_job = AsyncMock(return_value={"id": "job-2", "parent_job_id": "job-1", "status": "queued"})

        with patch.object(main_module.job_queue, "resume_job", resume_job):
            response = self.client.post("/agent/jobs/job-1/resume", json={"max_steps": 4})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["parent_job_id"], "job-1")
        resume_job.assert_awaited_once_with("job-1", max_steps=4)

    def test_discard_agent_job_endpoint_marks_job_discarded(self) -> None:
        discard_job = AsyncMock(return_value={"id": "job-1", "status": "discarded", "resumable": False})

        with patch.object(main_module.job_queue, "discard_job", discard_job):
            response = self.client.post("/agent/jobs/job-1/discard")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "discarded")
        discard_job.assert_awaited_once_with("job-1")

    def test_cancel_agent_job_endpoint_marks_job_cancelled(self) -> None:
        cancel_job = AsyncMock(return_value={"id": "job-1", "status": "cancelled", "resumable": False})

        with patch.object(main_module.job_queue, "cancel_job", cancel_job):
            response = self.client.post("/agent/jobs/job-1/cancel")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "cancelled")
        cancel_job.assert_awaited_once_with("job-1")

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

    def test_session_witness_returns_receipts(self) -> None:
        list_witness = AsyncMock(
            return_value=[
                {
                    "receipt_id": "rcpt-1",
                    "status": "ok",
                    "action": "click",
                    "profile": "normal",
                }
            ]
        )

        with patch.object(main_module.manager, "list_witness_receipts", list_witness):
            response = self.client.get("/sessions/session-1/witness?limit=25")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "session_id": "session-1",
                "count": 1,
                "receipts": [
                    {
                        "receipt_id": "rcpt-1",
                        "status": "ok",
                        "action": "click",
                        "profile": "normal",
                    }
                ],
            },
        )
        list_witness.assert_awaited_once_with("session-1", limit=25)


if __name__ == "__main__":
    unittest.main()

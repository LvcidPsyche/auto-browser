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
        if main_module.rate_limiter is not None:
            main_module.rate_limiter._events.clear()
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

    def test_core_http_endpoints_forward_to_services(self) -> None:
        async_methods = {
            main_module.manager: {
                "ensure_browser": {"ready": True},
                "list_sessions": [{"id": "session-1"}],
                "create_session": {"id": "session-1"},
                "get_session_record": {"id": "session-1", "remote_access": {"active": True}},
                "get_auth_state_info": {"session_id": "session-1"},
                "list_auth_profiles": [{"profile_name": "ops"}],
                "get_auth_profile": {"profile_name": "ops"},
                "observe": {"session": {"id": "session-1"}, "url": "https://example.com"},
                "capture_screenshot": {"screenshot_url": "/artifacts/session-1/s.png"},
                "list_downloads": [{"filename": "report.csv"}],
                "list_tabs": [{"index": 0}],
                "activate_tab": {"index": 0},
                "close_tab": {"closed_index": 1},
                "open_tab": {"index": 1},
                "navigate": {"action": "navigate"},
                "click": {"action": "click"},
                "type": {"action": "type"},
                "press": {"action": "press"},
                "scroll": {"action": "scroll"},
                "execute_decision": {"action": "click"},
                "upload": {"action": "upload"},
                "hover": {"action": "hover"},
                "select_option": {"action": "select_option"},
                "wait": {"action": "wait"},
                "reload": {"action": "reload"},
                "go_back": {"action": "go_back"},
                "go_forward": {"action": "go_forward"},
                "save_storage_state": {"saved_to": "state.json"},
                "save_auth_profile": {"profile_name": "ops"},
                "request_human_takeover": {"takeover_url": "http://127.0.0.1:6080"},
                "list_audit_events": [{"event_type": "browser_action", "timestamp": "2026-01-01T00:00:00Z"}],
                "list_approvals": [{"id": "approval-1"}],
                "get_approval": {"id": "approval-1"},
                "approve": {"id": "approval-1", "status": "approved"},
                "reject": {"id": "approval-1", "status": "rejected"},
                "execute_approval": {"id": "approval-1", "status": "executed"},
                "get_session": SimpleNamespace(page=SimpleNamespace(url="https://example.com"), trace_path=Path("missing.zip")),
                "get_network_log": {"entries": []},
                "fork_session": {"id": "fork-1"},
                "enable_shadow_browse": {"enabled": True},
                "close_session": {"closed": True},
                "import_auth_profile": {"profile_name": "ops"},
            },
            main_module.job_queue: {
                "list_jobs": [{"id": "job-1"}],
                "get_job": {"id": "job-1"},
                "enqueue_step": {"id": "job-step"},
                "enqueue_run": {"id": "job-run"},
            },
            main_module.maintenance: {
                "run_cleanup": {"deleted": 0},
            },
            main_module.cron_service: {
                "list_jobs": [{"id": "cron-1"}],
                "create_job": {"id": "cron-1"},
                "get_job": {"id": "cron-1"},
                "delete_job": True,
                "trigger_via_webhook": {"triggered": True},
            },
        }

        with ExitStack() as stack:
            for target, methods in async_methods.items():
                for name, result in methods.items():
                    stack.enter_context(patch.object(target, name, new=AsyncMock(return_value=result)))
            stack.enter_context(patch.object(main_module.manager, "get_remote_access_info", return_value={"active": False}))
            stack.enter_context(patch.object(main_module.manager, "get_pii_scrubber_status", return_value={"enabled": True}))
            stack.enter_context(patch.object(main_module.share_manager, "create_token", return_value={"token": "share"}))
            stack.enter_context(patch.object(main_module.share_manager, "token_info", return_value={"valid": True, "session_id": "session-1"}))
            stack.enter_context(patch.object(main_module.proxy_store, "list_personas", return_value=[]))
            stack.enter_context(patch.object(main_module.proxy_store, "set_persona", return_value={"name": "us-east"}))
            stack.enter_context(patch.object(main_module.proxy_store, "get_persona", return_value={"name": "us-east"}))
            stack.enter_context(patch.object(main_module.proxy_store, "delete_persona", return_value=True))

            requests = [
                ("get", "/healthz", None),
                ("get", "/readyz", None),
                ("get", "/operator", None),
                ("get", "/agent/jobs", None),
                ("get", "/agent/jobs/job-1", None),
                ("get", "/remote-access", None),
                ("get", "/remote-access?session_id=archived", None),
                ("get", "/audit/events?limit=9999", None),
                ("get", "/approvals", None),
                ("get", "/approvals/approval-1", None),
                ("post", "/approvals/approval-1/approve", {"comment": "ok"}),
                ("post", "/approvals/approval-1/reject", {"comment": "no"}),
                ("post", "/approvals/approval-1/execute", None),
                ("get", "/sessions", None),
                ("post", "/sessions", {"name": "session-1", "start_url": "https://example.com"}),
                ("get", "/sessions/session-1", None),
                ("get", "/sessions/session-1/auth-state", None),
                ("get", "/auth-profiles", None),
                ("get", "/auth-profiles/ops", None),
                ("get", "/sessions/session-1/observe", None),
                ("post", "/sessions/session-1/observe", {"limit": 10, "preset": "fast"}),
                ("post", "/sessions/session-1/screenshot", {"label": "manual"}),
                ("get", "/sessions/session-1/downloads", None),
                ("get", "/sessions/session-1/tabs", None),
                ("post", "/sessions/session-1/tabs/activate", {"index": 0}),
                ("post", "/sessions/session-1/tabs/close", {"index": 1}),
                ("post", "/sessions/session-1/tabs/open", {"url": "https://example.com", "activate": True}),
                ("post", "/sessions/session-1/actions/navigate", {"url": "https://example.com"}),
                ("post", "/sessions/session-1/actions/click", {"selector": "button"}),
                ("post", "/sessions/session-1/actions/type", {"selector": "input", "text": "hello"}),
                ("post", "/sessions/session-1/actions/press", {"key": "Enter"}),
                ("post", "/sessions/session-1/actions/scroll", {"delta_y": 200}),
                (
                    "post",
                    "/sessions/session-1/actions/execute",
                    {"action": {"action": "click", "reason": "click", "selector": "button", "risk_category": "write"}},
                ),
                ("post", "/sessions/session-1/actions/upload", {"selector": "input", "file_path": "report.csv"}),
                ("post", "/sessions/session-1/actions/hover", {"selector": "button"}),
                ("post", "/sessions/session-1/actions/select-option", {"selector": "select", "value": "a"}),
                ("post", "/sessions/session-1/actions/wait", {"wait_ms": 1}),
                ("post", "/sessions/session-1/actions/reload", None),
                ("post", "/sessions/session-1/actions/go-back", None),
                ("post", "/sessions/session-1/actions/go-forward", None),
                ("post", "/sessions/session-1/storage-state", {"path": "state.json"}),
                ("post", "/sessions/session-1/auth-profiles", {"profile_name": "ops"}),
                ("post", "/sessions/session-1/takeover", {"reason": "operator"}),
                ("post", "/sessions/session-1/agent/jobs/step", {"provider": "openai", "goal": "step"}),
                ("post", "/sessions/session-1/agent/jobs/run", {"provider": "openai", "goal": "run"}),
                ("get", "/sessions/session-1/network-log", None),
                ("post", "/sessions/session-1/fork?name=fork&start_url=https://example.com", None),
                ("post", "/sessions/session-1/share", {"ttl_minutes": 5}),
                ("get", "/share/share/observe", None),
                ("get", "/share/share", None),
                ("post", "/sessions/session-1/shadow-browse", None),
                ("get", "/sessions/session-1/witness", None),
                ("get", "/sessions/session-1/trace", None),
                ("get", "/pii-scrubber", None),
                ("get", "/proxy-personas", None),
                ("post", "/proxy-personas", {"name": "us-east", "server": "http://proxy.example.com:8080"}),
                ("get", "/proxy-personas/us-east", None),
                ("delete", "/proxy-personas/us-east", None),
                ("get", "/crons", None),
                ("post", "/crons", {"name": "daily", "goal": "check", "schedule": "0 9 * * *"}),
                ("get", "/crons/cron-1", None),
                ("delete", "/crons/cron-1", None),
                ("post", "/crons/cron-1/trigger", {"webhook_key": "secret"}),
                ("post", "/maintenance/cleanup", None),
                ("get", "/maintenance/status", None),
            ]

            for method, path, payload in requests:
                with self.subTest(path=path):
                    response = getattr(self.client, method)(path, json=payload) if payload is not None else getattr(self.client, method)(path)
                    self.assertLess(response.status_code, 400, response.text)

    def test_replay_export_and_public_error_edges(self) -> None:
        artifact_dir = Path(main_module.settings.artifact_root) / "session-1"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "manual-shot.png").write_bytes(b"png")
        auth_root = Path(main_module.settings.auth_root)
        auth_root.mkdir(parents=True, exist_ok=True)
        archive_path = auth_root / "ops.tar.gz"
        archive_path.write_bytes(b"archive")

        list_events = AsyncMock(
            return_value=[
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "event_type": "browser_action",
                    "operator_id": "alice",
                    "data": {"action": "click"},
                }
            ]
        )
        export_profile = AsyncMock(return_value={"archive_name": archive_path.name})

        with (
            patch.object(main_module.manager, "list_audit_events", list_events),
            patch.object(main_module.manager, "export_auth_profile", export_profile),
            patch.object(main_module.share_manager, "token_info", return_value={"valid": False}),
            patch.object(main_module.proxy_store, "delete_persona", return_value=False),
            patch.object(main_module.cron_service, "delete_job", new=AsyncMock(return_value=False)),
            patch.object(main_module.cron_service, "trigger_via_webhook", new=AsyncMock(side_effect=PermissionError())),
        ):
            replay = self.client.get("/sessions/session-1/replay")
            exported = self.client.get("/auth-profiles/ops/export")
            bad_share = self.client.get("/share/bad/observe")
            missing_proxy = self.client.delete("/proxy-personas/missing")
            missing_cron = self.client.delete("/crons/missing")
            forbidden_cron = self.client.post("/crons/cron-1/trigger", json={"webhook_key": "bad"})

        self.assertEqual(replay.status_code, 200)
        self.assertIn("Session Replay", replay.text)
        self.assertIn("/artifacts/session-1/manual-shot.png", replay.text)
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.headers["content-type"], "application/gzip")
        self.assertEqual(bad_share.status_code, 403)
        self.assertEqual(missing_proxy.status_code, 404)
        self.assertEqual(missing_cron.status_code, 404)
        self.assertEqual(forbidden_cron.status_code, 403)

    def test_http_error_mappings_are_stable(self) -> None:
        cases = [
            (main_module.manager, "ensure_browser", Exception("down"), "get", "/readyz", None, 503),
            (main_module.manager, "get_session_record", KeyError("missing"), "get", "/remote-access?session_id=missing", None, 404),
            (main_module.manager, "approve", PermissionError("conflict"), "post", "/approvals/a/approve", {"comment": "ok"}, 409),
            (main_module.manager, "reject", PermissionError("conflict"), "post", "/approvals/a/reject", {"comment": "no"}, 409),
            (main_module.manager, "execute_approval", PermissionError("conflict"), "post", "/approvals/a/execute", None, 409),
            (main_module.manager, "execute_approval", ValueError("bad"), "post", "/approvals/a/execute", None, 400),
            (main_module.manager, "execute_approval", Exception("boom"), "post", "/approvals/a/execute", None, 500),
            (main_module.manager, "create_session", ValueError("bad"), "post", "/sessions", {"name": "s"}, 400),
            (main_module.manager, "create_session", FileNotFoundError("missing"), "post", "/sessions", {"name": "s"}, 404),
            (main_module.manager, "create_session", PermissionError("no"), "post", "/sessions", {"name": "s"}, 403),
            (main_module.manager, "create_session", RuntimeError("busy"), "post", "/sessions", {"name": "s"}, 409),
            (main_module.manager, "create_session", Exception("boom"), "post", "/sessions", {"name": "s"}, 500),
            (main_module.manager, "get_auth_profile", ValueError("bad"), "get", "/auth-profiles/bad", None, 400),
            (main_module.manager, "observe", KeyError("missing"), "get", "/sessions/s/observe", None, 404),
            (main_module.manager, "observe", Exception("boom"), "post", "/sessions/s/observe", {"limit": 1}, 500),
            (main_module.manager, "activate_tab", ValueError("bad"), "post", "/sessions/s/tabs/activate", {"index": 99}, 400),
            (main_module.manager, "close_tab", ValueError("bad"), "post", "/sessions/s/tabs/close", {"index": 99}, 400),
            (
                main_module.manager,
                "open_tab",
                ValueError("bad"),
                "post",
                "/sessions/s/tabs/open",
                {"url": "https://example.com"},
                400,
            ),
            (
                main_module.manager,
                "navigate",
                PermissionError("no"),
                "post",
                "/sessions/s/actions/navigate",
                {"url": "https://example.com"},
                403,
            ),
            (
                main_module.manager,
                "navigate",
                Exception("boom"),
                "post",
                "/sessions/s/actions/navigate",
                {"url": "https://example.com"},
                500,
            ),
            (main_module.manager, "click", ValueError("bad"), "post", "/sessions/s/actions/click", {"selector": "button"}, 400),
            (
                main_module.manager,
                "click",
                PermissionError("no"),
                "post",
                "/sessions/s/actions/click",
                {"selector": "button"},
                403,
            ),
            (main_module.manager, "click", Exception("boom"), "post", "/sessions/s/actions/click", {"selector": "button"}, 500),
            (
                main_module.manager,
                "type",
                ValueError("bad"),
                "post",
                "/sessions/s/actions/type",
                {"selector": "input", "text": "hi"},
                400,
            ),
            (
                main_module.manager,
                "type",
                PermissionError("no"),
                "post",
                "/sessions/s/actions/type",
                {"selector": "input", "text": "hi"},
                403,
            ),
            (
                main_module.manager,
                "type",
                Exception("boom"),
                "post",
                "/sessions/s/actions/type",
                {"selector": "input", "text": "hi"},
                500,
            ),
            (main_module.manager, "press", PermissionError("no"), "post", "/sessions/s/actions/press", {"key": "Enter"}, 403),
            (main_module.manager, "press", Exception("boom"), "post", "/sessions/s/actions/press", {"key": "Enter"}, 500),
            (main_module.manager, "scroll", PermissionError("no"), "post", "/sessions/s/actions/scroll", {"delta_y": 1}, 403),
            (main_module.manager, "scroll", Exception("boom"), "post", "/sessions/s/actions/scroll", {"delta_y": 1}, 500),
            (
                main_module.manager,
                "execute_decision",
                ValueError("bad"),
                "post",
                "/sessions/s/actions/execute",
                {"action": {"action": "click", "reason": "click", "selector": "button"}},
                400,
            ),
            (
                main_module.manager,
                "execute_decision",
                PermissionError("no"),
                "post",
                "/sessions/s/actions/execute",
                {"action": {"action": "click", "reason": "click", "selector": "button"}},
                403,
            ),
            (
                main_module.manager,
                "execute_decision",
                Exception("boom"),
                "post",
                "/sessions/s/actions/execute",
                {"action": {"action": "click", "reason": "click", "selector": "button"}},
                500,
            ),
            (
                main_module.manager,
                "upload",
                FileNotFoundError("missing"),
                "post",
                "/sessions/s/actions/upload",
                {"selector": "input", "file_path": "missing.txt"},
                404,
            ),
            (
                main_module.manager,
                "upload",
                PermissionError("no"),
                "post",
                "/sessions/s/actions/upload",
                {"selector": "input", "file_path": "missing.txt"},
                403,
            ),
            (
                main_module.manager,
                "upload",
                ValueError("bad"),
                "post",
                "/sessions/s/actions/upload",
                {"selector": "input", "file_path": "missing.txt"},
                400,
            ),
            (
                main_module.manager,
                "upload",
                Exception("boom"),
                "post",
                "/sessions/s/actions/upload",
                {"selector": "input", "file_path": "missing.txt"},
                500,
            ),
            (main_module.manager, "hover", ValueError("bad"), "post", "/sessions/s/actions/hover", {"selector": "button"}, 400),
            (
                main_module.manager,
                "hover",
                PermissionError("no"),
                "post",
                "/sessions/s/actions/hover",
                {"selector": "button"},
                403,
            ),
            (main_module.manager, "hover", Exception("boom"), "post", "/sessions/s/actions/hover", {"selector": "button"}, 500),
            (
                main_module.manager,
                "select_option",
                ValueError("bad"),
                "post",
                "/sessions/s/actions/select-option",
                {"selector": "select", "value": "a"},
                400,
            ),
            (
                main_module.manager,
                "select_option",
                PermissionError("no"),
                "post",
                "/sessions/s/actions/select-option",
                {"selector": "select", "value": "a"},
                403,
            ),
            (
                main_module.manager,
                "select_option",
                Exception("boom"),
                "post",
                "/sessions/s/actions/select-option",
                {"selector": "select", "value": "a"},
                500,
            ),
            (main_module.manager, "wait", KeyError("missing"), "post", "/sessions/s/actions/wait", {"wait_ms": 1}, 404),
            (main_module.manager, "wait", Exception("boom"), "post", "/sessions/s/actions/wait", {"wait_ms": 1}, 500),
            (main_module.manager, "reload", PermissionError("no"), "post", "/sessions/s/actions/reload", None, 403),
            (main_module.manager, "reload", Exception("boom"), "post", "/sessions/s/actions/reload", None, 500),
            (main_module.manager, "go_back", PermissionError("no"), "post", "/sessions/s/actions/go-back", None, 403),
            (main_module.manager, "go_back", Exception("boom"), "post", "/sessions/s/actions/go-back", None, 500),
            (main_module.manager, "go_forward", PermissionError("no"), "post", "/sessions/s/actions/go-forward", None, 403),
            (main_module.manager, "go_forward", Exception("boom"), "post", "/sessions/s/actions/go-forward", None, 500),
            (
                main_module.manager,
                "save_storage_state",
                PermissionError("no"),
                "post",
                "/sessions/s/storage-state",
                {"path": "state.json"},
                403,
            ),
            (
                main_module.manager,
                "save_auth_profile",
                ValueError("bad"),
                "post",
                "/sessions/s/auth-profiles",
                {"profile_name": "ops"},
                400,
            ),
            (
                main_module.manager,
                "save_auth_profile",
                PermissionError("no"),
                "post",
                "/sessions/s/auth-profiles",
                {"profile_name": "ops"},
                403,
            ),
            (main_module.manager, "fork_session", RuntimeError("busy"), "post", "/sessions/s/fork", None, 409),
            (main_module.manager, "enable_shadow_browse", RuntimeError("bad"), "post", "/sessions/s/shadow-browse", None, 400),
        ]

        with patch.object(main_module, "rate_limiter", None):
            for target, method_name, side_effect, http_method, path, payload, expected_status in cases:
                with self.subTest(path=path, method=method_name, status=expected_status):
                    with patch.object(target, method_name, new=AsyncMock(side_effect=side_effect)):
                        call = getattr(self.client, http_method)
                        response = call(path, json=payload) if payload is not None else call(path)
                    self.assertEqual(response.status_code, expected_status, response.text)

            with (
                patch.object(main_module.manager, "get_session", new=AsyncMock(side_effect=KeyError("missing"))),
                patch.object(main_module.share_manager, "token_info", return_value={"valid": True, "session_id": "s"}),
            ):
                self.assertEqual(self.client.post("/sessions/s/share", json={"ttl_minutes": 5}).status_code, 404)
                self.assertEqual(self.client.get("/share/t/observe").status_code, 404)

            with (
                patch.object(main_module.manager, "get_session", new=AsyncMock(return_value=object())),
                patch.object(main_module.share_manager, "create_token", side_effect=ValueError("bad")),
            ):
                self.assertEqual(self.client.post("/sessions/s/share", json={"ttl_minutes": 1}).status_code, 400)

            with (
                patch.object(main_module.share_manager, "token_info", return_value={"valid": True, "session_id": "s"}),
                patch.object(main_module.manager, "observe", new=AsyncMock(side_effect=Exception("boom"))),
            ):
                self.assertEqual(self.client.get("/share/t/observe").status_code, 500)

            with patch.object(main_module.proxy_store, "set_persona", side_effect=ValueError("bad")):
                response = self.client.post("/proxy-personas", json={"name": "bad", "server": "http://proxy:8080"})
                self.assertEqual(response.status_code, 400)

            with patch.object(main_module.proxy_store, "get_persona", side_effect=KeyError("missing")):
                self.assertEqual(self.client.get("/proxy-personas/missing").status_code, 404)

            with patch.object(main_module.cron_service, "create_job", new=AsyncMock(side_effect=ValueError("bad"))):
                response = self.client.post("/crons", json={"name": "bad", "goal": "check", "schedule": "bad"})
                self.assertEqual(response.status_code, 400)

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

    def test_social_session_routes_are_not_shipped(self) -> None:
        endpoints = [
            ("get", "/sessions/session-1/social/posts", None),
            ("post", "/sessions/session-1/social/post", {"text": "hello"}),
            ("post", "/sessions/session-1/social/login", {"platform": "x", "username": "alice", "password": "secret"}),
        ]

        for method, path, body in endpoints:
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, json=body) if body is not None else getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()

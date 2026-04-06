from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pydantic import ValidationError

from app.browser_manager import BrowserManager
from app.config import Settings
from app.cron_service import CronService
from app.models import (
    BrowserActionDecision,
    ClickRequest,
    CreateSessionRequest,
    NavigateRequest,
    SelectOptionRequest,
    TypeRequest,
    UploadRequest,
)
from app.tool_inputs import (
    CdpAttachInput,
    CreateCronJobInput,
    CreateProxyPersonaInput,
    DragDropInput,
    GetNetworkLogInput,
    ObserveInput,
    SetCookiesInput,
)


class RequestValidationTests(unittest.TestCase):
    def test_create_session_rejects_proxy_persona_with_explicit_proxy(self) -> None:
        with self.assertRaises(ValidationError):
            CreateSessionRequest(
                start_url="https://example.com",
                proxy_persona="us-east",
                proxy_server="http://proxy.internal:8080",
            )

    def test_create_session_rejects_proxy_credentials_without_server(self) -> None:
        with self.assertRaises(ValidationError):
            CreateSessionRequest(proxy_username="alice")

    def test_create_session_rejects_unsupported_start_url_scheme(self) -> None:
        with self.assertRaises(ValidationError):
            CreateSessionRequest(start_url="ftp://example.com/file")

    def test_create_session_strips_whitespace(self) -> None:
        req = CreateSessionRequest(
            name="  daily run  ",
            start_url=" https://example.com/path ",
            proxy_persona=" us-east ",
        )

        self.assertEqual(req.name, "daily run")
        self.assertEqual(req.start_url, "https://example.com/path")
        self.assertEqual(req.proxy_persona, "us-east")

    def test_click_request_requires_target(self) -> None:
        with self.assertRaises(ValidationError):
            ClickRequest()

    def test_click_request_requires_full_coordinate_pair(self) -> None:
        with self.assertRaises(ValidationError):
            ClickRequest(x=10)

    def test_type_request_requires_locator(self) -> None:
        with self.assertRaises(ValidationError):
            TypeRequest(text="hello")

    def test_select_option_request_requires_choice(self) -> None:
        with self.assertRaises(ValidationError):
            SelectOptionRequest(selector="#size")

    def test_upload_request_requires_locator(self) -> None:
        with self.assertRaises(ValidationError):
            UploadRequest(file_path="/tmp/report.csv")

    def test_navigate_request_rejects_unsupported_scheme(self) -> None:
        with self.assertRaises(ValidationError):
            NavigateRequest(url="file:///tmp/index.html")

    def test_browser_action_decision_rejects_partial_coordinates(self) -> None:
        with self.assertRaises(ValidationError):
            BrowserActionDecision(action="click", reason="Click it", x=100)

    def test_browser_action_decision_rejects_invalid_navigation_url(self) -> None:
        with self.assertRaises(ValidationError):
            BrowserActionDecision(action="navigate", reason="Open it", url="ftp://example.com")

    def test_observe_input_accepts_preset(self) -> None:
        payload = ObserveInput(session_id="session-1", preset="rich", limit=120)

        self.assertEqual(payload.preset, "rich")
        self.assertEqual(payload.limit, 120)

    def test_get_network_log_uppercases_method(self) -> None:
        payload = GetNetworkLogInput(session_id="session-1", method="post")
        self.assertEqual(payload.method, "POST")

    def test_get_network_log_rejects_non_alpha_method(self) -> None:
        with self.assertRaises(ValidationError):
            GetNetworkLogInput(session_id="session-1", method="P0ST")

    def test_drag_drop_requires_source_and_target(self) -> None:
        with self.assertRaises(ValidationError):
            DragDropInput(session_id="session-1", source_selector="#from")

    def test_drag_drop_requires_complete_coordinate_pairs(self) -> None:
        with self.assertRaises(ValidationError):
            DragDropInput(session_id="session-1", source_x=10, target_x=20, target_y=30)

    def test_set_cookies_requires_name_and_domain_or_url(self) -> None:
        with self.assertRaises(ValidationError):
            SetCookiesInput(session_id="session-1", cookies=[{"value": "abc"}])

    def test_set_cookies_accepts_url_scoped_cookie(self) -> None:
        payload = SetCookiesInput(
            session_id="session-1",
            cookies=[{"name": "sid", "value": "abc", "url": "https://example.com"}],
        )

        self.assertEqual(payload.cookies[0]["name"], "sid")

    def test_cdp_attach_rejects_invalid_scheme(self) -> None:
        with self.assertRaises(ValidationError):
            CdpAttachInput(cdp_url="chrome://version")

    def test_create_proxy_persona_requires_supported_proxy_scheme(self) -> None:
        with self.assertRaises(ValidationError):
            CreateProxyPersonaInput(name="us-east", server="ftp://proxy.internal")

    def test_create_cron_job_requires_schedule_or_webhook(self) -> None:
        with self.assertRaises(ValidationError):
            CreateCronJobInput(name="daily", goal="check inbox")

    def test_create_cron_job_accepts_webhook_only(self) -> None:
        payload = CreateCronJobInput(name="daily", goal="check inbox", webhook_enabled=True)
        self.assertTrue(payload.webhook_enabled)
        self.assertEqual(payload.provider, "openai")

    def test_create_cron_job_rejects_invalid_start_url(self) -> None:
        with self.assertRaises(ValidationError):
            CreateCronJobInput(
                name="daily",
                goal="check inbox",
                webhook_enabled=True,
                start_url="mailto:test@example.com",
            )


class BrowserManagerProxyPersonaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = Settings(
            _env_file=None,
            ARTIFACT_ROOT=str(root / "artifacts"),
            AUTH_ROOT=str(root / "auth"),
            UPLOAD_ROOT=str(root / "uploads"),
            APPROVAL_ROOT=str(root / "approvals"),
            AUDIT_ROOT=str(root / "audit"),
            SESSION_STORE_ROOT=str(root / "sessions"),
            REMOTE_ACCESS_INFO_PATH=str(root / "tunnels/reverse-ssh.json"),
        )

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_create_session_requires_proxy_store_for_persona(self) -> None:
        manager = BrowserManager(self.settings)

        with self.assertRaisesRegex(RuntimeError, "No PROXY_PERSONA_FILE configured"):
            await manager.create_session(proxy_persona="us-east")

    async def test_create_session_resolves_proxy_persona_before_browser_startup(self) -> None:
        proxy_store = Mock()
        proxy_store.resolve_proxy.return_value = {
            "server": "http://proxy.internal:8080",
            "username": "alice",
            "password": "secret",
        }
        manager = BrowserManager(self.settings, proxy_store=proxy_store)

        captured: dict[str, str | None] = {}

        def fake_build_context_kwargs(
            user_agent: str | None,
            proxy_server: str | None,
            proxy_username: str | None,
            proxy_password: str | None,
        ) -> dict[str, object]:
            captured["user_agent"] = user_agent
            captured["proxy_server"] = proxy_server
            captured["proxy_username"] = proxy_username
            captured["proxy_password"] = proxy_password
            raise RuntimeError("stop-after-proxy-resolution")

        manager._build_context_kwargs = fake_build_context_kwargs  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "stop-after-proxy-resolution"):
            await manager.create_session(proxy_persona="us-east")

        proxy_store.resolve_proxy.assert_called_once_with("us-east")
        self.assertEqual(captured["proxy_server"], "http://proxy.internal:8080")
        self.assertEqual(captured["proxy_username"], "alice")
        self.assertEqual(captured["proxy_password"], "secret")


class CronServiceProxyPersonaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.manager = SimpleNamespace(create_session=AsyncMock(return_value={"id": "session-1"}))
        self.job_queue = SimpleNamespace(enqueue_run=AsyncMock(return_value={"id": "job-1"}))
        self.cron = CronService(
            Path(self.tempdir.name) / "crons.json",
            job_queue=self.job_queue,
            manager=self.manager,
        )

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_trigger_job_passes_proxy_persona_to_session_creation(self) -> None:
        job = await self.cron.create_job(
            name="daily",
            goal="check the dashboard",
            webhook_enabled=True,
            proxy_persona="us-east",
            start_url="https://example.com",
            auth_profile="default",
        )

        result = await self.cron.trigger_job(job["id"])

        self.assertTrue(result["triggered"])
        self.manager.create_session.assert_awaited_once_with(
            name=f"cron-{job['id']}",
            start_url="https://example.com",
            auth_profile="default",
            proxy_persona="us-east",
        )
        queued_request = self.job_queue.enqueue_run.await_args.args[1]
        self.assertEqual(queued_request.provider, "openai")


if __name__ == "__main__":
    unittest.main()

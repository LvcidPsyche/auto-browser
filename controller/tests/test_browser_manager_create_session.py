from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager
from app.config import Settings


def _settings(root: Path) -> Settings:
    return Settings(
        ARTIFACT_ROOT=str(root / "artifacts"),
        UPLOAD_ROOT=str(root / "uploads"),
        AUTH_ROOT=str(root / "auth"),
        APPROVAL_ROOT=str(root / "approvals"),
        AUDIT_ROOT=str(root / "audit"),
        WITNESS_ROOT=str(root / "witness"),
        SESSION_STORE_ROOT=str(root / "sessions"),
        REMOTE_ACCESS_INFO_PATH=str(root / "tunnels/reverse-ssh.json"),
        BROWSER_WS_ENDPOINT_FILE=str(root / "missing-ws.txt"),
        WITNESS_ENABLED=False,
        NETWORK_INSPECTOR_ENABLED=False,
        ENABLE_TRACING=False,
        STEALTH_ENABLED=False,
        MAX_SESSIONS=2,
    )


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.on = unittest.mock.Mock()
        self.set_default_timeout = unittest.mock.Mock()
        self.goto = AsyncMock(side_effect=self._goto)
        self.title = AsyncMock(return_value="Fixture Page")

    async def _goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.url = url


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.pages = [page]
        self.on = unittest.mock.Mock()
        self.close = AsyncMock()
        self.new_page = AsyncMock(return_value=page)


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.new_context = AsyncMock(return_value=context)


class BrowserManagerCreateSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = BrowserManager(_settings(self.root))
        self.manager.audit.append = AsyncMock()
        self.manager._persist_session = AsyncMock()
        self.manager._settle = AsyncMock()
        self.manager._maybe_provision_session_tunnel = AsyncMock()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_create_session_builds_context_loads_memory_and_runs_hook(self) -> None:
        page = FakePage()
        context = FakeContext(page)
        browser = FakeBrowser(context)
        self.manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]
        self.manager.memory = SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(to_system_prompt=lambda: "remember this"))
        )

        async def failing_hook(session_id: str, created_page) -> None:
            raise RuntimeError("hook failed")

        self.manager.register_extension_hooks(session_created=failing_hook)

        result = await self.manager.create_session(
            name="fixture",
            start_url="https://example.com",
            memory_profile="checkout",
            request_proxy_server="http://proxy.example.com:8080",
            request_proxy_username="alice",
            request_proxy_password="secret",
            user_agent="AutoBrowserTest/1.0",
            protection_mode="confidential",
            totp_secret="JBSWY3DPEHPK3PXP",
        )

        session = self.manager.sessions[result["id"]]
        self.assertEqual(result["name"], "fixture")
        self.assertEqual(session.metadata["memory_profile"], "checkout")
        self.assertEqual(session.metadata["memory_context"], "remember this")
        self.assertEqual(page.url, "https://example.com")
        context.on.assert_called_once()
        page.on.assert_called()
        browser.new_context.assert_awaited_once()
        context_kwargs = browser.new_context.await_args.kwargs
        self.assertEqual(context_kwargs["user_agent"], "AutoBrowserTest/1.0")
        self.assertEqual(context_kwargs["proxy"]["username"], "alice")
        self.manager._persist_session.assert_awaited()

    async def test_create_session_rejects_conflicting_auth_and_proxy_inputs(self) -> None:
        with self.assertRaises(ValueError):
            await self.manager.create_session(storage_state_path="state.json", auth_profile="ops")
        with self.assertRaises(ValueError):
            await self.manager.create_session(proxy_persona="east", request_proxy_server="http://proxy.example.com:8080")

    async def test_session_limit_message_mentions_shared_browser_mode(self) -> None:
        self.manager.settings.max_sessions = 1
        self.manager.sessions["session-1"] = object()  # type: ignore[assignment]

        with self.assertRaisesRegex(RuntimeError, "one visible desktop"):
            self.manager._check_session_limit()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.provider_registry import ProviderRegistry
from app.utils import UTC


class FakeTabPage:
    def __init__(self, context: "FakeTabContext", url: str, title: str) -> None:
        self.context = context
        self.url = url
        self._title = title
        self.front_calls = 0
        self.closed = False

    async def title(self) -> str:
        return self._title

    async def bring_to_front(self) -> None:
        self.front_calls += 1

    async def close(self) -> None:
        self.closed = True
        self.context.pages.remove(self)


class FakeTabContext:
    def __init__(self) -> None:
        self.pages: list[FakeTabPage] = []

    async def new_page(self) -> "FakeTabPage":
        page = FakeTabPage(self, "about:blank", "New Tab")
        self.pages.append(page)
        return page


class BrowserTabManagementTests(unittest.IsolatedAsyncioTestCase):
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
        )
        self.manager = BrowserManager(self.settings)
        self.manager._persist_session = AsyncMock()  # type: ignore[method-assign]
        self.manager._settle = AsyncMock()  # type: ignore[method-assign]

        context = FakeTabContext()
        first = FakeTabPage(context, "https://example.com", "Home")
        second = FakeTabPage(context, "https://example.com/export", "Export")
        context.pages.extend([first, second])

        artifact_dir = Path(self.settings.artifact_root) / "session-1"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.session = BrowserSession(
            id="session-1",
            name="session-1",
            created_at=datetime.now(UTC),
            context=context,  # type: ignore[arg-type]
            page=first,  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(self.settings.auth_root) / "session-1",
            upload_dir=Path(self.settings.upload_root) / "session-1",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.session.auth_dir.mkdir(parents=True, exist_ok=True)
        self.session.upload_dir.mkdir(parents=True, exist_ok=True)
        self.manager.sessions[self.session.id] = self.session

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_list_tabs_returns_all_pages_and_active_flag(self) -> None:
        tabs = await self.manager.list_tabs(self.session.id)

        self.assertEqual(len(tabs), 2)
        self.assertTrue(tabs[0]["active"])
        self.assertFalse(tabs[1]["active"])
        self.assertEqual(tabs[1]["title"], "Export")

    async def test_activate_tab_switches_active_page(self) -> None:
        result = await self.manager.activate_tab(self.session.id, 1)

        self.assertEqual(self.session.page.url, "https://example.com/export")
        self.assertEqual(result["tabs"][1]["active"], True)
        self.assertEqual(self.session.page.front_calls, 1)

    async def test_close_tab_closes_secondary_page(self) -> None:
        result = await self.manager.close_tab(self.session.id, 1)

        self.assertEqual(len(result["tabs"]), 1)
        self.assertEqual(result["tabs"][0]["url"], "https://example.com")

    async def test_open_tab_adds_new_page_and_activates(self) -> None:
        result = await self.manager.open_tab(self.session.id, url=None, activate=True)

        self.assertEqual(len(result["tabs"]), 3)
        self.assertEqual(result["activated"], True)
        # New page is now the active page
        self.assertEqual(self.session.page.url, "about:blank")
        self.assertEqual(self.session.page.front_calls, 1)

    async def test_open_tab_without_activate_keeps_current_page(self) -> None:
        original_page = self.session.page
        result = await self.manager.open_tab(self.session.id, url=None, activate=False)

        self.assertEqual(len(result["tabs"]), 3)
        self.assertEqual(result["activated"], False)
        # Active page unchanged
        self.assertIs(self.session.page, original_page)


class ProviderRegistryLoginCommandTests(unittest.TestCase):
    def test_cli_modes_expose_login_commands(self) -> None:
        settings = Settings(
            _env_file=None,
            OPENAI_AUTH_MODE="cli",
            OPENAI_CLI_PATH="codex",
            CLAUDE_AUTH_MODE="cli",
            CLAUDE_CLI_PATH="claude",
            GEMINI_AUTH_MODE="cli",
            GEMINI_CLI_PATH="gemini",
            CLI_HOME="",
        )

        infos = {item.provider: item for item in ProviderRegistry(settings).list()}

        self.assertEqual(infos["openai"].login_command, "codex")
        self.assertEqual(infos["claude"].login_command, "claude")
        self.assertEqual(infos["gemini"].login_command, "gemini")


if __name__ == "__main__":
    unittest.main()

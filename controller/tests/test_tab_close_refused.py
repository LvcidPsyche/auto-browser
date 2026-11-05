"""Closing the active tab does not make a tab ALLOWED_HOSTS refuses the active one (NAV-09)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.utils import UTC
from tests.test_tabs_and_providers import FakeTabContext, FakeTabPage

ALLOWED = "https://allowed.example/"
REFUSED = "https://refused.example/"


class CloseTabTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        settings = Settings(
            _env_file=None,
            ALLOWED_HOSTS="allowed.example",
            ARTIFACT_ROOT=str(root / "artifacts"),
            AUTH_ROOT=str(root / "auth"),
            UPLOAD_ROOT=str(root / "uploads"),
            APPROVAL_ROOT=str(root / "approvals"),
            AUDIT_ROOT=str(root / "audit"),
            SESSION_STORE_ROOT=str(root / "sessions"),
        )
        self.manager = BrowserManager(settings)
        self.manager._persist_session = AsyncMock()  # type: ignore[method-assign]
        self.manager._settle = AsyncMock()  # type: ignore[method-assign]
        self.context = FakeTabContext()
        artifact_dir = Path(settings.artifact_root) / "s1"
        artifact_dir.mkdir(parents=True)
        self.session = BrowserSession(
            id="s1",
            name="s1",
            created_at=datetime.now(UTC),
            context=self.context,  # type: ignore[arg-type]
            page=None,  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=Path(settings.auth_root) / "s1",
            upload_dir=Path(settings.upload_root) / "s1",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.manager.sessions["s1"] = self.session

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def tabs(self, *urls: str) -> list[FakeTabPage]:
        pages = [FakeTabPage(self.context, url, f"tab {i}") for i, url in enumerate(urls)]
        self.context.pages.extend(pages)
        self.session.page = pages[0]
        return pages

    async def test_the_nearest_allowed_tab_is_chosen_over_a_refused_neighbour(self) -> None:
        closing, refused, allowed = self.tabs(ALLOWED, REFUSED, ALLOWED)

        await self.manager.close_tab("s1", 0)

        self.assertIs(self.session.page, allowed)
        self.assertTrue(closing.closed)

    async def test_the_old_choice_stands_when_every_other_tab_is_refused(self) -> None:
        closing, first, second = self.tabs(ALLOWED, REFUSED, REFUSED + "x")

        await self.manager.close_tab("s1", 0)

        self.assertIs(self.session.page, first)

    async def test_an_allowed_neighbour_is_still_chosen_as_before(self) -> None:
        first, closing, last = self.tabs(ALLOWED, ALLOWED + "b", ALLOWED + "c")
        self.session.page = closing

        await self.manager.close_tab("s1", 1)

        self.assertIs(self.session.page, last)


if __name__ == "__main__":
    unittest.main()

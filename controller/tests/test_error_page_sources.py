"""Fork and shadow browse from Chrome's error page (the 1.12 follow-ups, FU2).

A navigation the pre-request gate stopped, or one that failed, leaves the session on
``chrome-error://chromewebdata/``. Without a start_url, fork and shadow browse open the source page's URL, and that
one is no page to open: they refuse before exporting anything, saying why and what to pass instead.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.action_errors import ERROR_PAGE_SOURCE_FORK, ERROR_PAGE_SOURCE_SHADOW, ErrorPageSourceError
from app.routes.session_diagnostics import create_session_diagnostics_router
from app.routes.sessions import create_sessions_router
from app.session_ownership import acting_as
from tests.test_auth_state_ownership import ALICE, StateContext, build_manager
from tests.test_request_gate_wiring import START_URL, _browser

ERROR_PAGE = "chrome-error://chromewebdata/"


class ForkFromAnErrorPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager, _ = build_manager(self.root)
        self.manager.settings.max_sessions = 5
        with acting_as(ALICE):
            self.source_id = (await self.manager.create_session(name="alice"))["id"]
        session = self.manager.sessions[self.source_id]
        session.context = StateContext(session.page)
        session.page.url = ERROR_PAGE
        self.export = AsyncMock(wraps=self.manager.auth_state.write_storage_state)
        self.manager.auth_state.write_storage_state = self.export  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_without_a_start_url_it_is_refused_before_anything_is_exported(self) -> None:
        with acting_as(ALICE), self.assertRaises(ErrorPageSourceError) as caught:
            await self.manager.fork_session(self.source_id)

        self.assertEqual(str(caught.exception), ERROR_PAGE_SOURCE_FORK)
        self.assertIn("start_url", str(caught.exception))
        self.export.assert_not_awaited()
        self.assertEqual(sorted((self.root / "auth" / self.source_id).glob("fork_*")), [])

    async def test_a_start_url_the_caller_names_still_forks(self) -> None:
        with acting_as(ALICE):
            forked = await self.manager.fork_session(self.source_id, start_url="https://example.com/next")

        self.assertEqual(forked["forked_from"], self.source_id)
        self.export.assert_awaited_once()


class ShadowFromAnErrorPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from tests.test_request_gate_wiring import GateWiringTests

        self.harness = GateWiringTests()
        await GateWiringTests.asyncSetUp(self.harness)
        self.manager = self.harness.manager
        self.manager.settings.shadow_browse_enabled = True
        self.browser = self.harness.browser
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        self.source_id = (await self.manager.create_session(name="source", start_url=START_URL))["id"]
        self.source = self.manager.sessions[self.source_id]
        self.source.context.storage_state = AsyncMock(return_value={})
        self.headed = _browser()
        self.launch = AsyncMock(return_value=self.headed)
        self.harness._playwright(launch=self.launch)

    async def asyncTearDown(self) -> None:
        await self.harness.asyncTearDown()

    async def test_it_is_refused_before_the_state_is_exported_or_a_browser_launched(self) -> None:
        self.source.page.url = ERROR_PAGE

        with self.assertRaises(ErrorPageSourceError) as caught:
            await self.manager.enable_shadow_browse(self.source_id)

        self.assertEqual(str(caught.exception), ERROR_PAGE_SOURCE_SHADOW)
        self.source.context.storage_state.assert_not_awaited()
        self.launch.assert_not_awaited()

    async def test_an_allowed_page_still_opens_in_the_shadow_browser(self) -> None:
        result = await self.manager.enable_shadow_browse(self.source_id)

        self.assertIn("shadow_session_id", result)
        self.launch.assert_awaited_once()


class RestSurfaceTests(unittest.TestCase):
    def client(self, error: Exception) -> TestClient:
        manager = MagicMock()
        manager.fork_session = AsyncMock(side_effect=error)
        manager.enable_shadow_browse = AsyncMock(side_effect=error)
        app = FastAPI()
        app.include_router(create_sessions_router(manager=manager))
        app.include_router(create_session_diagnostics_router(manager=manager, settings=SimpleNamespace()))
        return TestClient(app)

    def test_fork_answers_400_saying_what_to_pass(self) -> None:
        with self.client(ErrorPageSourceError(ERROR_PAGE_SOURCE_FORK)) as client:
            response = client.post("/sessions/s1/fork")

        self.assertEqual((response.status_code, response.json()["detail"]), (400, ERROR_PAGE_SOURCE_FORK))

    def test_shadow_browse_answers_400_saying_what_to_do(self) -> None:
        with self.client(ErrorPageSourceError(ERROR_PAGE_SOURCE_SHADOW)) as client:
            response = client.post("/sessions/s1/shadow-browse")

        self.assertEqual((response.status_code, response.json()["detail"]), (400, ERROR_PAGE_SOURCE_SHADOW))


if __name__ == "__main__":
    unittest.main()

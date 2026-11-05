"""A fork counts against MAX_SESSIONS, and its 409 says so instead of a bare "Conflict"."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

from app.routes.sessions import create_sessions_router
from app.session_ownership import acting_as
from tests.test_auth_state_ownership import ALICE, StateContext, build_manager


class ForkSessionLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _ = build_manager(Path(self.tmp.name))
        self.manager.settings.max_sessions = 1
        with acting_as(ALICE):
            self.source_id = (await self.manager.create_session(name="alice"))["id"]
        session = self.manager.sessions[self.source_id]
        session.context = StateContext(session.page)
        session.page.url = "https://example.com/"
        app = FastAPI()
        app.include_router(create_sessions_router(manager=self.manager))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_fork_409_names_the_session_limit(self) -> None:
        with acting_as(ALICE):
            response = await self.client.post(f"/sessions/{self.source_id}/fork")

        self.assertEqual(response.status_code, 409)
        self.assertIn("Session limit reached: max_sessions=1", response.json()["detail"])

    async def test_create_409_names_the_session_limit(self) -> None:
        with acting_as(ALICE):
            response = await self.client.post("/sessions", json={})

        self.assertEqual(response.status_code, 409)
        self.assertIn("Session limit reached: max_sessions=1", response.json()["detail"])

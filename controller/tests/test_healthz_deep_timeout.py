from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI

from app.routes import system
from app.routes.system import create_system_router

HANG_SECONDS = 30
TEST_TIMEOUT_SECONDS = 0.2


async def _hang() -> None:
    await asyncio.sleep(HANG_SECONDS)


class DeepHealthTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_hung_ensure_browser_answers_503_within_the_readyz_bound(self) -> None:
        manager = SimpleNamespace(ensure_browser=_hang, sessions={})
        api = FastAPI()
        api.include_router(
            create_system_router(
                settings=SimpleNamespace(environment_name="test"),
                manager=manager,
                metrics=None,
                maintenance=None,
                orchestrator=None,
                version="x",
            )
        )
        started = time.perf_counter()
        with mock.patch.object(system, "READYZ_TIMEOUT_SECONDS", TEST_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t") as client:
                response = await asyncio.wait_for(client.get("/healthz/deep"), timeout=5)
        elapsed = time.perf_counter() - started

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "deep_health_probe_timed_out")
        self.assertLess(elapsed, TEST_TIMEOUT_SECONDS + 2)

    async def test_a_timed_out_probe_leaves_ensure_browser_running_to_completion(self) -> None:
        # Cancelling ensure_browser mid-connect leaked a half-acquired connection and its gate.
        release = asyncio.Event()
        outcome: list[str] = []

        async def slow_connect() -> object:
            try:
                await release.wait()
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise
            outcome.append("connected")
            return SimpleNamespace(new_context=mock.AsyncMock(side_effect=RuntimeError("probe stops here")))

        manager = SimpleNamespace(ensure_browser=slow_connect, sessions={})
        api = FastAPI()
        api.include_router(
            create_system_router(
                settings=SimpleNamespace(environment_name="test"),
                manager=manager,
                metrics=None,
                maintenance=None,
                orchestrator=None,
                version="x",
            )
        )
        with mock.patch.object(system, "READYZ_TIMEOUT_SECONDS", TEST_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t") as client:
                response = await asyncio.wait_for(client.get("/healthz/deep"), timeout=5)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(outcome, [])

        release.set()
        await asyncio.sleep(0.05)

        self.assertEqual(outcome, ["connected"])


if __name__ == "__main__":
    unittest.main()

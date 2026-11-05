from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
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

    async def test_timed_out_probes_share_one_pending_connect(self) -> None:
        # Each timed-out probe left its own connect queued behind the browser lock, one more per poll in an outage.
        release = asyncio.Event()
        connects: list[int] = []

        async def ensure_browser() -> object:
            connects.append(1)
            await release.wait()  # as every connect queues on the browser lock until the browser answers
            return _rendering_browser()

        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        with mock.patch.object(system, "READYZ_TIMEOUT_SECONDS", TEST_TIMEOUT_SECONDS):
            async with _client(manager) as client:
                for _ in range(3):
                    response = await asyncio.wait_for(client.get("/healthz/deep"), timeout=5)
                    self.assertEqual(response.json()["error"], "deep_health_probe_timed_out")

                self.assertEqual(len(connects), 1)
                pending = system._PENDING_CONNECTS.get(id(manager))  # this manager's entry, not the whole dict
                self.assertIsNotNone(pending)
                release.set()
                await asyncio.wait_for(asyncio.shield(pending), timeout=5)
                await asyncio.sleep(0)  # its done callbacks run
                self.assertIsNone(system._PENDING_CONNECTS.get(id(manager)))

                response = await asyncio.wait_for(client.get("/healthz/deep"), timeout=5)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(len(connects), 2)  # a finished connect is not reused: the next probe asks the manager


class DeepHealthAnswerTests(unittest.IsolatedAsyncioTestCase):
    # The fixture is patched, so both branches run wherever the suite does (the controller image has no evals/).
    async def _answer(self, fixture: Path) -> dict:
        async def ensure_browser() -> object:
            return _rendering_browser()

        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        with mock.patch.object(system, "_DEEP_HEALTH_FIXTURE", fixture):
            async with _client(manager) as client:
                response = await asyncio.wait_for(client.get("/healthz/deep"), timeout=5)
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_a_readable_fixture_is_named_fixture_not_by_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "deep_health.html"
            fixture.write_text(system._DEEP_HEALTH_FALLBACK, encoding="utf-8")

            body = await self._answer(fixture)

        self.assertEqual(body["checks"][0]["details"]["source"], "fixture")
        # In the parsed values: a search of the raw text misses a path whose backslashes JSON escaped.
        self.assertFalse([text for text in _strings(body) if tmp in text or fixture.name in text])

    async def test_a_missing_fixture_is_named_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = await self._answer(Path(tmp) / "missing.html")

        self.assertEqual(body["checks"][0]["details"]["source"], "embedded")
        self.assertFalse([text for text in _strings(body) if tmp in text])


class ReadyzConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_readiness_probes_share_one_connect_and_never_cancel_it(self) -> None:
        # /readyz cancelled ensure_browser on its timeout, leaking a half-acquired connection and its gate, one per
        # probe through an outage; it is auth- and rate-limit-exempt, so any caller could cause them.
        release = asyncio.Event()
        connects: list[int] = []
        outcome: list[str] = []

        async def ensure_browser() -> object:
            connects.append(1)
            try:
                await release.wait()
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise
            outcome.append("connected")
            return object()

        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        with mock.patch.object(system, "READYZ_TIMEOUT_SECONDS", TEST_TIMEOUT_SECONDS):
            async with _client(manager) as client:
                for _ in range(3):
                    response = await asyncio.wait_for(client.get("/readyz"), timeout=5)
                    self.assertEqual(response.status_code, 503)
                self.assertEqual((len(connects), outcome), (1, []))

                release.set()
                for _ in range(50):
                    if outcome:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(outcome, ["connected"])  # the connect the probes gave up on finished
                response = await asyncio.wait_for(client.get("/readyz"), timeout=5)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(connects), 2)  # a finished connect is not reused: the next probe asks the manager


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return [value] if isinstance(value, str) else []


def _rendering_browser() -> object:
    locator = SimpleNamespace(
        get_attribute=mock.AsyncMock(return_value="ready"), inner_text=mock.AsyncMock(return_value="Deep health ready")
    )
    page = SimpleNamespace(set_content=mock.AsyncMock(), locator=mock.Mock(return_value=locator))
    context = SimpleNamespace(new_page=mock.AsyncMock(return_value=page), close=mock.AsyncMock())
    return SimpleNamespace(new_context=mock.AsyncMock(return_value=context))


def _client(manager: object) -> httpx.AsyncClient:
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
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t")


if __name__ == "__main__":
    unittest.main()

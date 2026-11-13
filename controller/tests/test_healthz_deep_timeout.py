from __future__ import annotations

import asyncio
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main_module
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


class DeepHealthConnectedBrowserTests(unittest.IsolatedAsyncioTestCase):
    """A browser that connects but then never answers must not hold the probe open either."""

    @staticmethod
    def _never(release: asyncio.Event) -> mock.AsyncMock:
        async def wait(*_args: object, **_kwargs: object) -> None:
            await release.wait()

        return mock.AsyncMock(side_effect=wait)

    async def _probe(
        self, browser: object, probe_seconds: float = TEST_TIMEOUT_SECONDS, close_seconds: float = TEST_TIMEOUT_SECONDS
    ) -> tuple[httpx.Response, float]:
        async def ensure_browser() -> object:
            return browser

        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        started = time.perf_counter()
        with (
            mock.patch.object(system, "_DEEP_HEALTH_PROBE_SECONDS", probe_seconds),
            mock.patch.object(system, "_DEEP_HEALTH_CLOSE_SECONDS", close_seconds),
        ):
            async with _client(manager) as client:
                response = await asyncio.wait_for(client.get("/healthz/deep"), timeout=5)
        return response, time.perf_counter() - started

    async def test_a_new_context_that_never_returns_answers_503_timed_out(self) -> None:
        release = asyncio.Event()
        browser = SimpleNamespace(new_context=self._never(release))

        response, elapsed = await self._probe(browser)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "deep_health_probe_timed_out")
        self.assertLess(elapsed, TEST_TIMEOUT_SECONDS + 2)

    async def test_a_new_page_that_never_returns_answers_503_and_closes_the_context(self) -> None:
        release = asyncio.Event()
        context = SimpleNamespace(new_page=self._never(release), close=mock.AsyncMock())
        browser = SimpleNamespace(new_context=mock.AsyncMock(return_value=context))

        # Room to reach new_page: on a loaded runner the whole of 0.2 s can go before the context exists (CI saw a
        # 0.27 s stall here), and then there is no context to close and the assertion below proves nothing.
        response, elapsed = await self._probe(browser, probe_seconds=1.0)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "deep_health_probe_timed_out")
        self.assertLess(elapsed, TEST_TIMEOUT_SECONDS + 2)
        context.close.assert_awaited_once()

    async def test_a_close_that_never_returns_is_left_to_finish_and_logged(self) -> None:
        release = asyncio.Event()
        browser = _rendering_browser()
        context = await browser.new_context()
        context.close = self._never(release)

        with self.assertLogs(system.logger, level="WARNING") as logged:
            response, elapsed = await self._probe(browser, probe_seconds=2.0)  # room for the close grace inside it
        release.set()

        self.assertEqual(response.status_code, 200)  # the page rendered; only the cleanup hung
        self.assertEqual(response.json()["status"], "ok")
        self.assertLess(elapsed, TEST_TIMEOUT_SECONDS + 2)
        self.assertTrue([line for line in logged.output if "closing its browser context" in line])

    async def test_a_context_created_after_the_probe_gave_up_is_closed(self) -> None:
        # Cancelling `await new_context()` leaves Chromium to create the context anyway; nothing else holds it.
        release = asyncio.Event()
        context = SimpleNamespace(close=mock.AsyncMock())

        async def new_context(*_args: object, **_kwargs: object) -> object:
            await release.wait()
            return context

        browser = SimpleNamespace(new_context=new_context)

        with self.assertLogs(system.logger, level="DEBUG") as logged:
            response, _elapsed = await self._probe(browser)
            context.close.assert_not_awaited()
            release.set()
            for _ in range(50):
                if context.close.await_count:
                    break
                await asyncio.sleep(0.01)

        self.assertEqual(response.json()["error"], "deep_health_probe_timed_out")
        context.close.assert_awaited_once()
        self.assertTrue([line for line in logged.output if "created after the probe gave up" in line])

    async def test_a_context_created_in_the_turn_the_probe_is_cancelled_is_closed(self) -> None:
        # The cancel can land when Chromium has just answered: the creation is done, nobody was handed the context,
        # and a callback that is added only to an unfinished creation never closes it. No clock: the creation itself
        # cancels its waiter as it returns.
        release = asyncio.Event()
        context = SimpleNamespace(close=mock.AsyncMock())
        waiters: list[asyncio.Future[object]] = []

        async def new_context(*_args: object, **_kwargs: object) -> object:
            await release.wait()
            waiters[0].cancel()
            return context

        waiters.append(asyncio.ensure_future(system._new_context(SimpleNamespace(new_context=new_context))))
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(waiters[0], timeout=5)
        await _closing_drained()

        context.close.assert_awaited_once()

    async def test_a_creation_that_failed_in_the_turn_the_probe_is_cancelled_is_read_and_closes_nothing(self) -> None:
        # Its error is read, so the loop is not left to report an exception nobody retrieved; there is nothing to close.
        release = asyncio.Event()
        waiters: list[asyncio.Future[object]] = []
        reported: list[dict[str, object]] = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: reported.append(context))

        async def new_context(*_args: object, **_kwargs: object) -> object:
            await release.wait()
            waiters[0].cancel()
            raise RuntimeError("the browser went away")

        waiters.append(asyncio.ensure_future(system._new_context(SimpleNamespace(new_context=new_context))))
        release.set()
        with self.assertNoLogs(system.logger, level="WARNING"):
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(waiters[0], timeout=5)
            await _closing_drained()

        self.assertEqual(reported, [])

    async def test_a_close_left_to_finish_is_held_until_it_ends(self) -> None:
        release = asyncio.Event()
        browser = _rendering_browser()
        context = await browser.new_context()
        context.close = self._never(release)

        await self._probe(browser, probe_seconds=2.0)

        self.assertEqual(len(system._CLOSING), 1)
        release.set()
        await asyncio.sleep(0.05)
        self.assertEqual(len(system._CLOSING), 0)

    async def test_a_total_that_expires_during_the_close_grace_still_logs_and_lets_the_close_finish(self) -> None:
        release = asyncio.Event()
        browser = _rendering_browser()
        context = await browser.new_context()
        context.close = self._never(release)

        with self.assertLogs(system.logger, level="WARNING") as logged:
            response, _elapsed = await self._probe(browser, probe_seconds=TEST_TIMEOUT_SECONDS, close_seconds=5.0)

        self.assertEqual(response.json()["error"], "deep_health_probe_timed_out")
        self.assertTrue([line for line in logged.output if "left to finish" in line])
        (closing,) = system._CLOSING
        self.assertFalse(closing.done())  # not cancelled with the probe
        release.set()
        await asyncio.sleep(0.05)
        self.assertEqual(len(system._CLOSING), 0)


class DeepHealthAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_probe_renders_its_embedded_page_and_reads_no_file(self) -> None:
        # The probe read evals/fixtures/deep_health.html, which the controller image does not hold, and warned of
        # it at every probe before it used the page it carries.
        async def ensure_browser() -> object:
            return browser

        browser = _rendering_browser()
        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        # As in the image, where no such file exists.
        with mock.patch.object(Path, "read_text", side_effect=FileNotFoundError("no fixture")) as read_text:
            with self.assertNoLogs(system.logger, level="WARNING"):
                answer = await asyncio.wait_for(system.run_deep_health_probe(manager), timeout=5)

        read_text.assert_not_called()
        self.assertEqual(answer["checks"][0]["details"]["source"], "embedded")
        page = (await (await browser.new_context()).new_page()).set_content
        self.assertIn('data-ab-deep-health="ready"', page.await_args.args[0])


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
        with (
            mock.patch.object(system, "READYZ_TIMEOUT_SECONDS", TEST_TIMEOUT_SECONDS),
            # Every call probes here: the shared connect is what is under test, not the answer cache.
            mock.patch.object(system, "READYZ_ANSWER_CACHE_SECONDS", 0),
        ):
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


class _Clock:
    """Stands in for app.routes.system's `time`. Patching time.monotonic itself would stop the event loop's clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def patch(self) -> mock._patch:
        return mock.patch.object(
            system, "time", new=SimpleNamespace(monotonic=self.monotonic, perf_counter=time.perf_counter)
        )


class ReadyzAnswerCacheTests(unittest.IsolatedAsyncioTestCase):
    """/readyz is exempt from the rate limit and the token, so a flood must not cost a probe per request."""

    async def _flood(self, client: httpx.AsyncClient, count: int) -> list[int]:
        return [(await client.get("/readyz")).status_code for _ in range(count)]

    async def test_a_flood_on_a_cold_cache_costs_one_probe(self) -> None:
        connects: list[int] = []

        async def ensure_browser() -> object:
            connects.append(1)
            return object()

        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        async with _client(manager) as client:
            statuses = await self._flood(client, 5)

        self.assertEqual((statuses, len(connects)), ([200] * 5, 1))

    async def test_concurrent_misses_share_one_probe(self) -> None:
        release = asyncio.Event()

        async def ensure_browser() -> object:
            await release.wait()
            return object()

        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        # _wait_for_browser, not ensure_browser: the shared connect would hide a second probe, and a probe that waited
        # on the connect is still a second wait the window was to spare.
        with mock.patch.object(system, "_wait_for_browser", wraps=system._wait_for_browser) as waited:
            async with _client(manager) as client:
                waiters = [asyncio.ensure_future(client.get("/readyz")) for _ in range(5)]
                await asyncio.sleep(0.05)  # every miss has found the probe in flight
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*waiters), timeout=5)

        self.assertEqual([response.status_code for response in responses], [200] * 5)
        self.assertEqual(waited.await_count, 1)

    async def test_a_flood_against_a_failing_browser_costs_one_probe_per_window(self) -> None:
        connects: list[int] = []

        async def ensure_browser() -> object:
            connects.append(1)
            raise ConnectionError("browser down")

        clock = _Clock()
        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        with clock.patch():
            async with _client(manager) as client:
                self.assertEqual(await self._flood(client, 4), [503] * 4)
                self.assertEqual(len(connects), 1)
                clock.advance(system.READYZ_ANSWER_CACHE_SECONDS - 0.01)
                self.assertEqual(await self._flood(client, 3), [503] * 3)
                self.assertEqual(len(connects), 1)  # a failing answer holds as long as a passing one
                clock.advance(0.02)
                self.assertEqual(await self._flood(client, 3), [503] * 3)
                self.assertEqual(len(connects), 2)

    async def test_a_passing_answer_stands_for_the_window_and_no_longer(self) -> None:
        connects: list[int] = []

        async def ensure_browser() -> object:
            connects.append(1)
            return object()

        clock = _Clock()
        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        with clock.patch():
            async with _client(manager) as client:
                self.assertEqual(await self._flood(client, 3), [200] * 3)
                clock.advance(system.READYZ_ANSWER_CACHE_SECONDS + 0.01)
                self.assertEqual(await self._flood(client, 1), [200])

        self.assertEqual(len(connects), 2)

    async def test_the_window_runs_from_when_the_answer_finished_not_when_the_probe_began(self) -> None:
        connects: list[int] = []
        clock = _Clock()

        async def ensure_browser() -> object:
            connects.append(1)
            clock.advance(60)  # a probe that took a minute
            return object()

        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        with clock.patch():
            async with _client(manager) as client:
                await client.get("/readyz")
                clock.advance(system.READYZ_ANSWER_CACHE_SECONDS - 0.01)
                await client.get("/readyz")
                self.assertEqual(len(connects), 1)
                clock.advance(0.02)
                await client.get("/readyz")

        self.assertEqual(len(connects), 2)

    async def test_a_cancelled_caller_does_not_cancel_the_probe_the_others_wait_on(self) -> None:
        release = asyncio.Event()
        connects: list[int] = []

        async def ensure_browser() -> object:
            connects.append(1)
            await release.wait()
            return object()

        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        async with _client(manager) as client:
            first = asyncio.ensure_future(client.get("/readyz"))
            second = asyncio.ensure_future(client.get("/readyz"))
            await asyncio.sleep(0.05)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

            release.set()
            response = await asyncio.wait_for(second, timeout=5)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(connects), 1)

            # And the answer it gave is the cached one: the next caller costs no probe.
            self.assertEqual((await client.get("/readyz")).status_code, 200)

        self.assertEqual(len(connects), 1)

    async def test_each_app_has_its_own_answer(self) -> None:
        connects: list[int] = []

        async def ensure_browser() -> object:
            connects.append(1)
            return object()

        manager = SimpleNamespace(ensure_browser=ensure_browser, sessions={})
        async with _client(manager) as one, _client(manager) as other:
            await one.get("/readyz")
            await other.get("/readyz")

        self.assertEqual(len(connects), 2)


class ReadyzRestartTests(unittest.TestCase):
    def test_a_restart_of_the_real_app_does_not_inherit_its_answer(self) -> None:
        ensure_browser = mock.AsyncMock(return_value=object())
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[])
                )
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
                stack.enter_context(mock.patch.object(service, method_name, new=mock.AsyncMock()))
            stack.enter_context(mock.patch.object(main_module.manager, "ensure_browser", new=ensure_browser))
            with TestClient(main_module.app) as client:
                self.assertEqual(client.get("/readyz").status_code, 200)
            with TestClient(main_module.app) as client:
                self.assertEqual(client.get("/readyz").status_code, 200)

        self.assertEqual(ensure_browser.await_count, 2)


async def _closing_drained() -> None:
    """Every close and creation the probe left behind has ended, and its done callbacks have run."""
    for _ in range(100):
        if not system._CLOSING:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0)


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

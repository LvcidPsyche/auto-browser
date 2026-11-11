"""With its key proved, a cron webhook trigger answers no 403, 400 or 500 that is not the key's, the body's or a
fault of the server's.

POST /crons/{id}/trigger answered the run's session as the session answers its own caller: 403 with the refused
host for a start_url the pre-request gate stopped, 400 for one that did not load. And a stored job holding, where
a run reads text or a number, a value that is neither (a store edited by hand) was an unhandled 500.

The real app (app.main), its router, its cron service and its BrowserManager: the run's session is created by the
real create_session over the suite's fake browser, whose page fails its load as Playwright reports each failure.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import app.main as main_module
from app.action_errors import BROWSER_UNREACHABLE_DETAIL, BrowserUnreachableError
from app.browser.request_gate import GateBlock
from app.browser_manager import BrowserManager
from app.cron_service import CronService
from app.models import McpToolCallRequest
from app.routes.operations import CRON_TRIGGER_RUN_REFUSED_DETAIL, CRON_TRIGGER_TRY_AGAIN_DETAIL
from app.tool_gateway.gateway import McpToolGateway
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

START_URL = "https://shop.example/start"
REFUSED_KEY = (403, {"detail": "Not permitted"})
RUN_REFUSED = (409, {"detail": CRON_TRIGGER_RUN_REFUSED_DETAIL})
TRY_AGAIN = (503, {"detail": CRON_TRIGGER_TRY_AGAIN_DETAIL})
# What the session's own answers held, none of it for a caller who holds a webhook key and not the operator's token.
OF_THE_SESSIONS_ANSWER = ("example", "ERR_", "ALLOWED_HOSTS", "PRE_REQUEST_BLOCKING", "browser_action", "retryable")
STOPPED_BY_THE_GATE = "Page.goto: net::ERR_BLOCKED_BY_CLIENT at https://shop.example/start"
REFUSED_HOST_BLOCK = GateBlock(
    context_id=None,
    target_id="t1",
    origin="https://other.example",
    reason="off_list",
    explanation=(
        "A navigation to https://other.example, a host ALLOWED_HOSTS does not allow, was stopped before it was sent."
    ),
)


class CronTriggerAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # unittest runs the loop in asyncio's debug mode, which records the stack at every task and future made: half
        # a second for each request through the app's middleware, and these tests make some seventy.
        asyncio.get_running_loop().set_debug(False)
        self.cron, self.manager = main_module.cron_service, main_module.manager
        self.store = Path(main_module.settings.cron_store_path)
        self.job_ids: list[str] = []
        self.addCleanup(self._forget_jobs)
        for name, value in (("allowed_hosts", "shop.example"), ("network_inspector_enabled", False)):
            patcher = patch.object(main_module.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sessions_before = set(self.manager.sessions)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app, raise_app_exceptions=False),
            base_url="http://testserver",
        )
        self.addAsyncCleanup(self.client.aclose)

    def _stored(self) -> dict[str, Any]:
        return json.loads(self.store.read_text(encoding="utf-8"))

    def _forget_jobs(self) -> None:
        jobs = self._stored()
        for job_id in self.job_ids:
            jobs.pop(job_id, None)
        self.store.write_text(json.dumps(jobs), encoding="utf-8")

    async def _job(self, **fields: Any) -> tuple[str, str]:
        created = await self.cron.create_job(name="t", goal="g", webhook_enabled=True, **fields)
        self.job_ids.append(created["id"])
        return created["id"], created["webhook_key"]

    async def _answers(self, job_id: str, key: str) -> tuple[httpx.Response, httpx.Response, list[str]]:
        """The trigger's answers to the job's key and to a wrong one, and the first line of each WARNING the route
        wrote for the two (the wrong key always writes one, its own)."""
        if main_module.rate_limiter is not None:
            main_module.rate_limiter._events.clear()
        with self.assertLogs("app.routes.operations", level="WARNING") as logged:
            right = await self.client.post(f"/crons/{job_id}/trigger", json={"webhook_key": key})
            wrong = await self.client.post(f"/crons/{job_id}/trigger", json={"webhook_key": "not-the-key"})
        return right, wrong, [record.getMessage().splitlines()[0] for record in logged.records]

    def assert_only_the_fixed_answer(self, right: httpx.Response, wrong: httpx.Response, expected: Any) -> None:
        self.assertEqual(right.status_code, expected[0], right.text)
        self.assertEqual(right.json(), expected[1])
        for text in OF_THE_SESSIONS_ANSWER:
            self.assertNotIn(text, right.text)
        # Only once the key is proved: a caller without it learns nothing of the job or of its run.
        self.assertEqual((wrong.status_code, wrong.json()), REFUSED_KEY)
        # The run's session is gone, and the job is free to fire again.
        self.assertEqual(set(self.manager.sessions), self.sessions_before)
        self.assertEqual(self.cron._active_runs, {})

    async def _a_start_url_that_fails(
        self, failure: Exception | None, *, blocks: list[GateBlock] | None = None, gate_attaches: bool = True
    ) -> tuple[httpx.Response, httpx.Response, list[str], str]:
        """Trigger a job whose start_url the fake browser's page fails to load with ``failure``."""
        page = FakePage()
        page.goto = AsyncMock(side_effect=failure)
        browser = FakeBrowser(FakeContext(page))
        if not gate_attaches:
            browser.new_browser_cdp_session = AsyncMock(side_effect=PlaywrightError("Target closed"))
        self.addAsyncCleanup(self.manager.request_gates.release, browser)
        job_id, key = await self._job(start_url=START_URL)
        acquired = patch.object(self.manager, "_acquire_session_browser", AsyncMock(return_value=(browser, None)))
        # The suite's fake CDP session reports no paused request, so a block the gate records is handed to its lookup.
        looked_up = patch.object(
            self.manager.request_gates,
            "blocks_on_page",
            AsyncMock(return_value=blocks) if blocks is not None else self.manager.request_gates.blocks_on_page,
        )
        with acquired, looked_up:
            right, wrong, lines = await self._answers(job_id, key)
        return right, wrong, lines, job_id

    async def test_a_start_url_the_gate_stops_answers_the_fixed_409_and_names_no_host(self) -> None:
        # The session's own 403 (browser_action_blocked), which names the refused host: a caller is told to read a
        # 403 of this route as a refused key, and the fixed 409 was made to name no host.
        for cause, blocks in (
            ("no block is tied to the page", None),
            ("the block names the host", [REFUSED_HOST_BLOCK]),
        ):
            with self.subTest(cause):
                right, wrong, lines, job_id = await self._a_start_url_that_fails(
                    PlaywrightError(STOPPED_BY_THE_GATE), blocks=blocks
                )

                self.assert_only_the_fixed_answer(right, wrong, RUN_REFUSED)
                self.assertEqual(len(lines), 2, lines)
                self.assertIn(
                    f"cron trigger refused for job '{job_id}': answered 409 for BrowserActionError: ", lines[0]
                )
                self.assertIn("other.example" if blocks else "the pre-request gate", lines[0])  # the log has the cause

    async def test_a_start_url_that_does_not_load_answers_503_so_that_a_webhooks_sender_tries_again(self) -> None:
        # The session's own 400 (browser_action_failed, retryable): this route's 400 is the request's own body, and a
        # sender tries a 5xx again and not a 4xx. The page may answer next time.
        for cause, failure in (
            (
                "the name does not resolve",
                PlaywrightError("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://shop.example/start"),
            ),
            ("the load times out", PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded.")),
        ):
            with self.subTest(cause):
                right, wrong, lines, job_id = await self._a_start_url_that_fails(failure)

                self.assert_only_the_fixed_answer(right, wrong, TRY_AGAIN)
                self.assertEqual(len(lines), 2, lines)
                self.assertIn(
                    f"cron trigger failed for job '{job_id}': answered 503 for BrowserActionError: ", lines[0]
                )
                self.assertIn("did not load", lines[0])

    async def test_a_browser_whose_gate_does_not_attach_answers_that_503_and_names_no_setting(self) -> None:
        # The session's own 503 (pre_request_gate_unavailable), whose text names PRE_REQUEST_BLOCKING.
        right, wrong, lines, job_id = await self._a_start_url_that_fails(None, gate_attaches=False)

        self.assert_only_the_fixed_answer(right, wrong, TRY_AGAIN)
        self.assertEqual(len(lines), 2, lines)
        self.assertIn(f"cron trigger failed for job '{job_id}': answered 503 for RequestGateUnavailable: ", lines[0])

    async def _a_stored_job_holding(self, field: str, value: Any) -> tuple[httpx.Response, httpx.Response, list[str]]:
        """Trigger a job whose stored record holds ``value`` in ``field``, on a controller whose browser cannot be
        reached: an answer of 503 with that text is a job that got as far as its session's browser."""
        job_id, key = await self._job()
        jobs = self._stored()
        jobs[job_id][field] = value
        self.store.write_text(json.dumps(jobs), encoding="utf-8")
        unreachable = AsyncMock(side_effect=BrowserUnreachableError("could not connect to the browser"))
        with patch.object(self.manager, "_acquire_session_browser", unreachable):
            right, wrong, lines = await self._answers(job_id, key)
        self.reached_the_browser = unreachable.await_count == 1
        return right, wrong, [line.replace(job_id, "JOB") for line in lines]

    async def test_a_stored_job_holding_what_a_run_cannot_take_answers_the_fixed_409(self) -> None:
        # A store edited by hand. Each raised what the first code to use the value raised (a TypeError, an
        # AttributeError), which nothing mapped: an unhandled 500, with the key proved.
        for field, value in (
            ("max_steps", "abc"),
            ("max_steps", [1]),
            ("max_steps", {"a": 1}),
            ("start_url", 5),
            ("start_url", True),
            ("start_url", ["https://shop.example/"]),
            ("auth_profile", 5),
            ("auth_profile", ["login"]),
            ("proxy_persona", ["us-east"]),
            ("proxy_persona", {"a": 1}),
            ("id", ["x"]),
            ("id", {"a": 1}),
        ):
            with self.subTest(field=field, value=value):
                right, wrong, lines = await self._a_stored_job_holding(field, value)

                self.assert_only_the_fixed_answer(right, wrong, RUN_REFUSED)
                self.assertFalse(self.reached_the_browser)
                self.assertEqual(len(lines), 2, lines)
                # The field, and nothing of what it holds.
                self.assertRegex(
                    lines[0],
                    rf"^cron trigger failed for job 'JOB': answered 409 for ValueError: \"the stored job's {field} "
                    r"is not a (string|number)\"$",
                )

    async def test_a_stored_job_holding_an_empty_value_there_still_runs_as_one_that_holds_none(self) -> None:
        # null, false, 0 and an empty list were no value to a run before the check, and are none to it.
        for field in ("max_steps", "start_url", "auth_profile", "proxy_persona"):
            for value in (None, False, 0, [], ""):
                with self.subTest(field=field, value=value):
                    right, wrong, _lines = await self._a_stored_job_holding(field, value)

                    self.assertEqual((right.status_code, right.json()), (503, {"detail": BROWSER_UNREACHABLE_DETAIL}))
                    self.assertTrue(self.reached_the_browser)
                    self.assertEqual((wrong.status_code, wrong.json()), REFUSED_KEY)


class CronRunRefusalAtTheOtherDoorsTests(unittest.IsolatedAsyncioTestCase):
    """The refusal is the service's own, so the tool that takes no key and a scheduled fire meet it as well."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.manager = BrowserManager(_settings(root))
        await self.manager.audit.startup()
        self.manager.create_session = AsyncMock(return_value={"id": "session-1"})  # type: ignore[method-assign]
        queue = MagicMock()
        queue.enqueue_run = AsyncMock(return_value={"id": "agent-job-1"})
        self.cron = CronService(root / "crons.json", job_queue=queue, manager=self.manager)
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=SimpleNamespace(),
            job_queue=queue,
            tool_profile="full",
            tool_packs="cron",
            cron_service=self.cron,
        )
        self.job_id = (await self.cron.create_job(name="n", goal="g"))["id"]
        jobs = json.loads((root / "crons.json").read_text(encoding="utf-8"))
        jobs[self.job_id]["max_steps"] = "abc"
        (root / "crons.json").write_text(json.dumps(jobs), encoding="utf-8")

    async def test_the_tool_that_takes_no_key_answers_an_error_that_names_the_field(self) -> None:
        # It answered "Tool execution failed", the gateway's answer for an error it does not know.
        call = McpToolCallRequest(name="browser.trigger_cron_job", arguments={"job_id": self.job_id})

        response = await self.gateway.call_tool(call)

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent, {"error": "the stored job's max_steps is not a number"})
        self.manager.create_session.assert_not_awaited()

    async def test_a_scheduled_fire_logs_the_refusal_and_starts_no_session(self) -> None:
        with self.assertLogs("app.cron_service", level="ERROR") as logged:
            await self.cron._scheduled_run(self.job_id)

        self.assertEqual(
            [record.getMessage() for record in logged.records],
            [f"cron job {self.job_id} failed: the stored job's max_steps is not a number"],
        )
        self.manager.create_session.assert_not_awaited()

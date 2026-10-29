"""What agents and operators are told about the pre-request gate (step 3), on fakes.

- A session refused for want of a gate: `pre_request_gate_unavailable`, 503, retryable, naming PRE_REQUEST_BLOCKING,
  over REST and MCP.
- A session the controller closed because its gate was lost: the next call names that reason.
- Each block is audited as `navigation_blocked` with `source: "request"`, by origin only.
- /readiness reports the gate's state, as a warning at worst.

tests/test_request_gate_surfaces_chromium.py covers the navigate, click, popup, tab list and start URL surfaces
on real Chromium.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.action_errors import PRE_REQUEST_GATE_UNAVAILABLE, BrowserActionError, SessionNotFoundError
from app.browser.request_gate import GateBlock, RequestGateUnavailable
from app.browser_manager import BrowserManager
from app.models import McpToolCallRequest
from app.readiness import pre_request_gate_check, run_readiness_checks
from app.routes.sessions import create_sessions_router
from app.tool_gateway import McpToolGateway
from tests._fake_cdp import FakeCDPSession
from tests.test_browser_manager_create_session import _settings
from tests.test_request_gate_wiring import _browser


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = BrowserManager(_settings(Path(self.tmp.name)))
        self.manager._settle = AsyncMock()  # type: ignore[method-assign]
        self.manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]
        self.browser = _browser()
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def refuse_the_gate(self) -> None:
        async def refusing() -> FakeCDPSession:
            return FakeCDPSession({"Fetch.enable": RuntimeError("Fetch.enable failed")})

        self.browser.new_browser_cdp_session.side_effect = refusing

    @staticmethod
    async def settled() -> None:
        for _ in range(20):
            await asyncio.sleep(0)


class RefusedCreateTests(_Base):
    """Q4: a dedicated code, 503, retryable, the same over REST and MCP."""

    async def test_the_error_is_a_retryable_503_naming_the_switch(self) -> None:
        self.refuse_the_gate()

        with self.assertRaises(RequestGateUnavailable) as caught:
            await self.manager.create_session(name="s")

        error = caught.exception
        self.assertIsInstance(error, BrowserActionError)
        self.assertEqual((error.code, error.status_code, error.retryable), (PRE_REQUEST_GATE_UNAVAILABLE, 503, True))
        self.assertIn("PRE_REQUEST_BLOCKING", error.message)
        self.assertEqual(error.message.count("PRE_REQUEST_BLOCKING=false"), 1, "the advice is repeated")

    async def test_over_mcp(self) -> None:
        self.refuse_the_gate()
        gateway = McpToolGateway(manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())

        result = await gateway.call_tool(McpToolCallRequest(name="browser.create_session", arguments={"name": "s"}))

        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["code"], PRE_REQUEST_GATE_UNAVAILABLE)
        self.assertTrue(result.structuredContent["retryable"])
        self.assertIn("PRE_REQUEST_BLOCKING", result.structuredContent["error"])


def test_a_refused_create_over_rest_is_a_503_with_its_code(tmp_path: Path) -> None:
    import app.main as main_module

    manager = BrowserManager(_settings(tmp_path))
    browser = _browser()

    async def refusing() -> FakeCDPSession:
        return FakeCDPSession({"Fetch.enable": RuntimeError("Fetch.enable failed")})

    browser.new_browser_cdp_session.side_effect = refusing
    manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]
    app = FastAPI()
    app.include_router(create_sessions_router(manager=manager))
    app.add_exception_handler(BrowserActionError, main_module.handle_browser_action_error)

    with TestClient(app) as client:
        response = client.post("/sessions", json={"name": "s"})

    assert response.status_code == 503
    assert response.json()["code"] == PRE_REQUEST_GATE_UNAVAILABLE
    assert response.json()["retryable"] is True


class GateLostCloseTests(_Base):
    """Q3: a session containment closed says why on the next call."""

    async def test_the_next_call_names_the_lost_gate(self) -> None:
        session_id = (await self.manager.create_session(name="s"))["id"]

        self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
        await self.settled()

        with self.assertRaises(SessionNotFoundError) as caught:
            await self.manager.get_session(session_id)
        self.assertEqual(caught.exception.code, "session_closed")
        self.assertEqual(caught.exception.reason, "request_gate_lost")
        self.assertIn("pre-request gate detached", caught.exception.message)
        record = await self.manager.session_store.get(session_id)
        self.assertEqual((record.status, record.close_reason), ("closed", "request_gate_lost"))

    async def test_a_close_the_caller_asked_for_has_no_reason(self) -> None:
        session_id = (await self.manager.create_session(name="s"))["id"]

        await self.manager.close_session(session_id)

        with self.assertRaises(SessionNotFoundError) as caught:
            await self.manager.get_session(session_id)
        self.assertIsNone(caught.exception.reason)
        self.assertNotIn("pre-request gate", caught.exception.message)


class BlockAuditTests(_Base):
    async def test_a_block_is_audited_by_origin_with_its_source(self) -> None:
        self.manager.audit.append = AsyncMock()  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="s"))["id"]
        session = self.manager.sessions[session_id]
        cdp = self.browser.cdp_sessions[0]
        cdp.emit(
            "Target.targetCreated",
            {"targetInfo": {"targetId": "p1", "type": "page", "browserContextId": session.gate_context_id}},
        )

        cdp.emit(
            "Fetch.requestPaused",
            {
                "requestId": "r1",
                "frameId": "p1",
                "request": {"url": "https://refused.example/secret/path?token=abc", "method": "GET", "headers": {}},
            },
        )
        await cdp.wait_for_reply("r1")
        await self.settled()

        [details] = [
            call.kwargs["details"]
            for call in self.manager.audit.append.await_args_list
            if call.kwargs.get("event_type") == "navigation_blocked"
        ]
        self.assertEqual(details, {"url": "https://refused.example", "source": "request", "reason": "off_list"})

    async def test_r6_a_block_in_no_session_is_audited_without_one(self) -> None:
        self.manager.audit.append = AsyncMock()  # type: ignore[method-assign]

        self.manager._on_navigation_stopped(
            GateBlock(context_id=None, target_id="t", origin="https://x.example", reason="unclassified", explanation="")
        )
        await self.settled()

        [call] = self.manager.audit.append.await_args_list
        self.assertIsNone(call.kwargs["session_id"])
        self.assertEqual(call.kwargs["details"]["source"], "request")

    async def test_r6_a_lookup_error_block_on_a_sessions_page_is_audited_to_that_session(self) -> None:
        self.manager.audit.append = AsyncMock()  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="s"))["id"]
        page = self.manager.sessions[session_id].page
        page.context = self.manager.sessions[session_id].context  # as a Playwright page has
        target_id = await self.manager.request_gates.target_id_of(page)
        self.assertIsNotNone(target_id)
        cdp = self.browser.cdp_sessions[0]
        cdp.replies["Target.getTargetInfo"] = RuntimeError("Target.getTargetInfo: boom")

        cdp.emit(
            "Fetch.requestPaused",
            {
                "requestId": "r1",
                "frameId": target_id,
                "request": {"url": "https://refused.example/", "method": "GET", "headers": {}},
            },
        )
        await cdp.wait_for_reply("r1")
        await self.settled()

        [call] = [
            c for c in self.manager.audit.append.await_args_list if c.kwargs.get("event_type") == "navigation_blocked"
        ]
        self.assertEqual((call.kwargs["session_id"], call.kwargs["details"]["reason"]), (session_id, "unclassified"))


class ReadinessTests(unittest.TestCase):
    def settings(self, **values) -> SimpleNamespace:
        return SimpleNamespace(**{"pre_request_blocking": True, "allowed_hosts": "example.com", **values})

    def test_attached(self) -> None:
        gates = {"state": "attached", "gates": [{"state": "attached", "attach_mode": False, "prefetch_pref": 2}]}

        self.assertEqual(pre_request_gate_check(self.settings(), gates).status, "pass")

    def test_r3_a_launched_browser_with_no_prefetch_readback_warns(self) -> None:
        gates = {"state": "attached", "gates": [{"state": "attached", "attach_mode": False}]}

        check = pre_request_gate_check(self.settings(), gates)

        self.assertEqual(check.status, "warn")
        self.assertIn("prefetch", check.message)

    def test_attach_mode_without_the_switch_warns(self) -> None:
        gates = {"state": "attached", "gates": [{"state": "attached", "attach_mode": True, "switch_declared": False}]}

        check = pre_request_gate_check(self.settings(), gates)

        self.assertEqual(check.status, "warn")
        self.assertIn("BlockOriginHeaderModificationOnRedirect", check.fix)
        self.assertIn("operator's own tabs", check.message)

    def test_attach_mode_with_the_switch_passes(self) -> None:
        gates = {"state": "attached", "gates": [{"state": "attached", "attach_mode": True, "switch_declared": True}]}

        self.assertEqual(pre_request_gate_check(self.settings(), gates).status, "pass")

    def test_lost_and_failed_gates_warn_and_never_fail(self) -> None:
        for state in ("lost", "attach_failed"):
            check = pre_request_gate_check(self.settings(), {"state": state, "gates": [{"state": state}]})
            self.assertEqual(check.status, "warn", state)

    def test_a_launched_browser_whose_prefetch_is_still_on_warns(self) -> None:
        for read_back in (0, {"error": "TimeoutError: "}):
            gates = {
                "state": "attached",
                "gates": [{"state": "attached", "attach_mode": False, "prefetch_pref": read_back}],
            }

            check = pre_request_gate_check(self.settings(), gates)

            self.assertEqual(check.status, "warn", read_back)
            self.assertIn("prefetch", check.message)

    def test_a_launched_browser_whose_prefetch_is_off_passes(self) -> None:
        gates = {"state": "attached", "gates": [{"state": "attached", "attach_mode": False, "prefetch_pref": 2}]}

        self.assertEqual(pre_request_gate_check(self.settings(), gates).status, "pass")

    def test_switched_off_warns(self) -> None:
        check = pre_request_gate_check(self.settings(pre_request_blocking=False), {"state": "off", "gates": []})

        self.assertEqual(check.status, "warn")
        self.assertIn("PRE_REQUEST_BLOCKING=false", check.message)

    def test_nothing_to_stop_passes(self) -> None:
        for allowed in ("*", ""):
            check = pre_request_gate_check(self.settings(allowed_hosts=allowed), {"state": "off", "gates": []})
            self.assertEqual(check.status, "pass", allowed)

    def test_the_report_carries_it_and_a_lost_gate_does_not_fail_readiness(self) -> None:
        settings = MagicMock(
            auth_state_encryption_key="k",
            require_auth_state_encryption=False,
            allowed_hosts="example.com",
            pre_request_blocking=True,
        )
        with patch("app.readiness.tesseract_available", return_value=True):
            report = run_readiness_checks(settings, request_gates={"state": "lost", "gates": [{"state": "lost"}]})

        [check] = [check for check in report.checks if check.name == "pre_request_gate"]
        self.assertEqual(check.status, "warn")
        self.assertNotEqual(report.overall, "fail")


class GatesStatusTests(_Base):
    async def test_status_reports_each_gates_state(self) -> None:
        await self.manager.create_session(name="s")
        self.assertEqual(self.manager.request_gates.status()["state"], "attached")

        self.browser.cdp_sessions[0].emit("close", self.browser.cdp_sessions[0])
        await self.settled()
        self.assertEqual(self.manager.request_gates.status()["state"], "lost")

    async def test_status_reports_a_failed_attach(self) -> None:
        self.refuse_the_gate()
        await self.manager.request_gates.attach(self.browser, attach_mode=True)

        status = self.manager.request_gates.status()

        self.assertEqual(status["state"], "attach_failed")
        self.assertIn("Fetch.enable failed", status["gates"][0]["error"])

    async def test_status_off(self) -> None:
        self.manager.settings.pre_request_blocking = False

        self.assertEqual(self.manager.request_gates.status(), {"state": "off", "gates": []})

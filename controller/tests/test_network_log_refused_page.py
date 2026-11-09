"""The network log of a page ALLOWED_HOSTS refuses is withheld on every surface that serves it (AUTH-22).

A refused page's own requests are in its session's network log, with their URLs, and the raw CDP route already
refuses that page. The log had four surfaces and none of them checked the page: GET /sessions/{id}/network/requests,
GET /sessions/{id}/network-log, the browser.get_network_log tool and the browser://<id>/network resource. Each now
refuses it, under the session lock as the raw CDP route does, and says nothing of the refused host. A session whose
page is allowed still gets its log.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI

from app.browser.page_read import REFUSED_NETWORK_LOG_DETAIL
from app.browser_manager import BrowserManager, BrowserSession
from app.mcp_transport import McpHttpTransport, McpSession
from app.models import McpToolCallRequest
from app.routes._utils import OFF_LIST_PAGE_DETAIL
from app.routes.extensions.network import network_router
from app.routes.session_diagnostics import create_session_diagnostics_router
from app.tool_gateway.gateway import McpToolGateway
from app.utils import UTC
from tests.test_browser_manager_create_session import FakePage, _settings
from tests.test_mcp_resource_ownership import PROTOCOL, mcp_request

SID = "s1"
REFUSED_HOST = "refused.example"
REFUSED_PAGE = f"https://{REFUSED_HOST}/page?token=sentinel-page"
REFUSED_REQUEST = f"https://{REFUSED_HOST}/api?key=sentinel-key"
ALLOWED_PAGE = "https://example.com/page"
ALLOWED_REQUEST = "https://example.com/api?key=sentinel-key"
REFUSAL = {"detail": OFF_LIST_PAGE_DETAIL}
BUSY_DETAIL = "The session is busy with another call. Try again."
# locked_read reads the bound where it is defined, so it is patched there, by name.
READ_LOCK_BOUND = "app.browser.page_read.READ_LOCK_TIMEOUT_SECONDS"
SHORT_BOUND = 0.05


class _Inspector:
    def __init__(self, url: str) -> None:
        self.url = url

    def entries(self, limit: int = 100, method: str | None = None, url_contains: str | None = None) -> list[dict]:
        return [{"method": "GET", "url": self.url, "resource_type": "document", "status": 200}][:limit]

    def summary(self) -> dict:
        return {"total": 1, "failed": 0, "hooks": 0}


class _Env(unittest.IsolatedAsyncioTestCase):
    """A real manager, its diagnostics service and its page check, and a session whose page the test sets."""

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.manager = BrowserManager(_settings(Path(tmp.name)))
        self.page = FakePage()
        artifact_dir = Path(tmp.name) / "artifacts" / SID
        self.session = BrowserSession(
            id=SID,
            name=SID,
            created_at=datetime.now(UTC),
            context=object(),  # type: ignore[arg-type]
            page=self.page,  # type: ignore[arg-type]
            artifact_dir=artifact_dir,
            auth_dir=artifact_dir / "auth",
            upload_dir=artifact_dir / "uploads",
            takeover_url="http://127.0.0.1:6080/vnc.html",
            trace_path=artifact_dir / "trace.zip",
        )
        self.manager.sessions[SID] = self.session
        self.api = api = FastAPI()
        api.state.browser_manager = self.manager
        api.state.network_inspectors = {}
        api.include_router(network_router)
        api.include_router(create_session_diagnostics_router(manager=self.manager, settings=self.manager.settings))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t")
        self.addAsyncCleanup(self.client.aclose)

    def on_a_refused_page(self) -> None:
        self.page.url = REFUSED_PAGE
        self.session.network_inspector = _Inspector(REFUSED_REQUEST)

    def on_an_allowed_page(self) -> None:
        self.page.url = ALLOWED_PAGE
        self.session.network_inspector = _Inspector(ALLOWED_REQUEST)

    def assert_no_refused_host(self, answer: str) -> None:
        self.assertNotIn(REFUSED_HOST, answer)
        self.assertNotIn("sentinel-key", answer)
        self.assertNotIn("sentinel-page", answer)


class RestNetworkRequestsTests(_Env):
    PATH = f"/sessions/{SID}/network/requests"

    async def test_a_refused_page_gets_the_cdp_routes_403_and_no_url(self) -> None:
        self.on_a_refused_page()

        response = await self.client.get(self.PATH)

        self.assertEqual((response.status_code, response.json()), (403, REFUSAL))
        self.assert_no_refused_host(response.text)

    async def test_an_allowed_page_gets_its_log(self) -> None:
        self.on_an_allowed_page()

        response = await self.client.get(self.PATH)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["requests"][0]["url"], ALLOWED_REQUEST)

    async def test_a_page_refused_while_the_read_waited_for_the_lock_is_refused_under_it(self) -> None:
        self.on_an_allowed_page()
        await self.session.lock.acquire()
        try:
            read = asyncio.ensure_future(self.client.get(self.PATH))
            await asyncio.sleep(0.05)
            self.assertFalse(read.done(), "the route answered while another call held the session")
            self.on_a_refused_page()
        finally:
            self.session.lock.release()

        response = await asyncio.wait_for(read, timeout=5)

        self.assertEqual((response.status_code, response.json()), (403, REFUSAL))

    async def test_a_session_held_past_the_bound_answers_busy(self) -> None:
        self.on_an_allowed_page()
        await self.session.lock.acquire()
        try:
            with mock.patch(READ_LOCK_BOUND, SHORT_BOUND):
                response = await self.client.get(self.PATH)
        finally:
            self.session.lock.release()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": BUSY_DETAIL})

    async def test_a_session_gone_while_its_inspector_is_registered_has_no_log(self) -> None:
        # An inspector outlives its session until the close finishes: no live session has no page to judge.
        self.on_an_allowed_page()
        self.api.state.network_inspectors[SID] = self.session.network_inspector
        del self.manager.sessions[SID]

        response = await self.client.get(self.PATH)

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(), {"detail": f"No network inspector for session {SID!r}"})
        self.assertNotIn(ALLOWED_REQUEST, response.text)

    async def test_a_session_nobody_owns_with_no_inspector_is_still_404(self) -> None:
        self.on_an_allowed_page()
        self.session.network_inspector = None

        response = await self.client.get(self.PATH)

        self.assertEqual(response.status_code, 404, response.text)


class RestNetworkLogTests(_Env):
    PATH = f"/sessions/{SID}/network-log"

    async def test_a_refused_page_gets_the_cdp_routes_403_and_no_url(self) -> None:
        self.on_a_refused_page()

        response = await self.client.get(self.PATH)

        self.assertEqual((response.status_code, response.json()), (403, REFUSAL))
        self.assert_no_refused_host(response.text)

    async def test_an_allowed_page_gets_its_log(self) -> None:
        self.on_an_allowed_page()

        response = await self.client.get(self.PATH)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["entries"][0]["url"], ALLOWED_REQUEST)

    async def test_a_page_refused_while_the_read_waited_for_the_lock_is_refused_under_it(self) -> None:
        self.on_an_allowed_page()
        await self.session.lock.acquire()
        try:
            read = asyncio.ensure_future(self.client.get(self.PATH))
            await asyncio.sleep(0.05)
            self.assertFalse(read.done(), "the route answered while another call held the session")
            self.on_a_refused_page()
        finally:
            self.session.lock.release()

        response = await asyncio.wait_for(read, timeout=5)

        self.assertEqual((response.status_code, response.json()), (403, REFUSAL))


class BoundedReadOnBothRoutesTests(_Env):
    """Both REST routes wait for the session lock within one bound, and answer a page that cannot be read alike."""

    PATHS = (f"/sessions/{SID}/network/requests", f"/sessions/{SID}/network-log")

    async def test_a_held_lock_answers_the_busy_refusal_with_one_status(self) -> None:
        self.on_an_allowed_page()
        await self.session.lock.acquire()
        try:
            with mock.patch(READ_LOCK_BOUND, SHORT_BOUND):
                answers = [await asyncio.wait_for(self.client.get(path), timeout=5) for path in self.PATHS]
        finally:
            self.session.lock.release()

        self.assertEqual([answer.status_code for answer in answers], [409, 409], [answer.text for answer in answers])
        self.assertEqual(answers[0].json(), answers[1].json())
        self.assertEqual(answers[0].json(), {"detail": BUSY_DETAIL})

    async def test_a_refused_page_is_turned_away_before_the_wait(self) -> None:
        self.on_a_refused_page()
        await self.session.lock.acquire()
        try:
            answers = [await asyncio.wait_for(self.client.get(path), timeout=5) for path in self.PATHS]
        finally:
            self.session.lock.release()

        self.assertEqual([(answer.status_code, answer.json()) for answer in answers], [(403, REFUSAL), (403, REFUSAL)])


class ThePageIsCheckedAfterTheAwaitTests(_Env):
    """A page can move while the summary awaits: the entries are read last, and only if the page stayed allowed."""

    def moves_during_the_summary(self):
        """A _session_summary that sends the page onto a refused host during its await, as a page's own script can."""

        async def summary(_session):
            await asyncio.sleep(0)
            self.page.url = REFUSED_PAGE
            return {"id": SID, "current_url": ALLOWED_PAGE}

        return mock.patch.object(self.manager, "_session_summary", summary)

    async def test_the_service_refuses_with_the_fixed_text_and_no_entries(self) -> None:
        self.on_an_allowed_page()

        with self.moves_during_the_summary(), self.assertRaises(PermissionError) as refused:
            await self.manager.get_network_log(SID)

        self.assertEqual(str(refused.exception), REFUSED_NETWORK_LOG_DETAIL)
        self.assertIsNone(refused.exception.__cause__)

    async def test_the_refused_network_log_route_gets_no_entries(self) -> None:
        self.on_an_allowed_page()

        with self.moves_during_the_summary():
            response = await self.client.get(f"/sessions/{SID}/network-log")

        self.assertEqual((response.status_code, response.json()), (403, REFUSAL))
        self.assert_no_refused_host(response.text)

    async def test_the_tool_gets_no_entries(self) -> None:
        self.on_an_allowed_page()
        gateway = McpToolGateway(
            manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
        )

        with self.moves_during_the_summary():
            response = await gateway.call_tool(
                McpToolCallRequest(name="browser.get_network_log", arguments={"session_id": SID})
            )

        self.assertTrue(response.isError, response)
        self.assertEqual(response.structuredContent["code"], "not_permitted")
        self.assertEqual(response.structuredContent["error"], REFUSED_NETWORK_LOG_DETAIL)
        self.assert_no_refused_host(response.model_dump_json())

    async def test_a_refusal_before_the_lock_is_the_same_fixed_text(self) -> None:
        self.on_a_refused_page()

        with self.assertRaises(PermissionError) as refused:
            await self.manager.get_network_log(SID)

        self.assertEqual(str(refused.exception), REFUSED_NETWORK_LOG_DETAIL)

    async def test_a_page_that_stays_allowed_still_gets_its_entries(self) -> None:
        self.on_an_allowed_page()

        answer = await self.manager.get_network_log(SID)

        self.assertEqual(answer["entries"][0]["url"], ALLOWED_REQUEST)
        self.assertTrue(answer["enabled"])

    async def test_a_session_with_no_inspector_answers_not_enabled_as_before(self) -> None:
        self.on_an_allowed_page()
        self.session.network_inspector = None

        answer = await self.manager.get_network_log(SID)

        self.assertEqual((answer["enabled"], answer["entries"], answer["summary"]), (False, [], {}))
        self.assertIn("session", answer)


class BusyBeyondRestTests(_Env):
    async def test_the_tool_answers_the_busy_refusal_as_its_other_refusals(self) -> None:
        self.on_an_allowed_page()
        gateway = McpToolGateway(
            manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
        )
        await self.session.lock.acquire()
        try:
            with mock.patch(READ_LOCK_BOUND, SHORT_BOUND):
                response = await gateway.call_tool(
                    McpToolCallRequest(name="browser.get_network_log", arguments={"session_id": SID})
                )
        finally:
            self.session.lock.release()

        self.assertTrue(response.isError, response)
        self.assertEqual(response.structuredContent["error"], BUSY_DETAIL)

    async def test_the_busy_refusal_is_not_a_permission_refusal(self) -> None:
        self.on_an_allowed_page()
        await self.session.lock.acquire()
        try:
            with mock.patch(READ_LOCK_BOUND, SHORT_BOUND), self.assertRaises(RuntimeError) as busy:
                await self.manager.get_network_log(SID)
        finally:
            self.session.lock.release()

        self.assertEqual(str(busy.exception), BUSY_DETAIL)


class GetNetworkLogToolTests(_Env):
    async def _call(self) -> object:
        gateway = McpToolGateway(
            manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace(), tool_profile="full"
        )
        return await gateway.call_tool(
            McpToolCallRequest(name="browser.get_network_log", arguments={"session_id": SID})
        )

    async def test_a_refused_page_is_refused_and_names_no_host(self) -> None:
        self.on_a_refused_page()

        response = await self._call()

        self.assertTrue(response.isError, response)
        self.assert_no_refused_host(response.model_dump_json())
        self.assertEqual(response.structuredContent["code"], "not_permitted")

    async def test_an_allowed_page_gets_its_log(self) -> None:
        self.on_an_allowed_page()

        response = await self._call()

        self.assertFalse(response.isError, response)
        self.assertEqual(response.structuredContent["entries"][0]["url"], ALLOWED_REQUEST)


class NetworkResourceTests(_Env):
    URI = f"browser://{SID}/network"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.transport = McpHttpTransport(
            tool_gateway=SimpleNamespace(list_tools=lambda: []),
            server_name="auto-browser",
            server_version="0",
            manager=self.manager,
        )
        self.transport._sessions["m1"] = McpSession(
            id="m1", protocol_version=PROTOCOL, client_info={}, client_capabilities={}, initialized=True
        )

    async def _read(self) -> dict:
        body = {"jsonrpc": "2.0", "id": 7, "method": "resources/read", "params": {"uri": self.URI}}
        response = await self.transport.handle_post_request(mcp_request("POST", "m1", body))
        self.assertEqual(response.status_code, 200)
        return json.loads(response.body)

    async def test_a_refused_page_is_a_failed_read_with_fixed_text(self) -> None:
        self.on_a_refused_page()

        answer = await self._read()

        self.assertEqual(answer["error"]["code"], -32603)
        self.assertEqual(answer["error"]["message"], f"Could not read resource: {self.URI}")
        self.assertNotIn("result", answer)
        self.assert_no_refused_host(json.dumps(answer))

    async def test_an_allowed_page_gets_its_log(self) -> None:
        self.on_an_allowed_page()

        answer = await self._read()

        self.assertNotIn("error", answer)
        contents = answer["result"]["contents"][0]
        self.assertIn(ALLOWED_REQUEST, contents["text"])

    async def test_a_page_that_moves_during_the_summary_is_a_failed_read(self) -> None:
        self.on_an_allowed_page()

        async def summary(_session):
            self.page.url = REFUSED_PAGE
            return {"id": SID}

        with mock.patch.object(self.manager, "_session_summary", summary):
            answer = await self._read()

        self.assertEqual(answer["error"]["code"], -32603)
        self.assert_no_refused_host(json.dumps(answer))

    async def test_a_held_lock_is_a_failed_read(self) -> None:
        self.on_an_allowed_page()
        await self.session.lock.acquire()
        try:
            with mock.patch(READ_LOCK_BOUND, SHORT_BOUND):
                answer = await self._read()
        finally:
            self.session.lock.release()

        self.assertEqual(answer["error"]["code"], -32603)
        self.assertEqual(answer["error"]["message"], f"Could not read resource: {self.URI}")


if __name__ == "__main__":
    unittest.main()

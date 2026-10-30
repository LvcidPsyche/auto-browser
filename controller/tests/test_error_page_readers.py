"""What the readers say about Chrome's error page that a stopped navigation left (the 1.12 follow-ups, FU1).

A navigation the pre-request gate stopped leaves the page on ``chrome-error://chromewebdata/``. Its DOM is page
content and is returned as any page's is. The controller's own statement of what happened, the block's origin,
reason and explanation, is added as ``blocked_navigation`` to observe and to browser.get_html, while that block is
the page's last main-frame request (F7's rule), as the tab list already does.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI

from app.routes.sessions import create_sessions_router
from app.tool_gateway import McpToolGateway
from app.tool_inputs import GetPageHtmlInput
from tests import test_request_gate_wiring as wiring
from tests.test_request_gate_wiring import _gate_sessions

ERROR_PAGE = "chrome-error://chromewebdata/"
REFUSED_URL = "https://refused.example/secret?q=1"


class ErrorPageReaderTests(unittest.IsolatedAsyncioTestCase):
    """On the wiring harness: a real manager, its gate on a fake browser."""

    async def asyncSetUp(self) -> None:
        self.harness = wiring.GateWiringTests()
        await wiring.GateWiringTests.asyncSetUp(self.harness)
        self.manager, self.browser = self.harness.manager, self.harness.browser

    async def asyncTearDown(self) -> None:
        await self.harness.asyncTearDown()

    async def _stopped_session(self):
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session = self.manager.sessions[(await self.manager.create_session(name="s"))["id"]]
        self.manager.request_gates.target_id_of = AsyncMock(return_value="the-page")  # type: ignore[method-assign]
        self._pause(session, "stopped", REFUSED_URL)
        await _gate_sessions(self.browser)[0].wait_for_reply("stopped")
        session.page.url = ERROR_PAGE
        # The page reads themselves are not under test: the observation's own fields are stubbed.
        self.manager.observation._read_observation = AsyncMock(  # type: ignore[method-assign]
            return_value={"url": ERROR_PAGE, "title": "refused.example"}
        )
        return session

    def _pause(self, session, request_id: str, url: str) -> None:
        [gate_session] = _gate_sessions(self.browser)
        gate_session.replies["Target.getTargetInfo"] = {
            "targetInfo": {"targetId": "the-page", "type": "page", "browserContextId": session.gate_context_id}
        }
        gate_session.emit(
            "Fetch.requestPaused",
            {"requestId": request_id, "frameId": "the-page", "request": {"url": url, "method": "GET", "headers": {}}},
        )

    def _assert_names_the_origin_only(self, block: dict) -> None:
        self.assertEqual(set(block), {"origin", "reason", "explanation"})
        self.assertEqual((block["origin"], block["reason"]), ("https://refused.example", "off_list"))
        self.assertNotIn("secret", str(block))

    async def test_fu1_observe_on_a_stopped_navigations_error_page_says_what_was_stopped(self) -> None:
        session = await self._stopped_session()

        result = await self.manager.observe(session.id)

        self._assert_names_the_origin_only(result["blocked_navigation"])

    async def test_fu1_a_later_error_page_of_another_failure_is_not_labelled_by_the_old_block(self) -> None:
        session = await self._stopped_session()
        self._pause(session, "later", "https://example.com/unreachable")  # allowed, then failed at DNS: no commit
        await _gate_sessions(self.browser)[0].wait_for_reply("later")

        result = await self.manager.observe(session.id)

        self.assertNotIn("blocked_navigation", result)

    async def test_fu1_an_allowed_page_carries_no_label(self) -> None:
        session = await self._stopped_session()
        session.page.url = "https://example.com/"

        result = await self.manager.observe(session.id)

        self.assertNotIn("blocked_navigation", result)

    async def test_fu1_get_html_returns_the_dom_as_it_is_and_says_what_was_stopped(self) -> None:
        session = await self._stopped_session()
        dom = f"<html><body>{REFUSED_URL} is blocked</body></html>"
        session.page.content = AsyncMock(return_value=dom)
        gateway = McpToolGateway(manager=self.manager, orchestrator=MagicMock(), job_queue=MagicMock())

        result = await gateway._get_html(GetPageHtmlInput(session_id=session.id))

        self.assertIn(REFUSED_URL, result["content"], "the error page's DOM was rewritten")
        self._assert_names_the_origin_only(result["blocked_navigation"])

    async def test_fu1_rest_observe_carries_it(self) -> None:
        session = await self._stopped_session()
        app = FastAPI()
        app.include_router(create_sessions_router(manager=self.manager))

        async with self._client(app) as client:
            response = await client.get(f"/sessions/{session.id}/observe")

        self.assertEqual(response.status_code, 200)
        self._assert_names_the_origin_only(response.json()["blocked_navigation"])

    @staticmethod
    def _client(app: FastAPI):
        import httpx

        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://controller")


if __name__ == "__main__":
    unittest.main()

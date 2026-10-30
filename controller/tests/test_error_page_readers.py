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
from tests.test_browser_manager_create_session import FakePage
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

        # The page reads themselves are not under test: the observation's own fields are stubbed, with the real
        # tab list, which the observation's label is taken from.
        async def read(session_, **_kwargs):
            return {
                "url": session_.page.url,
                "title": "refused.example",
                "tabs": await self.manager.tabs.summaries(session_),
            }

        self.manager.observation._read_observation = AsyncMock(side_effect=read)  # type: ignore[method-assign]
        return session

    def _pause(self, session, request_id: str, url: str, *, target_id: str = "the-page") -> None:
        [gate_session] = _gate_sessions(self.browser)
        gate_session.replies["Target.getTargetInfo"] = {
            "targetInfo": {"targetId": target_id, "type": "page", "browserContextId": session.gate_context_id}
        }
        gate_session.emit(
            "Fetch.requestPaused",
            {"requestId": request_id, "frameId": target_id, "request": {"url": url, "method": "GET", "headers": {}}},
        )

    def _second_tab(self, session, target_id: str) -> FakePage:
        """A second tab of the session, on the error page, with its own targetId."""
        tab = FakePage()
        tab.context = session.context
        tab.url = ERROR_PAGE
        session.context.pages = [session.page, tab]
        by_page = {id(session.page): "the-page", id(tab): target_id}
        self.manager.request_gates.target_id_of = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda page: by_page[id(page)]
        )
        return tab

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

    # ── round 2: the label is decided on the URL the tab list shows, once per observe ─────────────────────

    async def test_r2_a_tab_listed_as_allowed_never_carries_the_label(self) -> None:
        """The listing read the page on an allowed URL; it moved to the error page before the label was decided."""
        session = await self._stopped_session()
        self.manager._shown_url = MagicMock(return_value="https://example.com/")  # type: ignore[method-assign]

        result = await self.manager.observe(session.id)

        [tab] = result["tabs"]
        self.assertEqual(tab["url"], "https://example.com/")
        self.assertNotIn("blocked_navigation", tab, "a tab listed as allowed was labelled")
        self.assertNotIn("blocked_navigation", result)

    async def test_r2_a_tab_listed_on_the_error_page_reads_its_block(self) -> None:
        """The listing read the error page; the page moved on before the label was decided."""
        session = await self._stopped_session()
        self.manager._shown_url = MagicMock(return_value=ERROR_PAGE)  # type: ignore[method-assign]
        session.page.url = "https://example.com/"

        result = await self.manager.observe(session.id)

        [tab] = result["tabs"]
        self._assert_names_the_origin_only(tab["blocked_navigation"])
        self.assertEqual(result["blocked_navigation"], tab["blocked_navigation"])

    async def test_r2_observe_looks_the_block_up_once(self) -> None:
        session = await self._stopped_session()
        target_id_of = self.manager.request_gates.target_id_of

        await self.manager.observe(session.id)

        self.assertEqual(target_id_of.await_count, 1)

    async def test_r2_another_tabs_block_does_not_label_the_active_page(self) -> None:
        """The active page failed on its own (no block); a background tab of the session was stopped (7a)."""
        self.manager._acquire_session_browser = AsyncMock(return_value=(self.browser, None))  # type: ignore[method-assign]
        session = self.manager.sessions[(await self.manager.create_session(name="s"))["id"]]
        tab = self._second_tab(session, "tab-2")
        self._pause(session, "tab-stopped", REFUSED_URL, target_id="tab-2")
        await _gate_sessions(self.browser)[0].wait_for_reply("tab-stopped")
        session.page.url = ERROR_PAGE
        self.manager.observation._read_observation = AsyncMock(side_effect=self._read_with_tabs)  # type: ignore[method-assign]
        session.page.content = AsyncMock(return_value="<html></html>")
        gateway = McpToolGateway(manager=self.manager, orchestrator=MagicMock(), job_queue=MagicMock())

        observed = await self.manager.observe(session.id)
        html = await gateway._get_html(GetPageHtmlInput(session_id=session.id))

        self.assertNotIn("blocked_navigation", observed)
        self.assertNotIn("blocked_navigation", html)
        active, background = observed["tabs"]
        self.assertTrue(active["active"])
        self.assertNotIn("blocked_navigation", active)
        self._assert_names_the_origin_only(background["blocked_navigation"])
        self.assertIs(session.context.pages[1], tab)

    async def test_r2_another_sessions_block_on_the_same_browser_does_not_label_this_page(self) -> None:
        session = await self._stopped_session()
        other = self.manager.sessions[(await self.manager.create_session(name="other"))["id"]]
        other.page = FakePage()  # the fake browser hands every session the same page; this one needs its own
        other.page.url = ERROR_PAGE
        by_page = {id(session.page): "the-page", id(other.page): "other-page"}
        self.manager.request_gates.target_id_of = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda page: by_page[id(page)]
        )
        # This session's own stop is superseded by a request of its page that was allowed, then failed.
        self._pause(session, "later", "https://example.com/unreachable")
        await _gate_sessions(self.browser)[0].wait_for_reply("later")
        self._pause(other, "other-stopped", "https://refused.example/other", target_id="other-page")
        await _gate_sessions(self.browser)[0].wait_for_reply("other-stopped")

        result = await self.manager.observe(session.id)

        self.assertNotIn("blocked_navigation", result)
        self.assertNotIn("blocked_navigation", result["tabs"][0])

    async def _read_with_tabs(self, session, **_kwargs) -> dict:
        return {"url": session.page.url, "title": "", "tabs": await self.manager.tabs.summaries(session)}

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

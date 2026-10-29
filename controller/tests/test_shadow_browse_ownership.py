"""A shadow-browse session carries the owner of the session it was made from.

enable_shadow_browse copies a live session's cookies into a new headed browser.
The copy was created with no owner, so under named credentials (API_BEARER_TOKENS)
any other operator could list it, drive it and read the original owner's logged-in
pages. The copy is the same logins, so it belongs to the same operator.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.action_errors import SessionNotFoundError
from app.models import OperatorIdentity
from app.session_ownership import acting_as
from tests.test_auth_state_ownership import StateContext, build_manager
from tests.test_browser_manager_create_session import FakeBrowser, FakePage

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")


class ShadowBrowseOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _ = build_manager(Path(self.tmp.name))
        self.manager.settings.shadow_browse_enabled = True
        self.manager.settings.max_sessions = 5
        headed = FakeBrowser(StateContext(FakePage(), "shadow"))
        headed.close = AsyncMock()
        headed.new_context = AsyncMock(return_value=StateContext(FakePage(), "shadow"))
        self.manager.playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=headed)))  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _shadow_of(self, operator: OperatorIdentity | None) -> tuple[str, str]:
        with acting_as(operator):
            session_id = (await self.manager.create_session(name="source"))["id"]
            self.manager.sessions[session_id].context = StateContext(self.manager.sessions[session_id].page)
            shadow = await self.manager.enable_shadow_browse(session_id)
        return session_id, shadow["shadow_session_id"]

    async def test_the_shadow_context_is_listened_to_as_a_sessions_is(self) -> None:
        """F9: its popups, and the first navigation of a popup it has not reported yet."""
        _, shadow_id = await self._shadow_of(ALICE)

        context = self.manager.sessions[shadow_id].context
        self.assertEqual([call.args[0] for call in context.on.call_args_list], ["page", "request", "requestfailed"])

    async def test_the_shadow_session_belongs_to_the_source_owner(self) -> None:
        _, shadow_id = await self._shadow_of(ALICE)

        self.assertEqual(self.manager.sessions[shadow_id].owner, "alice")
        with acting_as(BOB), self.assertRaises(SessionNotFoundError):
            await self.manager.get_session(shadow_id)
        with acting_as(BOB):
            self.assertNotIn(shadow_id, [s["id"] for s in await self.manager.list_sessions()])
        with acting_as(ALICE):
            self.assertEqual((await self.manager.get_session(shadow_id)).id, shadow_id)

    async def test_the_shadow_of_an_unowned_session_stays_unowned(self) -> None:
        _, shadow_id = await self._shadow_of(None)

        self.assertIsNone(self.manager.sessions[shadow_id].owner)
        with acting_as(BOB):
            self.assertEqual((await self.manager.get_session(shadow_id)).id, shadow_id)

    async def test_shadow_browse_is_not_held_to_the_session_limit(self) -> None:
        """It replaces the session being debugged; a limit check would break MAX_SESSIONS=1."""
        self.manager.settings.max_sessions = 1

        _, shadow_id = await self._shadow_of(ALICE)

        self.assertIn(shadow_id, self.manager.sessions)


class ShadowBrowseAllowedHostsTests(unittest.IsolatedAsyncioTestCase):
    """R3-RDT-09: the shadow copied a refused page's URL into its reply and audit event, and opened it in
    the headed browser, a second request to the refused host."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _ = build_manager(Path(self.tmp.name))
        self.manager.settings.shadow_browse_enabled = True
        self.manager.settings.max_sessions = 5
        self.manager.settings.allowed_hosts = "allowed.example"
        self.manager.audit.append = AsyncMock()
        self.shadow_page = FakePage()
        headed = FakeBrowser(StateContext(self.shadow_page, "shadow"))
        headed.close = AsyncMock()
        headed.new_context = AsyncMock(return_value=StateContext(self.shadow_page, "shadow"))
        self.launch = AsyncMock(return_value=headed)
        self.manager.playwright = SimpleNamespace(chromium=SimpleNamespace(launch=self.launch))  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def _source(self, url: str) -> str:
        session_id = (await self.manager.create_session(name="source"))["id"]
        session = self.manager.sessions[session_id]
        session.context = StateContext(session.page)
        session.page.url = url
        return session_id

    async def test_a_refused_page_is_not_copied_into_a_shadow_browser(self) -> None:
        for url in ("https://refused.example/cb?code=REFUSED-CONTENT", "data:text/html,REFUSED-CONTENT"):
            with self.subTest(url=url):
                session_id = await self._source(url)

                with self.assertRaises(PermissionError):
                    await self.manager.enable_shadow_browse(session_id)

                self.launch.assert_not_awaited()

    async def test_a_tainted_page_is_not_copied_into_a_shadow_browser(self) -> None:
        session_id = await self._source("about:blank")
        page = self.manager.sessions[session_id].page
        self.manager.page_provenance.committed(page, "https://refused.example/x", url_refused=True)

        with self.assertRaises(PermissionError):
            await self.manager.enable_shadow_browse(session_id)

        self.launch.assert_not_awaited()

    async def test_control_an_allowed_page_is_opened_in_the_shadow_browser(self) -> None:
        url = "https://allowed.example/cart?step=2"
        session_id = await self._source(url)

        shadow = await self.manager.enable_shadow_browse(session_id)

        self.assertEqual(shadow["url"], url)
        self.assertEqual(self.shadow_page.url, url)
        self.assertEqual(self.manager.audit.append.await_args.kwargs["details"]["url"], url)


if __name__ == "__main__":
    unittest.main()

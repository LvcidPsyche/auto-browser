"""A fork keeps what the source was created with (NAV-04)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.session_ownership import acting_as
from tests.test_auth_state_ownership import ALICE, StateContext, build_manager


class ForkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _ = build_manager(Path(self.tmp.name))
        self.manager.settings.max_sessions = 5
        with acting_as(ALICE):
            self.source_id = (await self.manager.create_session(name="alice"))["id"]
        self.session = self.manager.sessions[self.source_id]
        self.session.context = StateContext(self.session.page)
        self.session.page.url = "https://example.com/"
        self.created = AsyncMock(return_value={"id": "fork-1"})
        self.manager.create_session = self.created  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def fork(self, **kwargs):
        with acting_as(ALICE):
            return await self.manager.fork_session(self.source_id, **kwargs)

    async def test_nav04_the_fork_carries_the_sources_persona_mode_agent_and_totp(self) -> None:
        self.session.proxy_persona = "residential"
        self.session.protection_mode = "confidential"
        self.session.user_agent = "Source-UA/1.0"
        self.session.totp_secret = "JBSWY3DPEHPK3PXP"
        self.session.totp_hosts = ("login.example.com",)

        await self.fork()

        kwargs = self.created.await_args.kwargs
        self.assertEqual(kwargs["proxy_persona"], "residential")
        self.assertEqual(kwargs["protection_mode"], "confidential")
        self.assertEqual(kwargs["user_agent"], "Source-UA/1.0")
        self.assertEqual(kwargs["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(kwargs["totp_hosts"], ["login.example.com"])

    async def test_nav04_an_explicit_request_value_wins(self) -> None:
        self.session.proxy_persona = "residential"
        self.session.protection_mode = "confidential"
        self.session.user_agent = "Source-UA/1.0"

        await self.fork(proxy_persona="mobile", protection_mode="normal", user_agent="Other-UA/2.0")

        kwargs = self.created.await_args.kwargs
        self.assertEqual(kwargs["proxy_persona"], "mobile")
        self.assertEqual(kwargs["protection_mode"], "normal")
        self.assertEqual(kwargs["user_agent"], "Other-UA/2.0")

    async def test_nav04_a_plain_source_passes_nothing_extra(self) -> None:
        await self.fork()

        kwargs = self.created.await_args.kwargs
        self.assertIsNone(kwargs["proxy_persona"])
        self.assertIsNone(kwargs["user_agent"])
        self.assertIsNone(kwargs["totp_secret"])
        self.assertIsNone(kwargs["totp_hosts"])

    async def test_nav04_create_stores_the_user_agent_a_fork_will_read(self) -> None:
        del self.manager.create_session  # the real method again, not setUp's mock
        with acting_as(ALICE):
            created = await self.manager.create_session(name="ua", user_agent="Created-UA/3.0")

        self.assertEqual(self.manager.sessions[created["id"]].user_agent, "Created-UA/3.0")

    async def test_b_fix_1_a_stealth_ua_drawn_at_create_is_the_one_the_fork_gets(self) -> None:
        # With STEALTH_ENABLED and no request UA, create drew a random pool entry and stored None, so the fork drew
        # again: the source and its fork showed sites two different browsers.
        del self.manager.create_session
        self.manager.settings.stealth_enabled = True
        draws = iter(["Pool-UA/A", "Pool-UA/B"])
        new_context = self.manager.browser.new_context
        with patch("app.config.random.choice", side_effect=lambda agents: next(draws)):
            with acting_as(ALICE):
                source_id = (await self.manager.create_session(name="stealth"))["id"]
                source = self.manager.sessions[source_id]
                source.context = StateContext(source.page)
                source.page.url = "https://example.com/"
                await self.manager.fork_session(source_id)

        self.assertEqual(source.user_agent, "Pool-UA/A")
        self.assertEqual(
            [call.kwargs.get("user_agent") for call in new_context.await_args_list[-2:]], ["Pool-UA/A", "Pool-UA/A"]
        )


if __name__ == "__main__":
    unittest.main()

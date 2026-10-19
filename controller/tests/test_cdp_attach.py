"""browser.cdp_attach swaps the one browser every session shares.

Under named credentials (API_BEARER_TOKENS) any operator could attach a Chrome
they control, and every session any operator created afterwards ran on it, open
to the attacker's own CDP connection. Attach is refused when more than one
credential is configured. With one credential it still must not pull the browser
out from under live sessions, and it closes the browser it replaces.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager
from app.models import McpToolCallRequest, OperatorIdentity
from app.session_ownership import acting_as
from app.tool_gateway.gateway import McpToolGateway
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

BOB = OperatorIdentity(id="bob", source="token")
CDP_URL = "http://10.0.0.9:9222"


def _browser(version: str = "fake") -> FakeBrowser:
    browser = FakeBrowser(FakeContext(FakePage()))
    browser.new_context = AsyncMock(side_effect=lambda **_kwargs: FakeContext(FakePage()))
    browser.is_connected = lambda: True  # type: ignore[attr-defined]
    browser.version = version  # type: ignore[attr-defined]
    browser.close = AsyncMock()  # type: ignore[attr-defined]
    return browser


class CdpAttachTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = BrowserManager(_settings(Path(self.tmp.name)))
        self.manager._settle = AsyncMock()  # type: ignore[method-assign]
        self.manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]
        self.original = _browser("original")
        self.attacker = _browser("attacker")
        self.manager.browser = self.original  # type: ignore[assignment]
        self.connect = AsyncMock(return_value=self.attacker)
        self.manager.playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=self.connect))  # type: ignore[assignment]
        self.manager._acquire_session_browser = AsyncMock(  # type: ignore[method-assign]
            return_value=(self.original, None)
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_multi_operator_attach_is_refused(self) -> None:
        self.manager.settings.api_bearer_tokens = "alice:token-a,bob:token-b"

        with self.assertRaisesRegex(PermissionError, "every operator"):
            await self.manager.cdp_attach(CDP_URL)

        self.assertIs(self.manager.browser, self.original)
        self.connect.assert_not_awaited()

    async def test_named_plus_shared_token_counts_as_multi_operator(self) -> None:
        self.manager.settings.api_bearer_tokens = "alice:token-a"
        self.manager.settings.api_bearer_token = "shared-token"

        with self.assertRaises(PermissionError):
            await self.manager.cdp_attach(CDP_URL)

        self.assertIs(self.manager.browser, self.original)

    async def test_multi_operator_attach_over_mcp_is_an_error_result(self) -> None:
        self.manager.settings.api_bearer_tokens = "alice:token-a,bob:token-b"
        gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=SimpleNamespace(),
            job_queue=SimpleNamespace(),
            tool_profile="full",
        )

        with acting_as(BOB):
            result = await gateway.call_tool(
                McpToolCallRequest(name="browser.cdp_attach", arguments={"cdp_url": CDP_URL})
            )

        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["code"], "not_permitted")
        self.assertIn("every operator", result.structuredContent["error"])
        self.assertIs(self.manager.browser, self.original)

    async def test_single_operator_attach_closes_the_previous_browser(self) -> None:
        self.manager.settings.api_bearer_tokens = ""
        self.manager.settings.api_bearer_token = "only-token"

        result = await self.manager.cdp_attach(CDP_URL)

        self.assertTrue(result["attached"])
        self.assertEqual(result["browser_version"], "attacker")
        self.assertIs(self.manager.browser, self.attacker)
        self.original.close.assert_awaited_once()

    async def test_attach_with_no_previous_browser_just_attaches(self) -> None:
        self.manager.browser = None

        result = await self.manager.cdp_attach(CDP_URL)

        self.assertTrue(result["attached"])
        self.assertIs(self.manager.browser, self.attacker)

    async def test_attach_refused_while_a_session_uses_the_shared_browser(self) -> None:
        session_id = (await self.manager.create_session(name="live"))["id"]
        self.assertIs(self.manager.sessions[session_id].browser, self.original)

        with self.assertRaisesRegex(RuntimeError, "close the 1 live session"):
            await self.manager.cdp_attach(CDP_URL)

        self.assertIs(self.manager.browser, self.original)
        self.original.close.assert_not_awaited()
        self.connect.assert_not_awaited()

    async def test_session_with_its_own_browser_does_not_block_attach(self) -> None:
        own = _browser("isolated")
        self.manager._acquire_session_browser = AsyncMock(return_value=(own, None))  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="isolated"))["id"]
        self.manager.sessions[session_id].owns_browser = True  # what an isolated runtime's session records
        self.assertIs(self.manager.sessions[session_id].browser, own)

        result = await self.manager.cdp_attach(CDP_URL)

        self.assertTrue(result["attached"])
        self.assertIs(self.manager.browser, self.attacker)
        self.original.close.assert_awaited_once()
        own.close.assert_not_awaited()

    async def test_failed_close_of_the_previous_browser_is_logged_not_fatal(self) -> None:
        self.original.close = AsyncMock(side_effect=RuntimeError("already gone"))

        with self.assertLogs("app.browser.services.runtime", level="WARNING") as logs:
            result = await self.manager.cdp_attach(CDP_URL)

        self.assertTrue(result["attached"])
        self.assertIs(self.manager.browser, self.attacker)
        self.assertIn("already gone", "\n".join(logs.output))


class SessionCloseAfterSwapTests(unittest.IsolatedAsyncioTestCase):
    """Closing a session closes only a browser that session owns alone."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = BrowserManager(_settings(Path(self.tmp.name)))
        self.manager._settle = AsyncMock()  # type: ignore[method-assign]
        self.manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]
        self.original = _browser("original")
        self.attacker = _browser("attacker")
        self.manager.browser = self.original  # type: ignore[assignment]
        self.manager._acquire_session_browser = AsyncMock(  # type: ignore[method-assign]
            return_value=(self.original, None)
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_closing_a_session_never_closes_the_shared_browser_after_a_swap(self) -> None:
        first = (await self.manager.create_session(name="first"))["id"]
        second = (await self.manager.create_session(name="second"))["id"]
        # The state a cdp_attach leaves behind: manager.browser is a different browser.
        self.manager.browser = self.attacker  # type: ignore[assignment]

        await self.manager.close_session(first)

        self.original.close.assert_not_awaited()
        self.attacker.close.assert_not_awaited()
        self.assertIn(second, self.manager.sessions)

    async def test_closing_a_session_with_its_own_browser_closes_it(self) -> None:
        own = _browser("isolated")
        self.manager._acquire_session_browser = AsyncMock(return_value=(own, None))  # type: ignore[method-assign]
        session_id = (await self.manager.create_session(name="isolated"))["id"]
        self.manager.sessions[session_id].owns_browser = True

        await self.manager.close_session(session_id)

        own.close.assert_awaited_once()
        self.original.close.assert_not_awaited()

    async def test_closing_a_shadow_session_closes_its_headed_browser(self) -> None:
        self.manager.settings.shadow_browse_enabled = True
        headed = _browser("headed")
        self.manager.playwright = SimpleNamespace(  # type: ignore[assignment]
            chromium=SimpleNamespace(launch=AsyncMock(return_value=headed))
        )
        source = (await self.manager.create_session(name="source"))["id"]
        self.manager.sessions[source].context.storage_state = AsyncMock(return_value={})

        shadow_id = (await self.manager.enable_shadow_browse(source))["shadow_session_id"]
        await self.manager.close_session(shadow_id)

        headed.close.assert_awaited_once()
        self.original.close.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

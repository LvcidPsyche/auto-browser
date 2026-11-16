"""CONNECT_AT_STARTUP=false starts the controller without connecting to the shared browser-node.

The root (Glama) image runs the controller with no browser-node, only so that an MCP client can list its tools. Its
entrypoint set SESSION_ISOLATION_MODE=glama_inspect, a value no code named, which skipped the startup connect only
because it was not shared_browser_node; since 1.15.0 refuses an unknown mode, that image has failed at import. The
setting says what it means: with it false, the connect waits for the first call that needs the browser.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.browser_manager import BrowserManager
from app.config import Settings


def _manager(**settings: object) -> SimpleNamespace:
    """What BrowserManager.startup reads from its instance, each store's startup a mock."""
    store = SimpleNamespace(startup=AsyncMock(), mark_all_active_interrupted=AsyncMock())
    return SimpleNamespace(
        settings=SimpleNamespace(witness_enabled=False, **settings),
        approvals=SimpleNamespace(startup=AsyncMock(), prune_decided=AsyncMock()),
        audit=SimpleNamespace(startup=AsyncMock()),
        witness=SimpleNamespace(startup=AsyncMock()),
        witness_remote=SimpleNamespace(startup=AsyncMock()),
        session_store=store,
        memory=None,
        tunnel_broker=SimpleNamespace(startup=AsyncMock()),
        runtime_provisioner=SimpleNamespace(startup=AsyncMock()),
        ensure_browser=AsyncMock(),
    )


class ConnectAtStartupTests(unittest.IsolatedAsyncioTestCase):
    async def _start(self, manager: SimpleNamespace) -> None:
        playwright = MagicMock()
        playwright.return_value.start = AsyncMock()
        with (
            patch("app.browser_manager.async_playwright", playwright),
            patch("app.browser_manager.process_hardening.confine_playwright_driver_environment"),
        ):
            await BrowserManager.startup(manager)  # type: ignore[arg-type]

    def test_the_setting_is_read_from_the_environment_and_defaults_to_true(self) -> None:
        self.assertTrue(Settings(_env_file=None).connect_at_startup)
        self.assertFalse(Settings(_env_file=None, CONNECT_AT_STARTUP="false").connect_at_startup)

    async def test_the_shared_browser_node_is_connected_at_startup_by_default(self) -> None:
        manager = _manager(session_isolation_mode="shared_browser_node", connect_at_startup=True)
        await self._start(manager)
        manager.ensure_browser.assert_awaited_once_with()

    async def test_with_the_setting_false_startup_connects_to_nothing(self) -> None:
        manager = _manager(session_isolation_mode="shared_browser_node", connect_at_startup=False)
        await self._start(manager)
        manager.ensure_browser.assert_not_awaited()
        manager.runtime_provisioner.startup.assert_awaited_once_with()  # the rest of the startup still ran

    async def test_docker_ephemeral_connects_to_no_shared_node_at_startup_either_way(self) -> None:
        manager = _manager(session_isolation_mode="docker_ephemeral", connect_at_startup=True)
        await self._start(manager)
        manager.ensure_browser.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

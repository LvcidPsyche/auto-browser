from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.cdp.passthrough import CDPPassthrough
from app.routes.extensions.cdp import _get_cdp


class FakeCdpSession:
    def __init__(self) -> None:
        self.detached = False

    async def send(self, method: str, params: dict | None = None) -> dict:
        return {}

    async def detach(self) -> None:
        self.detached = True


class FakeContext:
    def __init__(self) -> None:
        self.created: list[tuple[object, FakeCdpSession]] = []

    async def new_cdp_session(self, page) -> FakeCdpSession:
        session = FakeCdpSession()
        self.created.append((page, session))
        return session


class FakePage:
    def __init__(self, context: FakeContext) -> None:
        self.context = context


class CdpActiveTabTests(unittest.IsolatedAsyncioTestCase):
    async def test_cdp_follows_the_sessions_active_tab(self) -> None:
        """The CDP session stayed on the first tab: after a tab switch the
        routes inspected the wrong page, and after closing it every call failed."""
        context = FakeContext()
        first, second = FakePage(context), FakePage(context)
        browser_session = SimpleNamespace(page=first)
        app = SimpleNamespace(
            state=SimpleNamespace(
                cdp_sessions={"session-1": await CDPPassthrough.from_page(first)},
                browser_manager=SimpleNamespace(sessions={"session-1": browser_session}),
            )
        )

        self.assertIs((await _get_cdp(app, "session-1")).page, first)
        self.assertEqual(len(context.created), 1)

        browser_session.page = second
        cdp = await _get_cdp(app, "session-1")

        self.assertIs(cdp.page, second)
        self.assertIs(app.state.cdp_sessions["session-1"], cdp)
        self.assertTrue(context.created[0][1].detached)
        self.assertIs((await _get_cdp(app, "session-1")), cdp)
        self.assertEqual(len(context.created), 2)

    async def test_unknown_session_has_no_cdp(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace(cdp_sessions={}))
        self.assertIsNone(await _get_cdp(app, "missing"))


if __name__ == "__main__":
    unittest.main()

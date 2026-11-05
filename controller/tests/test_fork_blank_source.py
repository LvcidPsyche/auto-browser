"""A fork of a page that is no http(s) URL opens no start URL (AUTH-09)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

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

    async def test_auth09_a_blank_source_forks_with_no_start_url(self) -> None:
        self.session.page.url = "about:blank"

        await self.fork()

        self.assertIsNone(self.created.await_args.kwargs["start_url"])

    async def test_auth09_a_data_url_source_forks_with_no_start_url(self) -> None:
        self.session.page.url = "data:text/html,<p>hi</p>"

        await self.fork()

        self.assertIsNone(self.created.await_args.kwargs["start_url"])

    async def test_auth09_the_audit_records_no_start_url(self) -> None:
        self.session.page.url = "about:blank"
        self.manager.audit.append = AsyncMock()  # type: ignore[method-assign]

        await self.fork()

        self.assertIsNone(self.manager.audit.append.await_args.kwargs["details"]["start_url"])

    async def test_auth09_a_named_start_url_is_still_passed(self) -> None:
        self.session.page.url = "about:blank"

        await self.fork(start_url="https://example.com/next")

        self.assertEqual(self.created.await_args.kwargs["start_url"], "https://example.com/next")


if __name__ == "__main__":
    unittest.main()

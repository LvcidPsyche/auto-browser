"""close_session runs its teardown to the end, and says so when a step failed (NAV-36, NAV-37, AUTH-20)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.session_ownership import acting_as
from tests.test_auth_state_ownership import ALICE, build_manager


class CloseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _ = build_manager(Path(self.tmp.name))
        self.manager.settings.max_sessions = 5
        with acting_as(ALICE):
            self.session_id = (await self.manager.create_session(name="alice"))["id"]
        self.session = self.manager.sessions[self.session_id]
        self.audit = AsyncMock(wraps=self.manager.audit.append)
        self.manager.audit.append = self.audit  # type: ignore[method-assign]
        self.receipt = AsyncMock()
        self.manager._record_witness_receipt = self.receipt  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def closed_audit(self):
        calls = [c for c in self.audit.await_args_list if c.kwargs.get("event_type") == "session_closed"]
        self.assertEqual(len(calls), 1)
        return calls[0].kwargs

    async def close(self):
        with acting_as(ALICE):
            return await self.manager.close_session(self.session_id)

    async def test_a_clean_close_is_closed_and_ok(self) -> None:
        result = await self.close()

        self.assertTrue(result["closed"])
        self.assertEqual(result["failed_steps"], [])
        self.assertEqual(self.closed_audit()["status"], "ok")
        self.assertEqual(self.receipt.await_args.kwargs["status"], "ok")

    async def test_nav37_a_failed_context_close_is_partial_and_named(self) -> None:
        self.session.context.close = AsyncMock(side_effect=RuntimeError("Target closed"))

        result = await self.close()

        self.assertFalse(result["closed"])
        self.assertEqual(result["failed_steps"], ["close browser context"])
        self.assertNotIn(self.session_id, self.manager.sessions)
        self.assertEqual(result["session"]["status"], "closed")

    async def test_auth20_the_audit_event_and_witness_receipt_say_partial_with_the_steps(self) -> None:
        self.session.context.close = AsyncMock(side_effect=RuntimeError("Target closed"))

        await self.close()

        audit = self.closed_audit()
        self.assertEqual(audit["status"], "partial")
        self.assertEqual(audit["details"]["failed_steps"], ["close browser context"])
        receipt = self.receipt.await_args.kwargs
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["metadata"]["failed_steps"], ["close browser context"])

    async def test_nav37_a_failed_tunnel_release_is_named(self) -> None:
        self.session.tunnel = SimpleNamespace()
        self.manager.tunnel_broker.release = AsyncMock(side_effect=RuntimeError("ssh gone"))  # type: ignore[method-assign]

        result = await self.close()

        self.assertFalse(result["closed"])
        self.assertEqual(result["failed_steps"], ["release session tunnel"])

    async def test_nav37_an_owned_browser_that_will_not_close_is_named(self) -> None:
        self.session.owns_browser = True
        self.session.browser = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("hung")))

        result = await self.close()

        self.assertFalse(result["closed"])
        self.assertEqual(result["failed_steps"], ["close isolated browser"])

    async def test_nav36_a_cancel_mid_teardown_still_records_the_close(self) -> None:
        started = asyncio.Event()
        proceed = asyncio.Event()

        async def slow_close() -> None:
            started.set()
            await proceed.wait()

        self.session.context.close = slow_close

        with acting_as(ALICE):
            task = asyncio.create_task(self.manager.close_session(self.session_id))
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            await asyncio.sleep(0)
            proceed.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertNotIn(self.session_id, self.manager.sessions)
        self.assertEqual(self.closed_audit()["status"], "ok")
        self.receipt.assert_awaited_once()
        record = await self.manager.session_store.get(self.session_id)
        self.assertEqual(record.status, "closed")
        self.assertFalse(self.session.lock.locked())


if __name__ == "__main__":
    unittest.main()

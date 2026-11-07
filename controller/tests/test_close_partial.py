"""close_session runs its teardown to the end, and says so when a step failed (NAV-36, NAV-37, AUTH-20)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.browser.services import sessions as sessions_module
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
        self.assertEqual(result["teardown"], "complete")
        self.assertEqual(result["failed_steps"], [])
        self.assertEqual(self.closed_audit()["status"], "ok")
        self.assertEqual(self.receipt.await_args.kwargs["status"], "ok")

    async def test_nav37_a_failed_context_close_is_partial_and_named(self) -> None:
        self.session.context.close = AsyncMock(side_effect=RuntimeError("Target closed"))

        result = await self.close()

        # B-fix-9: the session is gone, so closed; the failed step makes the teardown partial.
        self.assertTrue(result["closed"])
        self.assertEqual(result["teardown"], "partial")
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

        self.assertTrue(result["closed"])
        self.assertEqual(result["teardown"], "partial")
        self.assertEqual(result["failed_steps"], ["release session tunnel"])

    async def test_nav37_an_owned_browser_that_will_not_close_is_named(self) -> None:
        self.session.owns_browser = True
        self.session.browser = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("hung")))

        result = await self.close()

        self.assertTrue(result["closed"])
        self.assertEqual(result["teardown"], "partial")
        self.assertEqual(result["failed_steps"], ["close isolated browser"])

    async def test_r5c_11_a_gate_release_that_raises_after_the_browser_closed_does_not_stop_the_close(self) -> None:
        # The gate release after the owned browser's close sat outside every try: its error escaped the close, and the
        # audit event, the witness receipt and the closed record that follow the teardown were never written.
        self.session.owns_browser = True
        self.session.browser = SimpleNamespace(close=AsyncMock())
        self.manager.request_gates.release = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("detach failed")
        )

        result = await self.close()

        self.assertTrue(result["closed"])
        self.assertEqual(result["teardown"], "complete")  # the browser closed; its gate's session went with it
        self.assertEqual(self.closed_audit()["status"], "ok")
        self.receipt.assert_awaited_once()
        self.assertEqual((await self.manager.session_store.get(self.session_id)).status, "closed")

    async def test_r5c_15_a_closed_hook_cancelled_on_its_own_does_not_stop_the_close_being_recorded(self) -> None:
        # Codex review of R5C-15: the session-closed hook was awaited with `except Exception` only, so a hook whose own
        # future was cancelled ended the close before its audit event, receipt and closed record.
        self.manager._session_closed_hook = AsyncMock(side_effect=asyncio.CancelledError())

        with self.assertLogs("app.browser.services", "WARNING") as logs:
            result = await self.close()

        self.assertTrue(result["closed"])
        self.assertEqual(result["teardown"], "complete")  # the hook is the extensions', not the session's teardown
        self.closed_audit()
        self.receipt.assert_awaited_once()
        self.assertEqual((await self.manager.session_store.get(self.session_id)).status, "closed")
        self.assertIn("session-closed hook", "\n".join(logs.output))

    async def test_r5c_15_a_closed_hook_that_never_returns_does_not_hold_the_close(self) -> None:
        # The hook was awaited unbounded, and a close runs to completion whatever happens to its caller, so a hook that
        # never returned held the close (and its audit event, receipt and record) for ever.
        let_go = asyncio.Event()

        async def stalled(_session_id: str) -> None:
            await let_go.wait()

        self.manager._session_closed_hook = stalled

        with patch.object(sessions_module, "TEARDOWN_STEP_TIMEOUT_SECONDS", 0.2), acting_as(ALICE):
            task = asyncio.create_task(self.manager.close_session(self.session_id))
            done, _ = await asyncio.wait({task}, timeout=5)  # not wait_for: a cancel does not stop a close
            let_go.set()
            result = await task

        self.assertIn(task, done, "the close waited for the hook")
        self.assertTrue(result["closed"])
        self.receipt.assert_awaited_once()
        self.assertEqual((await self.manager.session_store.get(self.session_id)).status, "closed")

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

    async def test_a_cancel_during_the_busy_paths_second_lock_wait_still_records_the_close(self) -> None:
        # R-14: the lock waits and the busy path's context close ran in the caller, outside the protected task, so a
        # cancel there left a registered session with a closed context and no record, audit event or receipt.
        holding, let_go = asyncio.Event(), asyncio.Event()

        async def hold_the_lock() -> None:
            async with self.session.lock:
                holding.set()
                await let_go.wait()

        context_closed = asyncio.Event()
        real_context_close = self.session.context.close

        async def close_context() -> None:
            context_closed.set()
            await real_context_close()

        self.session.context.close = close_context
        holder = asyncio.create_task(hold_the_lock())
        await holding.wait()
        with patch.object(sessions_module, "CLOSE_LOCK_WAIT_SECONDS", 0.5), acting_as(ALICE):
            task = asyncio.create_task(self.manager.close_session(self.session_id))
            await asyncio.wait_for(context_closed.wait(), timeout=5)
            await asyncio.sleep(0.05)  # in the second lock wait
            task.cancel()
            await asyncio.sleep(0)
            let_go.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await holder

        self.assertNotIn(self.session_id, self.manager.sessions)
        self.closed_audit()  # exactly one session_closed event
        self.receipt.assert_awaited_once()
        record = await self.manager.session_store.get(self.session_id)
        self.assertEqual(record.status, "closed")
        self.assertFalse(self.session.lock.locked())


if __name__ == "__main__":
    unittest.main()

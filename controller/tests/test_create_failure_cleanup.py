"""A create that fails leaves no directories and no active record (NAV-35)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.test_auth_state_ownership import build_manager

SESSION_ID = "abcdef012345"


class CreateFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager, _ = build_manager(self.root)
        patcher = patch("app.browser.services.session_creation.new_session_id", return_value=SESSION_ID)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def leftovers(self) -> list[str]:
        return [
            str(path.relative_to(self.root))
            for name in ("artifacts", "auth", "uploads")
            if (path := self.root / name / SESSION_ID).exists()
        ]

    async def test_a_refused_request_creates_no_directories(self) -> None:
        # No PROXY_PERSONA_FILE: refused after the id is minted, before any browser opens.
        with self.assertRaises(RuntimeError):
            await self.manager.create_session(proxy_persona="nobody")

        self.assertEqual(self.leftovers(), [])

    async def test_a_missing_auth_profile_creates_no_directories(self) -> None:
        with self.assertRaises(Exception):
            await self.manager.create_session(auth_profile="no-such-profile")

        self.assertEqual(self.leftovers(), [])

    async def test_a_browser_that_will_not_start_removes_the_directories(self) -> None:
        self.manager._acquire_session_browser = AsyncMock(side_effect=RuntimeError("no browser"))  # type: ignore[method-assign]

        with self.assertRaises(RuntimeError):
            await self.manager.create_session()

        self.assertEqual(self.leftovers(), [])

    async def test_w4_23_the_rollback_removes_the_directories_in_a_worker_thread(self) -> None:
        # shutil.rmtree ran on the event loop, so rolling back a session with large directories stalled every request.
        import asyncio

        from app.browser.services import session_creation

        self.manager._acquire_session_browser = AsyncMock(side_effect=RuntimeError("no browser"))  # type: ignore[method-assign]
        real_to_thread = asyncio.to_thread
        offloaded: list[str] = []

        async def to_thread(function, /, *args, **kwargs):
            offloaded.append(getattr(function, "__name__", repr(function)))
            return await real_to_thread(function, *args, **kwargs)

        with patch.object(session_creation.asyncio, "to_thread", to_thread, create=True):
            with self.assertRaises(RuntimeError):
                await self.manager.create_session()

        self.assertIn("remove_session_dirs", offloaded)
        self.assertEqual(self.leftovers(), [])

    async def test_a_failure_after_the_first_record_leaves_no_active_record(self) -> None:
        self.manager.witness_bridge.record_session_receipt = AsyncMock(side_effect=RuntimeError("witness down"))  # type: ignore[method-assign]

        with self.assertRaises(RuntimeError):
            await self.manager.create_session()

        self.assertEqual(self.leftovers(), [])
        self.assertNotIn(SESSION_ID, self.manager.sessions)
        try:
            record = await self.manager.session_store.get(SESSION_ID)
        except KeyError:
            return
        self.assertEqual(record.status, "closed")
        self.assertFalse(record.live)

    async def test_w8_2_a_failed_create_whose_record_cannot_be_parsed_ends_closed(self) -> None:
        # The rollback's pre-read returned on any error but KeyError, so a record it could not parse stayed as it was.
        from app.utils import record_path

        path = record_path(self.manager.session_store.file_store.root, SESSION_ID, ".json")

        async def corrupt_then_fail(*_args, **_kwargs):
            path.write_text("{not json", encoding="utf-8")
            raise RuntimeError("witness down")

        self.manager.witness_bridge.record_session_receipt = AsyncMock(  # type: ignore[method-assign]
            side_effect=corrupt_then_fail
        )

        with self.assertRaises(RuntimeError):
            await self.manager.create_session()

        record = await self.manager.session_store.get(SESSION_ID)
        self.assertEqual((record.status, record.close_reason, record.live), ("closed", "create_failed", False))

    async def test_w10_6_a_directory_check_that_raises_does_not_stop_the_rollback(self) -> None:
        # Path.exists raises for EACCES or EIO on Python 3.11: the rollback skipped the other directories and the
        # record close, and the caller saw that error instead of the create's own.
        artifacts = self.root / "artifacts" / SESSION_ID
        armed = False
        real_exists = Path.exists

        async def fail(*_args, **_kwargs):
            nonlocal armed
            armed = True
            raise RuntimeError("witness down")

        def exists(path, *args, **kwargs):
            if armed and path == artifacts:
                raise PermissionError(13, "Permission denied", str(path))
            return real_exists(path, *args, **kwargs)

        self.manager.witness_bridge.record_session_receipt = AsyncMock(side_effect=fail)  # type: ignore[method-assign]
        with patch.object(Path, "exists", exists):
            with self.assertLogs("app.browser.services.session_creation", "WARNING") as logs:
                with self.assertRaisesRegex(RuntimeError, "witness down"):
                    await self.manager.create_session()

        self.assertTrue(armed, "the create never reached the witness receipt")
        self.assertEqual(self.leftovers(), [])
        record = await self.manager.session_store.get(SESSION_ID)
        self.assertEqual((record.status, record.close_reason, record.live), ("closed", "create_failed", False))
        self.assertTrue(
            any(str(artifacts) in line and "Permission denied" in line for line in logs.output), logs.output
        )

    async def test_w11_5_a_removal_error_inside_rmtree_is_logged_with_its_error_and_the_rollback_goes_on(self) -> None:
        # rmtree(ignore_errors=True) dropped the error, so the warning named the directory without it (W10-6 asked
        # for both). One file in the artifacts tree cannot be unlinked; the rest of that tree, the other directories
        # and the record close still run.
        import os

        artifacts = self.root / "artifacts" / SESSION_ID
        armed = False
        real_unlink = os.unlink

        async def fail(*_args, **_kwargs):
            nonlocal armed
            armed = True
            (artifacts / "stuck.bin").write_bytes(b"x")
            (artifacts / "other.bin").write_bytes(b"x")
            raise RuntimeError("witness down")

        def unlink(path, *args, **kwargs):
            if armed and os.path.basename(os.fsdecode(path)) == "stuck.bin":
                raise PermissionError(13, "Permission denied", os.fsdecode(path))
            return real_unlink(path, *args, **kwargs)

        self.manager.witness_bridge.record_session_receipt = AsyncMock(side_effect=fail)  # type: ignore[method-assign]
        with patch("os.unlink", unlink):
            with self.assertLogs("app.browser.services.session_creation", "WARNING") as logs:
                with self.assertRaisesRegex(RuntimeError, "witness down"):
                    await self.manager.create_session()

        self.assertTrue(armed, "the create never reached the witness receipt")
        self.assertEqual(self.leftovers(), [f"artifacts{os.sep}{SESSION_ID}"])
        self.assertEqual(sorted(path.name for path in artifacts.iterdir()), ["stuck.bin"])
        record = await self.manager.session_store.get(SESSION_ID)
        self.assertEqual((record.status, record.close_reason, record.live), ("closed", "create_failed", False))
        # W12-4: one warning for the directory, naming its first error (the unlink's), not the parent rmdir's
        # "Directory not empty" too, nor an error-less line from the check that it is still there.
        naming = [line for line in logs.output if str(artifacts) in line]
        self.assertEqual(len(naming), 1, logs.output)
        self.assertIn("Permission denied", naming[0])

    async def test_w12_4_a_directory_never_created_is_not_logged(self) -> None:
        # rmtree reports a missing directory to its callback as FileNotFoundError (lstat on Linux; scandir and rmdir
        # on Windows); a path already gone is not an error, so no warning names it.
        import shutil

        uploads = self.root / "uploads" / SESSION_ID
        armed = False

        async def fail(*_args, **_kwargs):
            nonlocal armed
            armed = True
            shutil.rmtree(uploads)
            raise RuntimeError("witness down")

        self.manager.witness_bridge.record_session_receipt = AsyncMock(side_effect=fail)  # type: ignore[method-assign]
        with self.assertNoLogs("app.browser.services.session_creation", "WARNING"):
            with self.assertRaisesRegex(RuntimeError, "witness down"):
                await self.manager.create_session()

        self.assertTrue(armed, "the create never reached the witness receipt")
        self.assertEqual(self.leftovers(), [])

    async def test_w5_6_a_cancel_during_the_rollback_still_closes_the_record_and_removes_the_directories(self) -> None:
        # The directory removal is awaited before the closed record is written: a second cancel there (a shutdown)
        # left a record the create had already persisted reading active.
        import asyncio
        import threading

        from app.browser.services.sessions import BrowserSessionService

        self.manager.witness_bridge.record_session_receipt = AsyncMock(side_effect=RuntimeError("witness down"))  # type: ignore[method-assign]
        started, go_on = threading.Event(), threading.Event()
        self.addCleanup(go_on.set)
        real = BrowserSessionService.remove_session_dirs

        def gated(service, session_id):
            started.set()
            go_on.wait(10)
            real(service, session_id)

        with patch.object(BrowserSessionService, "remove_session_dirs", gated):
            task = asyncio.create_task(self.manager.create_session())
            self.assertTrue(await asyncio.to_thread(started.wait, 10), "the rollback never removed the directories")
            self.assertEqual((await self.manager.session_store.get(SESSION_ID)).status, "active")
            task.cancel()
            await asyncio.sleep(0.05)
            go_on.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        record = await self.manager.session_store.get(SESSION_ID)
        self.assertEqual(record.status, "closed")
        self.assertEqual(record.close_reason, "create_failed")
        self.assertFalse(record.live)
        self.assertEqual(self.leftovers(), [])

    async def _cancel_while_rollback_waits_in(self, step: str) -> tuple[AsyncMock, int]:
        # W7-2: a failed create's rollback with an isolated runtime and a tunnel; the caller is cancelled while the
        # rollback waits in ``step``. Returns the runtime release and how many CancelledErrors the caller saw.
        import asyncio

        from app.session_isolation import IsolatedBrowserRuntime

        runtime = IsolatedBrowserRuntime(
            session_id=SESSION_ID,
            container_id="c0",
            container_name="browser-node-x",
            network_name="net",
            browser_node_name="browser-node-x",
            profile_dir=self.root / "profile",
            downloads_dir=self.root / "downloads",
            ws_endpoint_file=self.root / "ws",
            ws_endpoint="ws://127.0.0.1:1/",
            takeover_url="http://127.0.0.1:6080/",
            novnc_port=None,
        )
        browser = self.manager.browser
        browser.close = AsyncMock()  # type: ignore[attr-defined]
        self.manager._acquire_session_browser = AsyncMock(  # type: ignore[method-assign]
            return_value=(browser, runtime)
        )
        release_runtime = AsyncMock()
        self.manager.runtime_provisioner.release = release_runtime  # type: ignore[method-assign]

        async def provision(session) -> None:
            session.tunnel = object()

        self.manager._maybe_provision_session_tunnel = AsyncMock(side_effect=provision)  # type: ignore[method-assign]
        self.manager.tunnel_broker.release = AsyncMock()  # type: ignore[method-assign]
        self.manager.witness_bridge.record_session_receipt = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("witness down")
        )
        reached, go_on = asyncio.Event(), asyncio.Event()

        async def gated(*_args, **_kwargs) -> None:
            reached.set()
            await go_on.wait()

        if step == "context close":
            real_new_context = browser.new_context.side_effect

            def new_context(**kwargs):
                context = real_new_context(**kwargs)
                context.close = AsyncMock(side_effect=gated)
                return context

            browser.new_context = AsyncMock(side_effect=new_context)
        else:
            self.manager.tunnel_broker.release = AsyncMock(side_effect=gated)  # type: ignore[method-assign]

        task = asyncio.create_task(self.manager.create_session())
        await asyncio.wait_for(reached.wait(), 10)
        self.assertEqual((await self.manager.session_store.get(SESSION_ID)).status, "active")
        task.cancel()
        await asyncio.sleep(0.05)
        go_on.set()
        cancels = 0
        try:
            await asyncio.wait_for(asyncio.shield(task), 10)
        except asyncio.CancelledError:
            cancels += 1
        self.assertTrue(task.done())
        return release_runtime, cancels

    async def _assert_rolled_back(self, release_runtime: AsyncMock, cancels: int) -> None:
        self.assertEqual(cancels, 1)
        release_runtime.assert_awaited_once()
        self.assertEqual(self.leftovers(), [])
        record = await self.manager.session_store.get(SESSION_ID)
        self.assertEqual(record.status, "closed")
        self.assertEqual(record.close_reason, "create_failed")

    async def test_w7_2_a_cancel_during_the_context_close_still_finishes_the_rollback(self) -> None:
        await self._assert_rolled_back(*await self._cancel_while_rollback_waits_in("context close"))

    async def test_w7_2_a_cancel_during_the_tunnel_release_still_finishes_the_rollback(self) -> None:
        await self._assert_rolled_back(*await self._cancel_while_rollback_waits_in("tunnel release"))

    async def _create_whose_context_close_never_returns(self, *, cancel: bool) -> tuple[AsyncMock, int, list[str]]:
        # W9-3: none of the rollback's awaits was bounded, so a context.close() that stalled held the runtime release,
        # the directory removal and the record write, and a cancelled caller waited with it. Returns the runtime
        # release, how many CancelledErrors the caller saw, and the rollback's warnings.
        import asyncio

        from app.browser.services import sessions as sessions_module
        from app.session_isolation import IsolatedBrowserRuntime

        runtime = IsolatedBrowserRuntime(
            session_id=SESSION_ID,
            container_id="c0",
            container_name="browser-node-x",
            network_name="net",
            browser_node_name="browser-node-x",
            profile_dir=self.root / "profile",
            downloads_dir=self.root / "downloads",
            ws_endpoint_file=self.root / "ws",
            ws_endpoint="ws://127.0.0.1:1/",
            takeover_url="http://127.0.0.1:6080/",
            novnc_port=None,
        )
        browser = self.manager.browser
        browser.close = AsyncMock()  # type: ignore[attr-defined]
        self.manager._acquire_session_browser = AsyncMock(  # type: ignore[method-assign]
            return_value=(browser, runtime)
        )
        release_runtime = AsyncMock()
        self.manager.runtime_provisioner.release = release_runtime  # type: ignore[method-assign]
        self.manager.witness_bridge.record_session_receipt = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("witness down")
        )
        reached, never = asyncio.Event(), asyncio.Event()

        async def stalled(*_args, **_kwargs) -> None:
            reached.set()
            await never.wait()

        real_new_context = browser.new_context.side_effect

        def new_context(**kwargs):
            context = real_new_context(**kwargs)
            context.close = AsyncMock(side_effect=stalled)
            return context

        browser.new_context = AsyncMock(side_effect=new_context)
        cancels = 0
        with (
            patch.object(sessions_module, "TEARDOWN_STEP_TIMEOUT_SECONDS", 0.2),
            self.assertLogs("app.browser.services", level="WARNING") as logs,
        ):
            task = asyncio.create_task(self.manager.create_session())
            await asyncio.wait_for(reached.wait(), 10)
            if cancel:
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), 10)
            except asyncio.CancelledError:
                cancels += 1
            except RuntimeError:
                pass
        self.assertTrue(task.done())
        return release_runtime, cancels, logs.output

    async def _assert_rolled_back_past_the_stalled_close(self, release_runtime, logs: list[str]) -> None:
        release_runtime.assert_awaited_once()
        self.assertEqual(self.leftovers(), [])
        record = await self.manager.session_store.get(SESSION_ID)
        self.assertEqual((record.status, record.close_reason), ("closed", "create_failed"))
        self.assertEqual(len([line for line in logs if "context" in line]), 1, logs)
        # W10-17: the rollback's lines name it, so an operator can tell them from a close_session's.
        self.assertIn(
            f"gave up waiting to close the browser context of a failed create for session {SESSION_ID}",
            "\n".join(logs),
        )

    async def test_w9_3_a_context_close_that_never_returns_does_not_hold_the_rollback(self) -> None:
        release_runtime, cancels, logs = await self._create_whose_context_close_never_returns(cancel=False)
        self.assertEqual(cancels, 0)
        await self._assert_rolled_back_past_the_stalled_close(release_runtime, logs)

    async def test_w9_3_a_cancel_during_a_stalled_context_close_is_raised_once_at_the_end(self) -> None:
        release_runtime, cancels, logs = await self._create_whose_context_close_never_returns(cancel=True)
        self.assertEqual(cancels, 1)
        await self._assert_rolled_back_past_the_stalled_close(release_runtime, logs)

    def _isolated_create_that_fails(self) -> AsyncMock:
        """A create on an isolated runtime whose witness receipt fails; returns the runtime release."""
        from app.session_isolation import IsolatedBrowserRuntime

        runtime = IsolatedBrowserRuntime(
            session_id=SESSION_ID,
            container_id="c0",
            container_name="browser-node-x",
            network_name="net",
            browser_node_name="browser-node-x",
            profile_dir=self.root / "profile",
            downloads_dir=self.root / "downloads",
            ws_endpoint_file=self.root / "ws",
            ws_endpoint="ws://127.0.0.1:1/",
            takeover_url="http://127.0.0.1:6080/",
            novnc_port=None,
        )
        browser = self.manager.browser
        browser.close = AsyncMock()  # type: ignore[attr-defined]
        self.manager._acquire_session_browser = AsyncMock(return_value=(browser, runtime))  # type: ignore[method-assign]
        release_runtime = AsyncMock()
        self.manager.runtime_provisioner.release = release_runtime  # type: ignore[method-assign]
        self.manager.witness_bridge.record_session_receipt = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("witness down")
        )
        return release_runtime

    async def test_r5c_11_a_gate_release_that_raises_does_not_stop_a_failed_creates_rollback(self) -> None:
        # The owned browser's gate release sat outside every try: raising there skipped the runtime release, the
        # directory removal and the record close, and the caller got the release's error instead of its own.
        release_runtime = self._isolated_create_that_fails()
        self.manager.request_gates.release = AsyncMock(side_effect=RuntimeError("detach failed"))  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "witness down"):
            await self.manager.create_session()

        release_runtime.assert_awaited_once()
        self.assertEqual(self.leftovers(), [])
        record = await self.manager.session_store.get(SESSION_ID)
        self.assertEqual((record.status, record.close_reason), ("closed", "create_failed"))

    async def test_r5c_15_a_step_cancelled_on_its_own_does_not_end_a_failed_creates_rollback(self) -> None:
        # _teardown_step caught Exception only: a context close whose own future was cancelled (the rollback's task
        # was not) ended the rollback with a CancelledError, so the runtime was not released, the record not closed,
        # and a caller that was never cancelled got a CancelledError.
        import asyncio

        release_runtime = self._isolated_create_that_fails()
        browser = self.manager.browser
        real_new_context = browser.new_context.side_effect

        def new_context(**kwargs):
            context = real_new_context(**kwargs)
            context.close = AsyncMock(side_effect=asyncio.CancelledError())
            return context

        browser.new_context = AsyncMock(side_effect=new_context)

        with self.assertLogs("app.browser.services", "WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "witness down"):
                await self.manager.create_session()

        release_runtime.assert_awaited_once()
        self.assertEqual(self.leftovers(), [])
        record = await self.manager.session_store.get(SESSION_ID)
        self.assertEqual((record.status, record.close_reason), ("closed", "create_failed"))
        self.assertIn(
            f"close the browser context of a failed create for session {SESSION_ID} was cancelled on its own",
            "\n".join(logs.output),
        )

    async def test_r5c_15_an_owned_browser_close_cancelled_on_its_own_does_not_end_the_rollback(self) -> None:
        # Codex review of R5C-15: the owned browser's close sat in `except Exception` only, so its own cancellation
        # still ended the rollback before the runtime release and the record close.
        import asyncio

        release_runtime = self._isolated_create_that_fails()
        self.manager.browser.close = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[attr-defined]

        with self.assertRaisesRegex(RuntimeError, "witness down"):
            await self.manager.create_session()

        release_runtime.assert_awaited_once()
        self.assertEqual(self.leftovers(), [])
        record = await self.manager.session_store.get(SESSION_ID)
        self.assertEqual((record.status, record.close_reason), ("closed", "create_failed"))

    async def test_r5c_15_a_cancel_of_the_task_running_a_teardown_step_is_still_raised(self) -> None:
        import asyncio

        from app.browser.services.sessions import BrowserSessionService

        started = asyncio.Event()

        async def step() -> None:
            started.set()
            await asyncio.Event().wait()

        failed: list[str] = []
        task = asyncio.create_task(BrowserSessionService._teardown_step(SESSION_ID, "close a thing", step, failed))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(failed, [])

    async def test_r5c_15_a_step_cancelled_on_its_own_is_a_failed_step(self) -> None:
        import asyncio

        from app.browser.services.sessions import BrowserSessionService

        async def step() -> None:
            raise asyncio.CancelledError()

        failed: list[str] = []
        with self.assertLogs("app.browser.services", "WARNING"):
            done = await BrowserSessionService._teardown_step(SESSION_ID, "close a thing", step, failed)

        self.assertFalse(done)
        self.assertEqual(failed, ["close a thing"])

    def _dangling_link_at_the_artifacts_path(self) -> Path:
        from tests.test_session_isolation import _link_dir

        artifacts = self.root / "artifacts" / SESSION_ID
        target = self.root / "gone"
        target.mkdir()
        artifacts.parent.mkdir(parents=True, exist_ok=True)
        _link_dir(artifacts, target)
        target.rmdir()
        return artifacts

    @unittest.skipIf(os.name == "nt", "Windows' rmtree refuses a link outright, which the callback reports")
    async def test_r5c_22_a_dangling_symlink_at_a_session_path_is_reported(self) -> None:
        # POSIX rmtree's lstat passes for a dangling symlink and its open fails with FileNotFoundError, which the
        # callback took for a path already gone; the check after it follows links, so the link left was never named.
        artifacts = self._dangling_link_at_the_artifacts_path()

        with self.assertLogs("app.browser.services.session_creation", "WARNING") as logs:
            self.manager.session_lifecycle.remove_session_dirs(SESSION_ID)

        self.assertEqual([line for line in logs.output if str(artifacts) in line], [logs.output[0]], logs.output)
        self.assertTrue(os.path.lexists(artifacts))  # a link is never removed through, as a link to a live dir is not

    async def test_r5c_22_an_entry_left_after_a_not_found_error_is_reported(self) -> None:
        # The same on every platform: rmtree as POSIX runs it on a dangling link (lstat passes, open fails with
        # FileNotFoundError, the entry stays), over a real dangling link (a junction where Windows refuses symlinks).
        from app.browser.services import session_creation

        artifacts = self._dangling_link_at_the_artifacts_path()

        def rmtree_as_posix_on_a_dangling_link(path, *, onexc=None, onerror=None) -> None:
            error = FileNotFoundError(2, "No such file or directory", str(path))
            if onexc is not None:
                onexc(os.open, path, error)
            else:
                onerror(os.open, path, (FileNotFoundError, error, None))

        with patch.object(session_creation.shutil, "rmtree", rmtree_as_posix_on_a_dangling_link):
            with self.assertLogs("app.browser.services.session_creation", "WARNING") as logs:
                self.manager.session_lifecycle.remove_session_dirs(SESSION_ID)

        self.assertEqual([line for line in logs.output if str(artifacts) in line], [logs.output[0]], logs.output)
        self.assertEqual(len(logs.output), 1, logs.output)  # the auth and upload paths are not there: not named

    async def test_a_successful_create_keeps_its_directories(self) -> None:
        await self.manager.create_session()

        self.assertEqual(len(self.leftovers()), 3)


if __name__ == "__main__":
    unittest.main()

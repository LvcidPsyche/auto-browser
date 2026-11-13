"""The share page polls /share/{token}/observe every few seconds for as long as it is open.

It used to run a full observation (DOM, ARIA and OCR reads plus a screenshot kept
for good) under the session lock on each poll. These tests pin the cheap version:
one redacted frame, the URL and the title, the lock held for just that, and old
frames pruned.
"""

from __future__ import annotations

import asyncio
import gc
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import app.main as main_module
from app.browser.page_provenance import PageProvenance
from app.browser.services import observation as observation_module
from app.browser.services.observation import SHARE_FRAME_KEEP_SECONDS, SHARE_FRAME_LABEL, BrowserObservationService
from app.browser_scripts import AB_TITLE_MAX
from app.routes._utils import OFF_LIST_PAGE_DETAIL


class FakePage:
    url = "https://example.com/watched"

    def __init__(self, session_lock: list[asyncio.Lock]) -> None:
        self._session_lock = session_lock

    async def title(self) -> str:
        assert self._session_lock[0].locked(), "title must be read under the session lock"
        return "Watched"

    async def evaluate(self, script: object, *args: object) -> object:
        raise AssertionError("a share poll must not read the DOM")

    async def aria_snapshot(self, **_: object) -> str:
        raise AssertionError("a share poll must not read the accessibility tree")


def _session(artifact_dir: Path) -> SimpleNamespace:
    lock_holder: list[asyncio.Lock] = []
    session = SimpleNamespace(id="session-1", artifact_dir=artifact_dir, lock=asyncio.Lock(), share_frame=None)
    lock_holder.append(session.lock)
    session.page = FakePage(lock_holder)
    return session


def _write_frame(directory: Path, name: str, *, age_seconds: float = 0.0) -> Path:
    path = directory / name
    path.write_bytes(b"png")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def _manager(session: SimpleNamespace, *, scrubbing: bool = False) -> SimpleNamespace:
    counter = {"n": 0}

    async def capture(target: SimpleNamespace, label: str, *, staged: bool = False) -> dict[str, str]:
        assert target.lock.locked(), "the capture must run under the session lock"
        counter["n"] += 1
        path = _write_frame(target.artifact_dir, f"20260101T0000{counter['n']:02d}Z-{label}.png")
        return {"path": str(path), "url": f"/artifacts/{target.id}/{path.name}"}

    return SimpleNamespace(
        get_session=AsyncMock(return_value=session),
        _capture_screenshot=AsyncMock(side_effect=capture),
        _assert_page_allowed=MagicMock(),
        _page_refused=MagicMock(return_value=False),
        page_provenance=PageProvenance(),
        _session_summary=AsyncMock(),
        tabs=SimpleNamespace(summaries=AsyncMock()),
        pii_scrubber=SimpleNamespace(screenshot_enabled=scrubbing, audit_report=False),
        ocr=SimpleNamespace(extract_from_image=AsyncMock(return_value={"available": True, "blocks": []})),
    )


class ShareSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.session = _session(self.dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_reads_a_frame_the_url_and_the_title_and_nothing_else(self) -> None:
        manager = _manager(self.session)

        result = await BrowserObservationService(manager=manager).share_snapshot("session-1")

        self.assertEqual(result["url"], "https://example.com/watched")
        self.assertEqual(result["title"], "Watched")
        self.assertTrue(Path(result["screenshot_path"]).name.endswith(f"-{SHARE_FRAME_LABEL}.png"))
        manager.tabs.summaries.assert_not_awaited()
        manager._session_summary.assert_not_awaited()
        manager.ocr.extract_from_image.assert_not_awaited()
        self.assertFalse(self.session.lock.locked())

    async def test_a_megabyte_title_is_capped(self) -> None:
        self.session.page.title = AsyncMock(return_value="T" * 1_000_000)
        manager = _manager(self.session)

        result = await BrowserObservationService(manager=manager).share_snapshot("session-1")

        self.assertEqual(len(result["title"]), AB_TITLE_MAX)

    async def test_the_frame_is_redacted_when_screenshot_scrubbing_is_on(self) -> None:
        manager = _manager(self.session, scrubbing=True)

        result = await BrowserObservationService(manager=manager).share_snapshot("session-1")

        manager.ocr.extract_from_image.assert_awaited_once_with(result["screenshot_path"])

    async def test_the_lock_is_not_held_while_old_frames_are_pruned(self) -> None:
        manager = _manager(self.session)

        def prune(directory: Path, *, keep: Path) -> None:
            self.assertFalse(self.session.lock.locked(), "pruning must run after the lock is released")

        with patch.object(observation_module, "_prune_share_frames", side_effect=prune) as pruned:
            await BrowserObservationService(manager=manager).share_snapshot("session-1")

        pruned.assert_called_once()

    async def test_old_share_frames_are_pruned_and_nothing_else(self) -> None:
        stale = [
            _write_frame(
                self.dir, f"2026010100000{i}Z-{SHARE_FRAME_LABEL}.png", age_seconds=SHARE_FRAME_KEEP_SECONDS + 5
            )
            for i in range(5)
        ]
        recent = _write_frame(self.dir, f"20260101000010Z-{SHARE_FRAME_LABEL}.png", age_seconds=2)
        other = _write_frame(self.dir, "20260101000011Z-observe.png", age_seconds=SHARE_FRAME_KEEP_SECONDS + 500)
        manager = _manager(self.session)

        result = await BrowserObservationService(manager=manager).share_snapshot("session-1")

        self.assertFalse(any(path.exists() for path in stale))
        self.assertTrue(recent.exists())
        self.assertTrue(other.exists(), "frames from other captures are not the share page's to delete")
        self.assertTrue(Path(result["screenshot_path"]).exists())

    async def test_polling_does_not_accumulate_frames(self) -> None:
        manager = _manager(self.session)
        service = BrowserObservationService(manager=manager)
        # Every poll takes its own frame here: one that arrived within the reuse window would be answered the last.
        with (
            patch.object(observation_module, "SHARE_FRAME_KEEP_SECONDS", 0.0),
            patch.object(observation_module, "SHARE_FRAME_REUSE_SECONDS", 0.0),
        ):
            for _ in range(10):
                await service.share_snapshot("session-1")
                await asyncio.sleep(0.01)

        self.assertEqual(manager._capture_screenshot.await_count, 10)
        self.assertEqual(len(list(self.dir.glob(f"*-{SHARE_FRAME_LABEL}.png"))), 1)


class _Clock:
    """The observation module's clock: the wall clock, which pruning reads, and a monotonic one a test moves."""

    time = staticmethod(time.time)

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


async def _settled() -> None:
    """Let every task run as far as it can without the clock or anything the test holds back."""
    for _ in range(20):
        await asyncio.sleep(0)


class ShareFrameReuseTests(unittest.IsolatedAsyncioTestCase):
    """One capture answers every poll of a session that arrives while it is taken, or within the reuse window."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.session = _session(self.dir)
        self.manager = _manager(self.session)
        self.service = BrowserObservationService(manager=self.manager)
        self.clock = _Clock()
        patcher = patch.object(observation_module, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def captures(self) -> int:
        return self.manager._capture_screenshot.await_count

    def poll(self) -> asyncio.Future:
        return asyncio.ensure_future(self.service.share_snapshot("session-1"))

    def hold_captures(self, *, then_fail: bool = False) -> asyncio.Event:
        """Captures wait, under the session lock, until the event is set; then they end, or fail."""
        release = asyncio.Event()
        capture = self.manager._capture_screenshot.side_effect

        async def held(*args: object, **kwargs: object) -> dict[str, str]:
            await release.wait()
            if then_fail:
                raise RuntimeError("the browser went away")
            return await capture(*args, **kwargs)

        self.manager._capture_screenshot.side_effect = held
        return release

    async def test_a_poll_within_the_window_is_answered_from_the_frame_already_taken(self) -> None:
        first = await self.service.share_snapshot("session-1")
        self.clock.now += observation_module.SHARE_FRAME_REUSE_SECONDS - 0.01

        second = await self.service.share_snapshot("session-1")

        self.assertEqual(self.captures(), 1)
        self.assertEqual(second, first)
        self.assertIsNot(second, first)
        self.assertTrue(Path(second["screenshot_path"]).exists())

    async def test_a_poll_after_the_window_takes_a_new_frame(self) -> None:
        first = await self.service.share_snapshot("session-1")
        self.clock.now += observation_module.SHARE_FRAME_REUSE_SECONDS

        second = await self.service.share_snapshot("session-1")

        self.assertEqual(self.captures(), 2)
        self.assertNotEqual(second["screenshot_path"], first["screenshot_path"])

    async def test_polls_that_arrive_while_a_frame_is_taken_wait_for_that_one(self) -> None:
        release = self.hold_captures()
        polls = [self.poll() for _ in range(5)]
        await _settled()
        self.assertEqual((self.captures(), [poll.done() for poll in polls]), (1, [False] * 5))

        release.set()
        frames = await asyncio.gather(*polls)

        self.assertEqual(self.captures(), 1)
        self.assertEqual(len({frame["screenshot_path"] for frame in frames}), 1)
        self.assertFalse(self.session.lock.locked())

    async def test_a_viewer_that_goes_away_does_not_end_the_capture_the_others_wait_on(self) -> None:
        release = self.hold_captures()
        leaving, staying = self.poll(), self.poll()
        await _settled()

        # The first poll started the capture; it goes, and the capture stays the second's.
        leaving.cancel()
        await _settled()
        self.assertTrue(leaving.cancelled())
        release.set()
        frame = await staying

        self.assertEqual(self.captures(), 1)
        self.assertTrue(Path(frame["screenshot_path"]).exists())
        self.assertFalse(self.session.lock.locked())

    async def test_a_capture_every_viewer_left_is_finished_and_answers_the_next_poll(self) -> None:
        release = self.hold_captures()
        only = self.poll()
        await _settled()
        only.cancel()
        await _settled()

        release.set()
        frame = await self.service.share_snapshot("session-1")

        self.assertEqual(self.captures(), 1)
        self.assertTrue(Path(frame["screenshot_path"]).exists())
        self.assertFalse(self.session.lock.locked())

    async def test_a_capture_that_failed_answers_the_polls_of_its_window_and_is_not_taken_again_for_them(self) -> None:
        release = self.hold_captures(then_fail=True)
        polls = [self.poll() for _ in range(3)]
        await _settled()
        release.set()

        answers = await asyncio.gather(*polls, return_exceptions=True)

        self.assertEqual([type(answer) for answer in answers], [RuntimeError] * 3)
        with self.assertRaises(RuntimeError):
            await self.service.share_snapshot("session-1")
        self.assertEqual(self.captures(), 1)
        self.clock.now += observation_module.SHARE_FRAME_REUSE_SECONDS
        with self.assertRaises(RuntimeError):
            await self.service.share_snapshot("session-1")
        self.assertEqual(self.captures(), 2)

    async def test_the_failure_of_a_capture_nobody_waited_out_is_read(self) -> None:
        unread: list[dict[str, object]] = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unread.append(context))
        release = self.hold_captures(then_fail=True)
        only = self.poll()
        await _settled()
        only.cancel()
        await _settled()

        release.set()
        await _settled()
        # The session goes, and its capture with it: a failure nobody read is reported as the task is collected. The
        # cancelled poll goes too, since its cancellation's traceback still holds the capture.
        self.session.share_frame = None
        del only
        gc.collect()
        await _settled()

        self.assertEqual(unread, [])

    async def test_every_poll_looks_its_session_up_before_a_frame_is_reused(self) -> None:
        await self.service.share_snapshot("session-1")

        # Closed since, or not this caller's: the frame taken a moment ago is not its answer.
        self.manager.get_session.side_effect = KeyError("session-1")
        with self.assertRaises(KeyError):
            await self.service.share_snapshot("session-1")

        self.assertEqual((self.captures(), self.manager.get_session.await_count), (1, 2))

    async def test_the_frame_polls_are_answered_from_is_not_pruned(self) -> None:
        with patch.object(observation_module, "SHARE_FRAME_KEEP_SECONDS", 0.0):
            first = await self.service.share_snapshot("session-1")
            second = await self.service.share_snapshot("session-1")

        self.assertEqual(self.captures(), 1)
        self.assertEqual(list(self.dir.glob("*.png")), [Path(first["screenshot_path"])])
        self.assertEqual(second["screenshot_path"], first["screenshot_path"])


class ShareRouteTests(unittest.TestCase):
    """Through the real app: the poll must not run the full observation."""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self._tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.dir = Path(self._tmp)
        self.session = _session(self.dir)
        manager = _manager(self.session)
        self.stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        for service, method_name in (
            (main_module.manager, "startup"),
            (main_module.manager, "shutdown"),
            (main_module.job_queue, "startup"),
            (main_module.job_queue, "shutdown"),
            (main_module.cron_service, "startup"),
            (main_module.cron_service, "shutdown"),
            (main_module.maintenance, "startup"),
            (main_module.maintenance, "shutdown"),
        ):
            self.stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
        self.stack.enter_context(
            patch.object(
                main_module.share_manager, "token_info", return_value={"valid": True, "session_id": "session-1"}
            )
        )
        self.stack.enter_context(patch.object(main_module.manager, "get_session", new=manager.get_session))
        self.stack.enter_context(
            patch.object(main_module.manager, "_capture_screenshot", new=manager._capture_screenshot)
        )
        self.stack.enter_context(
            patch.object(main_module.manager, "_assert_page_allowed", new=manager._assert_page_allowed)
        )
        self.stack.enter_context(patch.object(main_module.manager.pii_scrubber, "screenshot_enabled", False))
        self.client = self.stack.enter_context(TestClient(main_module.app))

    def tearDown(self) -> None:
        self.stack.close()

    def test_shared_observe_answers_from_a_frame_without_a_full_observation(self) -> None:
        response = self.client.get("/share/tok/observe")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["url"], body["title"]), ("https://example.com/watched", "Watched"))
        self.assertRegex(body["screenshot_url"], rf"^/share/tok/screenshots/.*-{SHARE_FRAME_LABEL}\.png$")
        self.assertEqual(body["session"], {"id": "session-1"})
        self.assertFalse(self.session.lock.locked())

    def test_two_polls_a_moment_apart_cost_one_capture(self) -> None:
        answers = [self.client.get("/share/tok/observe"), self.client.get("/share/other-link/observe")]

        self.assertEqual([answer.status_code for answer in answers], [200, 200])
        self.assertEqual(main_module.manager._capture_screenshot.await_count, 1)
        # Each is answered the one frame under its own link.
        names = [answer.json()["screenshot_url"].rsplit("/", 1) for answer in answers]
        self.assertEqual([prefix for prefix, _ in names], ["/share/tok/screenshots", "/share/other-link/screenshots"])
        self.assertEqual(names[0][1], names[1][1])

    def test_a_page_refused_before_the_frame_answers_403_with_the_off_list_detail(self) -> None:
        main_module.manager._assert_page_allowed.side_effect = PermissionError("host 'evil.example' is not allowed")

        response = self.client.get("/share/tok/observe")

        self.assertEqual((response.status_code, response.json()), (403, {"detail": OFF_LIST_PAGE_DETAIL}))
        self.assertEqual(list(self.dir.glob("*.png")), [])

    def test_a_page_that_moved_while_the_frame_was_taken_answers_403_and_keeps_no_frame(self) -> None:
        with patch.object(main_module.manager, "_page_refused", return_value=True):
            response = self.client.get("/share/tok/observe")

        self.assertEqual((response.status_code, response.json()), (403, {"detail": OFF_LIST_PAGE_DETAIL}))
        self.assertEqual(list(self.dir.glob("*.png")), [])
        self.assertFalse(self.session.lock.locked())


if __name__ == "__main__":
    unittest.main()

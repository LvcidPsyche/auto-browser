"""The share page polls /share/{token}/observe every few seconds for as long as it is open.

It used to run a full observation (DOM, ARIA and OCR reads plus a screenshot kept
for good) under the session lock on each poll. These tests pin the cheap version:
one redacted frame, the URL and the title, the lock held for just that, and old
frames pruned.
"""

from __future__ import annotations

import asyncio
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
    session = SimpleNamespace(id="session-1", artifact_dir=artifact_dir, lock=asyncio.Lock())
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

    async def capture(target: SimpleNamespace, label: str) -> dict[str, str]:
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
        with patch.object(observation_module, "SHARE_FRAME_KEEP_SECONDS", 0.0):
            for _ in range(10):
                await service.share_snapshot("session-1")
                await asyncio.sleep(0.01)

        self.assertEqual(len(list(self.dir.glob(f"*-{SHARE_FRAME_LABEL}.png"))), 1)


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

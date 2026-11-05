from __future__ import annotations

import asyncio
import errno
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from playwright.async_api import Error as PlaywrightError

from app.artifacts import SessionArtifactService
from app.downloads import _RESERVE_ATTEMPTS, DownloadCaptureService


class FakeDownload:
    def __init__(self, filename: str, *, url: str = "https://example.com/report.csv", failure: str | None = None):
        self.suggested_filename = filename
        self.url = url
        self._failure = failure
        self.delete = mock.AsyncMock()

    async def save_as(self, path: str) -> None:
        Path(path).write_text("downloaded", encoding="utf-8")

    async def failure(self) -> str | None:
        return self._failure


class DownloadCaptureServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_refused_download_is_cancelled_and_recorded_by_its_origin_only(self) -> None:
        """Nothing else stops the browser fetching the rest of the file a refused page started."""
        with tempfile.TemporaryDirectory() as tempdir:
            artifacts = SessionArtifactService(tempdir)
            artifact_dir = artifacts.prepare_session_dir("session-1")
            service = DownloadCaptureService(artifacts)
            session = SimpleNamespace(id="session-1", artifact_dir=artifact_dir, downloads=[])
            download = FakeDownload("leak.txt", url="https://user:pw@refused.example:8443/x/leak.txt?q=secret")
            download.cancel = mock.AsyncMock()

            record = await service.block(session, download, "from a host ALLOWED_HOSTS does not allow")

            download.cancel.assert_awaited_once_with()
            self.assertEqual((record["status"], record["source_url"]), ("blocked", "https://refused.example:8443"))
            self.assertEqual((record["path"], record["filename"]), (None, None))

    async def test_capture_saves_file_and_jsonl_record(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            artifacts = SessionArtifactService(tempdir)
            artifact_dir = artifacts.prepare_session_dir("session-1")
            service = DownloadCaptureService(artifacts)
            session = SimpleNamespace(id="session-1", artifact_dir=artifact_dir, downloads=[])

            first = await service.capture(session, FakeDownload("../report.csv"))
            second = await service.capture(session, FakeDownload("report.csv", failure="network"))

            self.assertEqual(first["filename"], "report.csv")
            self.assertTrue(Path(first["path"]).is_file())
            self.assertEqual(second["status"], "failed")
            self.assertEqual((second["filename"], second["path"], second["url"]), (None, None, None))
            self.assertEqual(second["suggested_filename"], "report.csv")
            self.assertEqual(len(session.downloads), 2)

            records = [
                json.loads(line) for line in (artifact_dir / "downloads.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([record["filename"] for record in records], [first["filename"], None])

    async def test_concurrent_same_name_downloads_keep_both_files(self) -> None:
        class SlowDownload(FakeDownload):
            def __init__(self, body: str) -> None:
                super().__init__("report.csv")
                self.body = body

            async def save_as(self, path: str) -> None:
                await asyncio.sleep(0.01)
                Path(path).write_text(self.body, encoding="utf-8")

        with tempfile.TemporaryDirectory() as tempdir:
            artifacts = SessionArtifactService(tempdir)
            artifact_dir = artifacts.prepare_session_dir("session-1")
            service = DownloadCaptureService(artifacts)
            session = SimpleNamespace(id="session-1", artifact_dir=artifact_dir, downloads=[])

            records = await asyncio.gather(
                service.capture(session, SlowDownload("first")),
                service.capture(session, SlowDownload("second")),
            )

            self.assertNotEqual(records[0]["path"], records[1]["path"])
            self.assertEqual(
                sorted(Path(r["path"]).read_text(encoding="utf-8") for r in records),
                ["first", "second"],
            )

    async def test_dot_names_get_a_generated_name(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            artifacts = SessionArtifactService(tempdir)
            artifact_dir = artifacts.prepare_session_dir("session-1")
            service = DownloadCaptureService(artifacts)
            session = SimpleNamespace(id="session-1", artifact_dir=artifact_dir, downloads=[])

            for name in ("..", ".", "", "x" * 300 + ".csv"):
                with self.subTest(name=name):
                    record = await service.capture(session, FakeDownload(name))
                    self.assertEqual(record["status"], "completed")
                    self.assertEqual(Path(record["path"]).parent, artifact_dir / "downloads")
                    self.assertTrue(record["filename"].startswith("download-"))


class DownloadCapTests(unittest.IsolatedAsyncioTestCase):
    def _session(self, tempdir: str):
        artifacts = SessionArtifactService(tempdir)
        artifact_dir = artifacts.prepare_session_dir("session-1")
        return artifacts, SimpleNamespace(id="session-1", artifact_dir=artifact_dir, downloads=[])

    async def test_a_download_over_the_size_cap_is_deleted_and_recorded_as_refused(self) -> None:
        class Big(FakeDownload):
            async def save_as(self, path: str) -> None:
                Path(path).write_bytes(b"x" * 50)

        with tempfile.TemporaryDirectory() as tempdir:
            artifacts, session = self._session(tempdir)
            service = DownloadCaptureService(artifacts, max_bytes=10)

            record = await service.capture(session, Big("big.bin"))
            small = await service.capture(session, FakeDownload("small.txt"))

            self.assertEqual(record["status"], "refused")
            self.assertIn("DOWNLOAD_MAX_BYTES", record["failure"])
            self.assertEqual((record["path"], record["filename"], record["url"]), (None, None, None))
            self.assertEqual(small["status"], "completed")
            kept = sorted(path.name for path in (session.artifact_dir / "downloads").iterdir())
            self.assertEqual(kept, ["small.txt"])

    async def test_the_count_cap_stops_the_next_download_before_it_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            artifacts, session = self._session(tempdir)
            service = DownloadCaptureService(artifacts, max_per_session=3)
            for index in range(3):
                record = await service.capture(session, FakeDownload(f"f{index}.txt"))
                self.assertEqual(record["status"], "completed")

            extra = FakeDownload("f3.txt")
            extra.cancel = mock.AsyncMock()
            extra.save_as = mock.AsyncMock()
            record = await service.capture(session, extra)

            self.assertEqual(record["status"], "refused")
            self.assertIn("DOWNLOADS_MAX_PER_SESSION", record["failure"])
            extra.cancel.assert_awaited_once_with()
            extra.save_as.assert_not_awaited()
            self.assertEqual(len(list((session.artifact_dir / "downloads").iterdir())), 3)


class DownloadCleanupTests(unittest.IsolatedAsyncioTestCase):
    """The browser keeps its own copy of a download until its context closes; none may outlive the capture."""

    def _session(self, tempdir: str):
        artifacts = SessionArtifactService(tempdir)
        artifact_dir = artifacts.prepare_session_dir("session-1")
        return artifacts, SimpleNamespace(id="session-1", artifact_dir=artifact_dir, downloads=[])

    @staticmethod
    def _files(session) -> list[str]:
        directory = session.artifact_dir / "downloads"
        return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []

    async def test_the_browsers_copy_is_deleted_after_a_kept_download(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            artifacts, session = self._session(tempdir)
            download = FakeDownload("ok.txt")

            record = await DownloadCaptureService(artifacts).capture(session, download)

            self.assertEqual(record["status"], "completed")
            download.delete.assert_awaited_once_with()

    async def test_the_browsers_copy_is_deleted_after_an_oversized_download(self) -> None:
        class Big(FakeDownload):
            async def save_as(self, path: str) -> None:
                Path(path).write_bytes(b"x" * 50)

        with tempfile.TemporaryDirectory() as tempdir:
            artifacts, session = self._session(tempdir)
            download = Big("big.bin")

            record = await DownloadCaptureService(artifacts, max_bytes=10).capture(session, download)

            self.assertEqual(record["status"], "refused")
            download.delete.assert_awaited_once_with()
            self.assertEqual(self._files(session), [])

    async def test_the_browsers_copy_is_deleted_after_a_failed_download(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            artifacts, session = self._session(tempdir)
            download = FakeDownload("bad.txt", failure="network")

            record = await DownloadCaptureService(artifacts).capture(session, download)

            self.assertEqual(record["status"], "failed")
            download.delete.assert_awaited_once_with()
            self.assertEqual(self._files(session), [])

    async def test_the_browsers_copy_is_deleted_after_a_download_the_count_cap_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            artifacts, session = self._session(tempdir)
            service = DownloadCaptureService(artifacts, max_per_session=1)
            await service.capture(session, FakeDownload("one.txt"))
            extra = FakeDownload("two.txt")
            extra.cancel = mock.AsyncMock()

            record = await service.capture(session, extra)

            self.assertEqual(record["status"], "refused")
            extra.delete.assert_awaited_once_with()

    async def test_a_browser_that_cannot_delete_does_not_fail_the_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            artifacts, session = self._session(tempdir)
            download = FakeDownload("ok.txt")
            download.delete = mock.AsyncMock(side_effect=PlaywrightError("Target closed"))

            record = await DownloadCaptureService(artifacts).capture(session, download)

            self.assertEqual(record["status"], "completed")

    async def test_an_interrupted_save_leaves_no_file_and_a_retry_is_counted_once(self) -> None:
        class Interrupted(FakeDownload):
            async def save_as(self, path: str) -> None:
                Path(path).write_text("half", encoding="utf-8")
                raise PlaywrightError("connection reset")

        with tempfile.TemporaryDirectory() as tempdir:
            artifacts, session = self._session(tempdir)
            service = DownloadCaptureService(artifacts, max_per_session=1)

            failed = await service.capture(session, Interrupted("report.csv"))
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(self._files(session), [])

            retry = await service.capture(session, FakeDownload("report.csv"))
            self.assertEqual(retry["status"], "completed")
            self.assertEqual(self._files(session), ["report.csv"])

    async def test_a_save_cancelled_midway_still_deletes_the_browsers_copy_and_stays_cancelled(self) -> None:
        saving, deleted = asyncio.Event(), asyncio.Event()

        class Hanging(FakeDownload):
            async def save_as(self, path: str) -> None:
                saving.set()
                await asyncio.sleep(30)

        with tempfile.TemporaryDirectory() as tempdir:
            artifacts, session = self._session(tempdir)
            download = Hanging("report.csv")
            download.delete = mock.AsyncMock(side_effect=lambda: deleted.set())
            task = asyncio.create_task(DownloadCaptureService(artifacts).capture(session, download))
            await asyncio.wait_for(saving.wait(), 5)

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            await asyncio.wait_for(deleted.wait(), 5)

            download.delete.assert_awaited_once_with()
            self.assertEqual(self._files(session), [])

    async def test_a_failure_reported_after_a_complete_save_removes_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            artifacts, session = self._session(tempdir)

            await DownloadCaptureService(artifacts).capture(session, FakeDownload("x.txt", failure="canceled"))

            self.assertEqual(self._files(session), [])


class ReserveBoundTests(unittest.TestCase):
    """`_reserve` runs on the event loop, so it must give up instead of spinning."""

    @staticmethod
    def _failing_open(error: OSError, calls: list[int]):
        def fake_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
            calls.append(1)
            if len(calls) > 50:
                raise AssertionError("_reserve is still retrying: the loop is unbounded")
            raise error

        return fake_open

    def test_persistent_oserror_is_raised_after_one_name_fallback(self) -> None:
        calls: list[int] = []
        error = OSError(errno.ENOSPC, "No space left on device")
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.object(Path, "open", self._failing_open(error, calls)):
                with self.assertRaises(OSError) as raised:
                    DownloadCaptureService._reserve(Path(tempdir), "report.csv")

        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        self.assertLessEqual(len(calls), 3)

    def test_a_refused_name_falls_back_to_a_generated_one(self) -> None:
        real_open = Path.open
        tried: list[str] = []

        def refuse_the_first_name(self: Path, mode: str = "r", *args: object, **kwargs: object):
            tried.append(self.name)
            if len(tried) == 1:
                raise OSError(errno.ENAMETOOLONG, "File name too long")
            return real_open(self, mode, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.object(Path, "open", refuse_the_first_name):
                reserved = DownloadCaptureService._reserve(Path(tempdir), "report.csv")
            self.assertTrue(reserved.is_file())

        self.assertEqual(tried[0], "report.csv")
        self.assertTrue(reserved.name.startswith("download-"))

    def test_name_collisions_stop_after_a_bounded_number_of_attempts(self) -> None:
        calls: list[int] = []
        error = FileExistsError(errno.EEXIST, "File exists")
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.object(Path, "open", self._failing_open(error, calls)):
                with self.assertRaises(FileExistsError):
                    DownloadCaptureService._reserve(Path(tempdir), "report.csv")

        self.assertEqual(len(calls), _RESERVE_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()

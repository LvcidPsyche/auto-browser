from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.artifacts import SessionArtifactService
from app.downloads import DownloadCaptureService


class FakeDownload:
    def __init__(self, filename: str, *, url: str = "https://example.com/report.csv", failure: str | None = None):
        self.suggested_filename = filename
        self.url = url
        self._failure = failure

    async def save_as(self, path: str) -> None:
        Path(path).write_text("downloaded", encoding="utf-8")

    async def failure(self) -> str | None:
        return self._failure


class DownloadCaptureServiceTests(unittest.IsolatedAsyncioTestCase):
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
            self.assertNotEqual(first["filename"], second["filename"])
            self.assertEqual(len(session.downloads), 2)

            records = [
                json.loads(line) for line in (artifact_dir / "downloads.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([record["filename"] for record in records], [first["filename"], second["filename"]])

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


if __name__ == "__main__":
    unittest.main()

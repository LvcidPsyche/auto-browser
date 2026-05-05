from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.artifacts import SessionArtifactService


class FakePage:
    async def screenshot(self, *, path: str, full_page: bool) -> None:
        self.path = path
        self.full_page = full_page
        Path(path).write_bytes(b"png")


class SessionArtifactServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_capture_trace_and_append_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            service = SessionArtifactService(tempdir)
            artifact_dir = service.prepare_session_dir("session-1")
            trace_path = artifact_dir / "trace.zip"
            trace_path.write_bytes(b"trace")
            page = FakePage()
            session = SimpleNamespace(
                id="session-1",
                artifact_dir=artifact_dir,
                page=page,
                trace_path=trace_path,
                trace_recording=False,
            )

            screenshot = await service.capture_screenshot(session, "../bad label")
            trace = service.trace_payload(session)
            await service.append_jsonl(artifact_dir / "events.jsonl", {"status": "ok"})

            self.assertTrue((artifact_dir / "downloads").is_dir())
            self.assertIn("-bad-label.png", screenshot["path"])
            self.assertTrue(Path(screenshot["path"]).exists())
            self.assertFalse(page.full_page)
            self.assertEqual(trace["trace_url"], "/artifacts/session-1/trace.zip")
            self.assertTrue(trace["trace_exists"])
            lines = (artifact_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(lines[0]), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()

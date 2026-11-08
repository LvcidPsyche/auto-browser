from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.artifacts import SessionArtifactService


class FakePage:
    async def screenshot(self, *, full_page: bool, type: str, path: str | None = None) -> bytes:  # noqa: A002
        # Playwright's names. The capture takes the bytes and writes them itself (review of c4b), so no path is given.
        self.path = path
        self.full_page = full_page
        self.type = type
        return b"png"


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
            self.assertEqual(Path(screenshot["path"]).read_bytes(), b"png")
            self.assertIsNone(page.path)
            self.assertFalse(page.full_page)
            self.assertEqual(page.type, "png")  # ACT-33: a staged capture's hidden name does not end in .png
            self.assertEqual(trace["trace_url"], "/artifacts/session-1/trace.zip")
            self.assertTrue(trace["trace_exists"])
            lines = (artifact_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(lines[0]), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()

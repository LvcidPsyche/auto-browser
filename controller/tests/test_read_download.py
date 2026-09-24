"""browser.read_download: an agent can read the file it just downloaded.

Downloads were listed with a controller-side path and an /artifacts URL,
neither of which a model behind an MCP client can open, so "export the report
and summarise it" stopped at the export. The tool reads text downloads, pages
them like browser.get_html, refuses binaries, and reads nothing outside the
session's own downloads directory.
"""

from __future__ import annotations

import codecs
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app.action_errors import SessionNotFoundError
from app.browser_manager import BrowserManager
from app.config import Settings
from app.models import McpToolCallRequest, SessionRecord
from app.tool_gateway import McpToolGateway

SESSION_ID = "sess-dl"
CSV_TEXT = "id,customer,total\n1001,Jane Doe,120.00\n1002,José Roe,89.50\n"


class ReadDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.manager = BrowserManager(
            Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(self.root / "artifacts"),
                UPLOAD_ROOT=str(self.root / "uploads"),
                AUTH_ROOT=str(self.root / "auth"),
                APPROVAL_ROOT=str(self.root / "approvals"),
                AUDIT_ROOT=str(self.root / "audit"),
                SESSION_STORE_ROOT=str(self.root / "sessions"),
            )
        )
        await self.manager.session_store.startup()
        self.downloads_dir = self.root / "artifacts" / SESSION_ID / "downloads"
        self.downloads_dir.mkdir(parents=True)
        self.records: list[dict[str, Any]] = []

    def _add(self, name: str, data: bytes, *, status: str = "completed", path: Path | None = None) -> str:
        target = path or self.downloads_dir / name
        if path is None:
            target.write_bytes(data)
        download_id = f"dl{len(self.records):03d}"
        self.records.append(
            {
                "id": download_id,
                "status": status,
                "filename": name,
                "path": str(target),
                "url": f"/artifacts/{SESSION_ID}/downloads/{name}",
                "source_url": f"https://example.com/{name}",
                "failure": None if status == "completed" else "canceled",
            }
        )
        return download_id

    async def _persist(self) -> None:
        await self.manager.session_store.upsert(
            SessionRecord(
                id=SESSION_ID,
                name="downloads",
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
                status="closed",
                current_url="https://example.com",
                title="t",
                artifact_dir=str(self.root / "artifacts" / SESSION_ID),
                takeover_url="http://127.0.0.1:6080",
                remote_access={},
                downloads=self.records,
            )
        )

    async def _read(self, download_id: str | None = None) -> dict[str, Any]:
        await self._persist()
        return await self.manager.read_download_text(SESSION_ID, download_id)

    async def test_latest_completed_download_is_the_default(self) -> None:
        self._add("old.csv", b"old\n")
        self._add("report.csv", CSV_TEXT.encode("utf-8"))
        self._add("partial.csv", b"", status="failed")

        result = await self._read()

        self.assertEqual(result["filename"], "report.csv")
        self.assertEqual(result["text"], CSV_TEXT)
        self.assertEqual(result["content_type"], "text/csv")
        self.assertFalse(result["lossy"])
        self.assertEqual(result["source_url"], "https://example.com/report.csv")

    async def test_encodings(self) -> None:
        cases = {
            "bom.csv": (codecs.BOM_UTF8 + CSV_TEXT.encode("utf-8"), CSV_TEXT, False),
            "excel.txt": (codecs.BOM_UTF16_LE + CSV_TEXT.encode("utf-16-le"), CSV_TEXT, False),
            "legacy.csv": ("José\n".encode("cp1252"), "Jos�\n", True),
        }
        ids = {name: self._add(name, data) for name, (data, _, _) in cases.items()}
        for name, (_, text, lossy) in cases.items():
            with self.subTest(name):
                result = await self._read(ids[name])
                self.assertEqual(result["text"], text)
                self.assertIs(result["lossy"], lossy)

    async def test_binaries_are_refused_with_their_url(self) -> None:
        pdf = self._add("statement.pdf", b"%PDF-1.7\n" + bytes(range(128, 256)) * 4)
        blob = self._add("blob.bin.unknownext", b"MZ\x00\x00" + b"\x90" * 64)

        for download_id, name in ((pdf, "statement.pdf"), (blob, "blob.bin.unknownext")):
            with self.subTest(name):
                with self.assertRaisesRegex(ValueError, "is binary") as caught:
                    await self._read(download_id)
                self.assertIn(f"/artifacts/{SESSION_ID}/downloads/{name}", str(caught.exception))

    async def test_nothing_outside_the_sessions_downloads_directory_is_read(self) -> None:
        secret = self.root / "secret.txt"
        secret.write_text("do not read")
        other = self.root / "artifacts" / "other-session" / "downloads" / "theirs.csv"
        other.parent.mkdir(parents=True)
        other.write_text("not yours")
        cases = {
            "outside": self._add("secret.txt", b"", path=secret),
            "other session": self._add("theirs.csv", b"", path=other),
            "traversal": self._add("x.csv", b"", path=self.downloads_dir / ".." / ".." / ".." / "secret.txt"),
        }
        for label, download_id in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, "not in this session's downloads directory"):
                    await self._read(download_id)

    async def test_unusable_downloads_say_why(self) -> None:
        failed = self._add("partial.csv", b"", status="failed")
        gone = self._add("gone.csv", b"x")
        (self.downloads_dir / "gone.csv").unlink()
        big = self._add("big.csv", b"a,b\n" * 100)

        with self.assertRaisesRegex(ValueError, "did not complete \\(canceled\\)"):
            await self._read(failed)
        with self.assertRaisesRegex(ValueError, "no longer on disk"):
            await self._read(gone)
        with patch("app.browser.services.diagnostics.DOWNLOAD_READ_MAX_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "larger than 100 bytes; fetch it from /artifacts/"):
                await self._read(big)
        with self.assertRaisesRegex(ValueError, f"No download nope in session {SESSION_ID}.*{failed}"):
            await self._read("nope")

    async def test_no_completed_download(self) -> None:
        self._add("partial.csv", b"", status="failed")

        with self.assertRaisesRegex(ValueError, "has no completed downloads"):
            await self._read()

    async def test_unknown_session(self) -> None:
        with self.assertRaises(SessionNotFoundError):
            await self.manager.read_download_text("no-such-session")

    async def test_mcp_tool_pages_the_text(self) -> None:
        self._add("big.csv", ("row\n" * 600).encode("utf-8"))  # 2,400 characters
        await self._persist()
        gateway = McpToolGateway(manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())
        self.assertIn("browser.read_download", {tool["name"] for tool in gateway.list_tools()})

        chunks, offset = [], 0
        while offset is not None:
            response = await gateway.call_tool(
                McpToolCallRequest(
                    name="browser.read_download",
                    arguments={"session_id": SESSION_ID, "offset": offset, "max_chars": 1_000},
                )
            )
            self.assertFalse(response.isError, response.content[0].text)
            page = json.loads(response.content[0].text)
            self.assertEqual(page["filename"], "big.csv")
            self.assertNotIn("text", page)
            chunks.append(page["content"])
            offset = page["next_offset"]

        self.assertEqual("".join(chunks), "row\n" * 600)
        self.assertEqual(len(chunks), 3)

    async def test_mcp_errors_are_readable(self) -> None:
        self._add("statement.pdf", b"%PDF-1.7\n")
        await self._persist()
        gateway = McpToolGateway(manager=self.manager, orchestrator=SimpleNamespace(), job_queue=SimpleNamespace())

        response = await gateway.call_tool(
            McpToolCallRequest(name="browser.read_download", arguments={"session_id": SESSION_ID})
        )

        self.assertTrue(response.isError)
        self.assertIn("statement.pdf (application/pdf", response.content[0].text)


if __name__ == "__main__":
    unittest.main()

"""Unit checks for app/file_transfer.py: typing by bytes, names, size
ceilings, the controller-side fetch guard, and the non-persistent (local
capture) download path. The real browser path is covered end to end by
tests/test_file_transfer_e2e_real.py."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.action_errors import BrowserActionError
from app.file_transfer import FileTransferService, kind_limit, safe_filename, sniff, unsupported_reason

MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"v" * 64


class SniffTests(unittest.TestCase):
    def test_allowed_types_are_recognised_by_their_bytes(self) -> None:
        cases = {
            b"\xff\xd8\xff\xe0rest": ("image/jpeg", "image"),
            b"\x89PNG\r\n\x1a\nrest": ("image/png", "image"),
            b"GIF89a....": ("image/gif", "image"),
            b"RIFF\x00\x00\x00\x00WEBPVP8 ": ("image/webp", "image"),
            b"\x00\x00\x00\x18ftypheic....": ("image/heic", "image"),
            MP4: ("video/mp4", "video"),
            b"\x00\x00\x00\x14ftypqt  ....": ("video/quicktime", "video"),
            b"\x1aE\xdf\xa3\x01\x00": ("video/webm", "video"),
            b"ID3\x04\x00": ("audio/mpeg", "audio"),
            b"\xff\xf1\x50\x80": ("audio/aac", "audio"),
            b"\x00\x00\x00\x20ftypM4A ....": ("audio/mp4", "audio"),
            b"OggS\x00\x02": ("audio/ogg", "audio"),
            b"RIFF\x00\x00\x00\x00WAVEfmt ": ("audio/wav", "audio"),
            b"%PDF-1.7\n": ("application/pdf", "pdf"),
        }
        for head, expected in cases.items():
            self.assertEqual(sniff(head), expected, head)

    def test_mp4_and_mov_whose_first_box_is_not_ftyp(self) -> None:
        # Google Flow's single-video download, 2026-09-26: refused as "not a video".
        cases = {
            b"\x00\x00\x00\x08wide\x00\x00\x10\x00mdat": ("video/quicktime", "video"),
            b"\x00\x00\x00\x08free\x00\x00\x00\x08ftyp": ("video/quicktime", "video"),
            b"\x00\x00\x10\x00moov\x00\x00\x00\x6cmvhd": ("video/quicktime", "video"),
            b"\x00\x00\x00\x01mdat\x00\x00\x00\x00\x00\x10\x00\x00": ("video/quicktime", "video"),
            b"\x00\x00\x00\x18stypmsdh\x00\x00\x00\x00": ("video/mp4", "video"),
            b"\x00\x00\x00\x2csidx\x00\x00\x00\x00": ("video/mp4", "video"),
            b"\x00\x00\x00\x60moof\x00\x00\x00\x10mfhd": ("video/mp4", "video"),
        }
        for head, expected in cases.items():
            self.assertEqual(sniff(head), expected, head)
        # A box name alone is not enough: an impossible box size stays refused.
        self.assertIsNone(sniff(b"\x00\x00\x00\x03wide...."))

    def test_a_refusal_says_what_the_file_really_is(self) -> None:
        self.assertEqual(unsupported_reason(b"PK\x03\x04\x14\x00"), "zip")
        self.assertEqual(unsupported_reason(b"  <!DOCTYPE html><html>"), "html")
        self.assertEqual(unsupported_reason(b'{"error": 1}'), "json")
        self.assertEqual(unsupported_reason(b"<svg xmlns="), "xml_or_svg")
        self.assertEqual(unsupported_reason(b"MZ\x90\x00"), "unknown")

    def test_everything_else_is_refused(self) -> None:
        for head in (
            b"<!doctype html><script>", b"<svg xmlns=", b"MZ\x90\x00", b"\x7fELF", b"PK\x03\x04",
            b"#!/bin/sh", b"{\"json\": 1}", b"", b"\x00\x00",
        ):
            self.assertIsNone(sniff(head), head)

    def test_names_follow_the_real_type(self) -> None:
        self.assertEqual(safe_filename("photo.html", "image/jpeg"), "photo.jpg")
        self.assertEqual(safe_filename("../../etc/passwd", "application/pdf"), "passwd.pdf")
        self.assertEqual(safe_filename("C:\\x\\clip.MP4", "video/mp4"), "clip.mp4")
        self.assertEqual(safe_filename("", "image/png"), "file.png")
        self.assertEqual(safe_filename("نتيجة.mp4", "video/mp4"), "نتيجة.mp4")
        self.assertEqual(safe_filename('a<b>:"c|?*.png', "image/png"), "abc.png")
        self.assertTrue(len(safe_filename("x" * 500 + ".png", "image/png")) <= 104)

    def test_each_kind_has_its_own_ceiling_under_the_overall_one(self) -> None:
        overall = 200 * 1024 * 1024
        self.assertEqual(kind_limit("video", overall), overall)
        self.assertLess(kind_limit("image", overall), overall)
        self.assertEqual(kind_limit("image", 1000), 1000)


def _manager(tmp: Path, **settings) -> SimpleNamespace:
    audit_events: list = []

    async def append(**event):
        audit_events.append(event)

    def assert_url_allowed(url: str) -> None:
        if "denied.example" in url:
            raise PermissionError("not allowlisted")

    session = SimpleNamespace(
        id="s1", artifact_dir=tmp / "artifacts" / "s1", upload_dir=tmp / "uploads" / "s1",
        persistent_profile_name=None, downloads=[],
    )
    session.artifact_dir.mkdir(parents=True)
    session.upload_dir.mkdir(parents=True)

    async def get_session(session_id: str):
        if session_id != "s1":
            raise KeyError(session_id)
        return session

    return SimpleNamespace(
        settings=SimpleNamespace(
            file_transfer_max_bytes=settings.get("max_bytes", 10_000),
            file_transfer_download_timeout_seconds=5.0,
            file_transfer_retention_hours=6.0,
        ),
        audit=SimpleNamespace(append=append, events=audit_events),
        get_session=get_session,
        session=session,
        _assert_url_allowed=assert_url_allowed,
    )


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


class ReceiveUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_pushed_file_is_typed_named_and_kept_readable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager = _manager(Path(raw))
            service = FileTransferService(manager)
            record = await service.receive_upload(
                "s1", filename="clip.bin", chunks=_chunks(MP4[:10], MP4[10:]), declared_length=len(MP4),
            )
            self.assertEqual((record["mime_type"], record["kind"], record["filename"]), ("video/mp4", "video", "clip.mp4"))
            self.assertNotIn("path", record)
            stored = service.get("s1", record["id"])
            self.assertEqual(Path(stored["path"]).read_bytes(), MP4)
            self.assertTrue(Path(stored["path"]).is_relative_to(manager.session.upload_dir))
            self.assertEqual(manager.audit.events[-1]["event_type"], "file_received")
            await service.delete("s1", record["id"])
            with self.assertRaises(BrowserActionError):
                service.get("s1", record["id"])

    async def test_disguised_and_oversized_files_are_refused_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager = _manager(Path(raw), max_bytes=100)
            service = FileTransferService(manager)
            with self.assertRaises(BrowserActionError) as disguised:
                await service.receive_upload(
                    "s1", filename="x.png", chunks=_chunks(b"<html><script>1</script>"), declared_length=None,
                )
            self.assertEqual((disguised.exception.code, disguised.exception.status_code), ("file_unsupported_type", 415))
            with self.assertRaises(BrowserActionError) as big:
                await service.receive_upload("s1", filename="x.mp4", chunks=_chunks(MP4, MP4), declared_length=None)
            self.assertEqual((big.exception.code, big.exception.status_code), ("file_too_large", 413))
            with self.assertRaises(BrowserActionError) as declared:
                await service.receive_upload("s1", filename="x.mp4", chunks=_chunks(MP4), declared_length=10_000)
            self.assertEqual(declared.exception.code, "file_too_large")
            transfers = manager.session.upload_dir / "transfers"
            self.assertEqual([p for p in transfers.iterdir()], [])

    async def test_attach_refuses_a_download_or_unknown_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = FileTransferService(_manager(Path(raw)))
            with self.assertRaises(BrowserActionError) as unknown:
                await service.attach("s1", "0" * 24)
            self.assertEqual(unknown.exception.status_code, 404)


class LocalDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_takes_the_newest_local_download(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager = _manager(Path(raw))
            source = Path(raw) / "captured.pdf"
            source.write_bytes(b"%PDF-1.4 hello")
            manager.session.downloads.append(
                {"id": "x", "status": "completed", "path": str(source), "suggested_filename": "report.pdf",
                 "source_url": "https://site.example/r.pdf"}
            )
            service = FileTransferService(manager)
            record = await service.download("s1", mode="latest")
            self.assertEqual((record["filename"], record["mime_type"]), ("report.pdf", "application/pdf"))
            self.assertEqual(record["source"], {"mode": "latest", "host": "site.example"})
            self.assertTrue(Path(service.get("s1", record["id"])["path"]).is_relative_to(manager.session.artifact_dir))

    async def test_no_download_yet_is_a_clear_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = FileTransferService(_manager(Path(raw)))
            with self.assertRaises(BrowserActionError) as refused:
                await service.download("s1", mode="latest")
            self.assertEqual(refused.exception.code, "file_no_download")

    async def test_bad_requests_never_touch_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = FileTransferService(_manager(Path(raw)))
            for kwargs in (
                {"mode": "teleport"},
                {"mode": "click"},
                {"mode": "element"},
                {"mode": "url", "url": "file:///etc/passwd"},
                {"mode": "url", "url": "javascript:alert(1)"},
                {"mode": "media", "media_kind": "exe"},
            ):
                with self.assertRaises(BrowserActionError, msg=kwargs) as refused:
                    await service.download("s1", **kwargs)
                self.assertEqual(refused.exception.code, "file_bad_request", kwargs)
            with self.assertRaises(BrowserActionError) as blocked:
                await service.download("s1", mode="url", url="https://denied.example/x.png")
            self.assertEqual(blocked.exception.code, "file_fetch_blocked")


class PublicFetchGuardTests(unittest.TestCase):
    def test_the_controller_never_fetches_inside_the_stack(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = FileTransferService(_manager(Path(raw)))
            for url in (
                "http://browser-node:9224/downloads/file?id=1",
                "http://controller:8000/sessions",
                "http://localhost/x.png",
                "http://127.0.0.1/x.png",
                "http://10.0.0.5/x.png",
                "http://169.254.169.254/latest/meta-data",
                "http://[::1]/x.png",
                "ftp://files.example.com/x.png",
                "https://denied.example/x.png",
            ):
                with self.assertRaises(BrowserActionError, msg=url) as blocked:
                    service._assert_public_fetch(url)
                self.assertEqual(blocked.exception.code, "file_fetch_blocked", url)
            service._assert_public_fetch("https://cdn.example.com/x.png")
            service._assert_public_fetch("https://93.184.216.34/x.png")


if __name__ == "__main__":
    asyncio.run(unittest.main())


def _zip(files: dict[str, bytes], *, compression: int = 8) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class ZipDownloadTests(unittest.IsolatedAsyncioTestCase):
    """Google Flow's single-video "Download" hands over a zip (2026-09-26)."""

    async def _receive(self, data: bytes, *, max_bytes: int = 10_000, filename: str = "flow.zip"):
        raw = tempfile.TemporaryDirectory()
        self.addCleanup(raw.cleanup)
        service = FileTransferService(_manager(Path(raw.name), max_bytes=max_bytes))
        record = await service.receive_upload("s1", filename=filename, chunks=_chunks(data), declared_length=None)
        return service, record

    async def test_the_one_video_inside_a_zip_is_taken_out(self) -> None:
        service, record = await self._receive(_zip({"clips/ugc video.mp4": MP4, "meta.json": b"{}"}))
        self.assertEqual((record["mime_type"], record["kind"]), ("video/mp4", "video"))
        self.assertEqual(record["filename"], "ugc video.mp4")
        self.assertEqual(record["archive_members"], 2)
        self.assertEqual(Path(service.get("s1", record["id"])["path"]).read_bytes(), MP4)

    async def test_the_largest_video_wins_over_images_and_smaller_videos(self) -> None:
        big = MP4 + b"x" * 200
        _, record = await self._receive(_zip({"a.mp4": MP4, "b.mp4": big, "c.png": b"\x89PNG\r\n\x1a\n" + b"p" * 500}))
        self.assertEqual(record["kind"], "video")
        self.assertEqual(record["size_bytes"], len(big))
        self.assertEqual(record["archive_members"], 3)

    async def test_a_zip_without_media_is_refused_as_zip(self) -> None:
        with self.assertRaises(BrowserActionError) as refused:
            await self._receive(_zip({"notes.txt": b"hello", "data.json": b"{}"}))
        self.assertEqual(refused.exception.code, "file_unsupported_type")
        self.assertEqual(refused.exception.details.get("reason"), "zip")

    async def test_a_member_over_its_ceiling_is_refused_without_extracting_it(self) -> None:
        huge = MP4 + b"\x00" * 50_000  # compresses tiny, expands past the 10 KB ceiling
        with self.assertRaises(BrowserActionError) as refused:
            await self._receive(_zip({"bomb.mp4": huge}), max_bytes=10_000)
        self.assertEqual(refused.exception.code, "file_too_large")


class PendingChooserTests(unittest.IsolatedAsyncioTestCase):
    """Google Flow's "Upload" opens the native chooser straight from a click (2026-09-26):
    the agent clicked, the chooser opened, and upload_file found nothing to fill."""

    class _Chooser:
        def __init__(self) -> None:
            self.files = None

        async def set_files(self, path) -> None:
            self.files = path

    async def _service(self, *, target: dict, element=None):
        import time as _time

        raw = tempfile.TemporaryDirectory()
        self.addCleanup(raw.cleanup)
        manager = _manager(Path(raw.name))

        class _Locator:
            first = None

            async def element_handle(self, timeout=None):
                return element

        locator = _Locator()
        locator.first = locator
        manager.session.page = SimpleNamespace(locator=lambda selector: locator, frames=[])
        manager.session.pending_file_chooser = None
        manager.actions = SimpleNamespace(resolve_target=lambda **kwargs: dict(target))

        async def run_action(session, name, payload, operation):
            await operation()
            return {"status": "ok"}

        manager._run_action = run_action
        service = FileTransferService(manager)
        record = await service.receive_upload("s1", filename="shoe.png", chunks=_chunks(b"\x89PNG\r\n\x1a\n" + b"p" * 50), declared_length=None)
        return service, manager, record, _time

    async def test_a_chooser_opened_by_an_earlier_click_is_filled(self) -> None:
        service, manager, record, _time = await self._service(target={"mode": "page"})
        chooser = self._Chooser()
        manager.session.pending_file_chooser = (chooser, _time.monotonic())
        result = await service.attach("s1", record["id"])
        self.assertEqual(result["via"], "pending_chooser")
        self.assertEqual(chooser.files, service.get("s1", record["id"])["path"])
        self.assertIsNone(manager.session.pending_file_chooser)

    async def test_an_unresolvable_target_falls_back_to_the_open_chooser(self) -> None:
        service, manager, record, _time = await self._service(target={"selector": "#gone"}, element=None)
        chooser = self._Chooser()
        manager.session.pending_file_chooser = (chooser, _time.monotonic())
        result = await service.attach("s1", record["id"], selector="#gone")
        self.assertEqual(result["via"], "pending_chooser")

    async def test_a_stale_chooser_is_not_used(self) -> None:
        service, manager, record, _time = await self._service(target={"mode": "page"})
        chooser = self._Chooser()
        manager.session.pending_file_chooser = (chooser, _time.monotonic() - 120)
        with self.assertRaises(BrowserActionError) as refused:
            await service.attach("s1", record["id"])
        self.assertEqual(refused.exception.code, "file_no_input")
        self.assertIsNone(chooser.files)

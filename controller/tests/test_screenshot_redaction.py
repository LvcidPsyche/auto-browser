"""ACT-33: a screenshot that is to be redacted reaches its published name only once its redaction is done.

The capture was written straight to the name /artifacts serves it under, and OCR and the PII redaction then ran on
that file, so the raw image was one request away for as long as OCR took; the redacted bytes were then written over
it in place, so a write cut short left a partly written image there. Now the capture goes to a hidden name beside it
first, the redacted bytes reach that name through a temporary and os.replace, and the result is moved onto the
published name with os.replace; one with nothing to redact, or that could not be scrubbed, is moved there as it is.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app.artifacts import SessionArtifactService
from app.browser.page_provenance import PageProvenance
from app.browser.services import observation
from app.browser.services.diagnostics import checked_screenshot
from app.browser.services.observation import BrowserObservationService
from app.browser_scripts import INTERACTABLES_SCRIPT, PAGE_SUMMARY_SCRIPT
from app.pii_scrub import PiiScrubber
from tests._fake_element_ids import FakePageElementIds

# One OCR word that the redaction blacks out.
_EMAIL_BLOCKS = [{"x": 1, "y": 1, "width": 4, "height": 4, "text": "ops@example.com"}]


def _png() -> bytes:
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(image, format="PNG")
    return image.getvalue()


class _Page:
    url = "https://example.com"

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.release.set()

    async def screenshot(self, *, path: str, full_page: bool, type: str = "png") -> bytes:  # noqa: A002
        # Playwright's names; a hidden name does not end in .png, so the type is given, not read from it.
        assert type == "png"
        await self.release.wait()
        Path(path).write_bytes(_png())
        return b""

    async def title(self) -> str:
        return "Example"

    async def aria_snapshot(self, **_: object) -> str:
        return '- heading "Example" [level=1] [ref=e1]\n'

    async def evaluate(self, script: str, *args: object) -> object:
        if script is INTERACTABLES_SCRIPT:
            return []
        assert script is PAGE_SUMMARY_SCRIPT
        return {"title": "Example", "active_element": None, "text_excerpt": "Hello", "dom_outline": {}}


def _service(tmp_path: Path) -> tuple[BrowserObservationService, SimpleNamespace, list[list[str]]]:
    """A service whose captures go through its real capture_session_screenshot, onto a fake page, and whose OCR
    records the names in the session's directory each time it runs."""
    artifact_dir = tmp_path / "session-1"
    artifact_dir.mkdir()
    session = SimpleNamespace(
        id="session-1",
        lock=asyncio.Lock(),
        page=_Page(),
        artifact_dir=artifact_dir,
        console_messages=[],
        page_errors=[],
        request_failures=[],
        downloads=[],
        last_screenshot=None,
    )
    seen_by_ocr: list[list[str]] = []

    async def extract_from_image(path: str) -> dict:
        seen_by_ocr.append(sorted(child.name for child in artifact_dir.iterdir()))
        return {"available": True, "blocks": [], "redaction_blocks": _EMAIL_BLOCKS}

    manager = SimpleNamespace(
        settings=SimpleNamespace(
            ocr_skip_when_text_available=True,
            perception_preset_default="normal",
            pii_scrub_action_screenshots=True,
        ),
        pii_scrubber=PiiScrubber(screenshot_enabled=True, audit_report=False),
        ocr=SimpleNamespace(extract_from_image=extract_from_image),
        artifacts=SessionArtifactService(tmp_path),
        _session_summary=AsyncMock(return_value={"id": session.id}),
        _current_takeover_url=MagicMock(return_value=None),
        _shown_url=MagicMock(return_value=session.page.url),
        _assert_page_allowed=MagicMock(),
        _page_refused=MagicMock(return_value=False),
        page_provenance=PageProvenance(),
        remote_access=SimpleNamespace(session_info=MagicMock(return_value={})),
        tabs=SimpleNamespace(summaries=AsyncMock(return_value=[]), pages=MagicMock(return_value=[])),
        get_session=AsyncMock(return_value=session),
        element_ids=FakePageElementIds(),
    )
    service = BrowserObservationService(manager=manager)
    manager._capture_screenshot = service.capture_session_screenshot
    return service, session, seen_by_ocr


def _published(names: list[str]) -> list[str]:
    return [name for name in names if not name.startswith(".")]


async def test_a_redacted_capture_reaches_its_published_name_only_once_redacted(tmp_path: Path) -> None:
    service, session, seen_by_ocr = _service(tmp_path)

    result = await service.capture_screenshot(session.id)

    published = Path(result["screenshot_path"])
    assert len(seen_by_ocr) == 1
    assert _published(seen_by_ocr[0]) == [], "the raw capture was at a published name while OCR read it"
    assert published.read_bytes() != _png()  # the redacted image, not the raw one
    assert sorted(child.name for child in session.artifact_dir.iterdir()) == [published.name]
    assert result["screenshot_url"] == f"/artifacts/{session.id}/{published.name}"
    assert session.last_screenshot["path"] == str(published)


async def test_an_observation_and_an_action_snapshot_are_published_only_once_redacted(tmp_path: Path) -> None:
    service, session, seen_by_ocr = _service(tmp_path)

    observed = await service.observation_payload(session, preset="normal")
    snapshot = await service.light_snapshot(session, label="before-click")

    assert [_published(names) for names in seen_by_ocr] == [[], [Path(observed["screenshot_path"]).name]]
    for payload in (observed, snapshot):
        assert Path(payload["screenshot_path"]).read_bytes() != _png()
    names = sorted(child.name for child in session.artifact_dir.iterdir())
    assert names == sorted(Path(payload["screenshot_path"]).name for payload in (observed, snapshot))


async def test_a_redacted_copy_reaches_its_published_name_only_once_redacted(tmp_path: Path) -> None:
    service, session, seen_by_ocr = _service(tmp_path)
    raw = session.artifact_dir / "20260101T000000Z-raw.png"
    raw.write_bytes(_png())

    copy = await service.redacted_copy(session, {"path": str(raw), "url": "/artifacts/session-1/raw.png"}, "diff-a")

    assert _published(seen_by_ocr[0]) == [raw.name]
    assert Path(copy["path"]).read_bytes() != _png()
    assert sorted(child.name for child in session.artifact_dir.iterdir()) == sorted([raw.name, Path(copy["path"]).name])


async def test_a_screenshot_with_nothing_to_redact_is_published_as_it_is(tmp_path: Path) -> None:
    service, session, _ = _service(tmp_path)

    async def no_pii(path: str) -> dict:
        return {"available": True, "blocks": [], "redaction_blocks": [{**_EMAIL_BLOCKS[0], "text": "hello"}]}

    service.manager.ocr.extract_from_image = no_pii

    result = await service.capture_screenshot(session.id)

    assert Path(result["screenshot_path"]).read_bytes() == _png()
    assert sorted(child.name for child in session.artifact_dir.iterdir()) == [Path(result["screenshot_path"]).name]


async def test_the_redacted_bytes_are_never_written_over_the_capture_in_place(tmp_path: Path) -> None:
    # A write cut short in place left a partly written image; the redacted bytes now reach the file through a
    # temporary and os.replace. A write to the very file OCR read fails here, as a full disk would.
    service, session, _ = _service(tmp_path)
    ocr_paths: list[str] = []
    original_ocr = service.manager.ocr.extract_from_image

    async def recording(path: str) -> dict:
        ocr_paths.append(path)
        return await original_ocr(path)

    service.manager.ocr.extract_from_image = recording
    write_bytes = Path.write_bytes

    def failing_in_place(self: Path, data: bytes) -> int:
        if ocr_paths and self == Path(ocr_paths[-1]):
            raise OSError(28, "No space left on device")
        return write_bytes(self, data)

    with patch.object(Path, "write_bytes", failing_in_place):
        result = await service.capture_screenshot(session.id)

    assert "screenshot_pii_scrub" not in result
    assert Path(result["screenshot_path"]).read_bytes() != _png()


@pytest.mark.parametrize("read", ["capture", "observe"])
async def test_the_sessions_last_screenshot_is_never_one_still_being_redacted(tmp_path: Path, read: str) -> None:
    # Review of c4 (Codex 1): a capture became session.last_screenshot as soon as it was written, under its hidden
    # name, so a vision call that reuses the last screenshot (take_screenshot=false) read the raw image while OCR ran.
    service, session, _ = _service(tmp_path)
    earlier = await service.capture_screenshot(session.id)
    ocr_started, release_ocr = asyncio.Event(), asyncio.Event()
    original_ocr = service.manager.ocr.extract_from_image

    async def paused(path: str) -> dict:
        ocr_started.set()
        await release_ocr.wait()
        return await original_ocr(path)

    service.manager.ocr.extract_from_image = paused
    reads = {
        "capture": lambda: service.capture_screenshot(session.id),
        "observe": lambda: service.observation_payload(session, preset="normal"),
    }
    task = asyncio.create_task(reads[read]())
    await asyncio.wait_for(ocr_started.wait(), 5)

    assert checked_screenshot(session)["path"] == earlier["screenshot_path"]
    release_ocr.set()
    latest = await task
    assert checked_screenshot(session)["path"] == latest["screenshot_path"]


@pytest.mark.parametrize("step", ["redacted bytes", "compare copy"])
async def test_a_cancel_while_a_thread_writes_the_hidden_file_leaves_none_behind(tmp_path: Path, step: str) -> None:
    # Review of c4 (Codex 4): the hidden file was deleted when the cancel came, and the worker thread writing it, which
    # a cancel does not stop, wrote it again afterwards.
    service, session, _ = _service(tmp_path)
    started, release = threading.Event(), threading.Event()
    raw = tmp_path / "raw.png"
    raw.write_bytes(_png())

    def paused(function):
        def run(*args):
            started.set()
            release.wait(5)
            return function(*args)

        return run

    if step == "redacted bytes":
        patcher = patch.object(observation, "_write_through_temporary", paused(observation._write_through_temporary))
        read = service.capture_screenshot(session.id)
    else:
        patcher = patch.object(observation.shutil, "copyfile", paused(shutil.copyfile))
        read = service.redacted_copy(session, {"path": str(raw), "url": "/artifacts/session-1/raw.png"}, "diff-a")
    with patcher:
        task = asyncio.ensure_future(read)
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(100):
            await asyncio.sleep(0.01)

    assert list(session.artifact_dir.iterdir()) == []


@pytest.mark.parametrize("read", ["capture", "observe"])
async def test_a_publish_that_fails_leaves_no_hidden_file(tmp_path: Path, read: str) -> None:
    # Review of c4 (Codex 8): _publish dropped the hidden file's marker before moving it, and nothing discarded it when
    # the move failed (its directory's permissions changed, say), so the hidden file stayed.
    service, session, _ = _service(tmp_path)
    replace = os.replace

    def refusing_the_published_name(source: str, destination: str) -> None:
        if not Path(destination).name.startswith("."):
            raise PermissionError(13, "Permission denied")
        replace(source, destination)

    reads = {
        "capture": lambda: service.capture_screenshot(session.id),
        "observe": lambda: service.observation_payload(session, preset="normal"),
    }
    with patch.object(observation.os, "replace", refusing_the_published_name), pytest.raises(PermissionError):
        await reads[read]()

    assert list(session.artifact_dir.iterdir()) == []


async def test_an_observations_ocr_names_the_published_image(tmp_path: Path) -> None:
    # Review of c4 (Codex 9): the OCR result named the hidden file OCR read, which is gone once it is published.
    service, session, _ = _service(tmp_path)
    original_ocr = service.manager.ocr.extract_from_image

    async def naming_its_image(path: str) -> dict:
        return {**await original_ocr(path), "image_path": path}  # as OCRExtractor's result does

    service.manager.ocr.extract_from_image = naming_its_image

    observed = await service.observation_payload(session, preset="normal")

    assert observed["ocr"]["image_path"] == observed["screenshot_path"]


async def test_a_capture_cancelled_while_written_leaves_no_hidden_file(tmp_path: Path) -> None:
    # capture_session_screenshot deletes what a cancelled capture writes once it is written (_delete_when_done):
    # for a staged capture that is the hidden name, not the published one.
    service, session, _ = _service(tmp_path)
    session.page.release.clear()

    task = asyncio.create_task(service.capture_screenshot(session.id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    session.page.release.set()
    for _ in range(50):
        await asyncio.sleep(0.01)

    assert list(session.artifact_dir.iterdir()) == []

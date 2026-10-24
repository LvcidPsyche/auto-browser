"""Screenshot scrubbing needs OCR boxes; without tesseract it must not fail silently.

With the tesseract binary missing, `OCRExtractor` returns an error payload,
`scrub_screenshot` gets no blocks and the image is left as it was. Tesseract
ships only in the Docker image, so a controller run outside Docker with
screenshot scrubbing on used to hand out raw screenshots with no sign of it.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytesseract
import pytest
from PIL import Image
from pytesseract import TesseractNotFoundError

from app.browser.services.observation import BrowserObservationService, _scrub_disclosure
from app.browser_scripts import INTERACTABLES_SCRIPT, PAGE_SUMMARY_SCRIPT
from app.ocr import OCRExtractor, tesseract_available
from app.pii_scrub import PiiScrubber

LOGGER_NAME = "app.browser.services.observation"


class _Page:
    url = "https://example.com"

    async def title(self) -> str:
        return "Example"

    async def aria_snapshot(self, **_: object) -> str:
        return '- heading "Example" [level=1] [ref=e1]\n'

    async def evaluate(self, script: str, *args: object) -> object:
        if script is INTERACTABLES_SCRIPT:
            return []
        assert script is PAGE_SUMMARY_SCRIPT
        return {"title": "Example", "active_element": None, "text_excerpt": "Hello", "dom_outline": {}}


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        id="session-1",
        lock=asyncio.Lock(),
        page=_Page(),
        console_messages=[],
        page_errors=[],
        request_failures=[],
        downloads=[],
    )


def _service(tmp_path: Path, *, ocr_enabled: bool = True) -> BrowserObservationService:
    shot = tmp_path / "shot.png"
    Image.new("RGB", (40, 20), "white").save(shot)
    session = _session()
    manager = SimpleNamespace(
        settings=SimpleNamespace(
            ocr_skip_when_text_available=True,
            perception_preset_default="normal",
            pii_scrub_action_screenshots=True,
        ),
        pii_scrubber=PiiScrubber(screenshot_enabled=True, audit_report=False),
        ocr=OCRExtractor(enabled=ocr_enabled, language="eng", max_blocks=20, text_limit=2000),
        _capture_screenshot=AsyncMock(return_value={"path": str(shot), "url": "/artifacts/shot.png"}),
        _session_summary=AsyncMock(return_value={"id": session.id}),
        _current_takeover_url=MagicMock(return_value=None),
        _assert_page_allowed=MagicMock(),
        remote_access=SimpleNamespace(session_info=MagicMock(return_value={})),
        tabs=SimpleNamespace(summaries=AsyncMock(return_value=[])),
        get_session=AsyncMock(return_value=session),
    )
    manager.session = session
    return BrowserObservationService(manager=manager)


@pytest.fixture
def no_tesseract():
    with patch("app.ocr.pytesseract.image_to_data", side_effect=TesseractNotFoundError()):
        yield


@pytest.fixture
def tesseract_present():
    empty = {"text": [], "conf": [], "left": [], "top": [], "width": [], "height": []}
    with patch("app.ocr.pytesseract.image_to_data", return_value=empty):
        yield


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME and r.levelno == logging.WARNING]


async def test_capture_says_the_screenshot_was_not_scrubbed(tmp_path: Path, no_tesseract: None) -> None:
    service = _service(tmp_path)

    result = await service.capture_screenshot("session-1")

    assert result["screenshot_pii_scrub"] == {"applied": False, "reason": "tesseract_not_found"}


async def test_observe_and_action_snapshots_say_it_too(tmp_path: Path, no_tesseract: None) -> None:
    service = _service(tmp_path)
    session = service.manager.session

    observed = await service.observation_payload(session, preset="normal")
    fast = await service.observation_payload(session, preset="fast")
    snapshot = await service.light_snapshot(session, label="before-click")

    for payload in (observed, fast, snapshot):
        assert payload["screenshot_pii_scrub"] == {"applied": False, "reason": "tesseract_not_found"}


async def test_a_disabled_ocr_engine_is_reported_as_the_reason(tmp_path: Path) -> None:
    service = _service(tmp_path, ocr_enabled=False)

    result = await service.capture_screenshot("session-1")

    assert result["screenshot_pii_scrub"] == {"applied": False, "reason": "ocr_disabled"}


async def test_the_warning_is_logged_once_not_per_screenshot(
    tmp_path: Path, no_tesseract: None, caplog: pytest.LogCaptureFixture
) -> None:
    service = _service(tmp_path)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        for _ in range(3):
            await service.capture_screenshot("session-1")

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "tesseract_not_found" in warnings[0]
    assert "unscrubbed" in warnings[0]


async def test_with_tesseract_present_nothing_is_reported(
    tmp_path: Path, tesseract_present: None, caplog: pytest.LogCaptureFixture
) -> None:
    service = _service(tmp_path)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        captured = await service.capture_screenshot("session-1")
        observed = await service.observation_payload(service.manager.session, preset="normal")

    assert "screenshot_pii_scrub" not in captured
    assert "screenshot_pii_scrub" not in observed
    assert _warnings(caplog) == []


def test_tesseract_available_looks_up_the_binary_pytesseract_runs() -> None:
    # The readiness check and the tests above patch this lookup; this pins what
    # it does: a command that resolves on PATH counts, one that doesn't, doesn't.
    with patch.object(pytesseract.pytesseract, "tesseract_cmd", "no-such-tesseract-binary-8f3a"):
        assert tesseract_available() is False
    with patch.object(pytesseract.pytesseract, "tesseract_cmd", sys.executable):
        assert tesseract_available() is True


# ── Redaction that fails while it runs ─────────────────────────────────────

_EMAIL_BLOCKS = [{"x": 1, "y": 1, "width": 4, "height": 4, "text": "ops@example.com"}]


def _scrub_service(tmp_path: Path, image: bytes) -> tuple[BrowserObservationService, dict[str, str], AsyncMock]:
    service = _service(tmp_path)
    service.manager.pii_scrubber = PiiScrubber(screenshot_enabled=True, audit_report=True)
    service.manager.audit = SimpleNamespace(append=AsyncMock())
    shot = tmp_path / "shot.png"
    shot.write_bytes(image)
    return service, {"path": str(shot), "url": "/artifacts/shot.png"}, service.manager.audit.append


async def test_a_failed_redaction_is_reported_and_never_audited_as_ok(tmp_path: Path) -> None:
    service, screenshot, audit = _scrub_service(tmp_path, b"not-a-png")
    ocr = {"available": True, "redaction_blocks": _EMAIL_BLOCKS}

    await service._scrub_screenshot_if_needed(service.manager.session, screenshot, ocr)

    assert Path(screenshot["path"]).read_bytes() == b"not-a-png"
    assert screenshot["pii_scrub_skipped"] == "redaction_failed"
    assert _scrub_disclosure(screenshot) == {"screenshot_pii_scrub": {"applied": False, "reason": "redaction_failed"}}
    assert [call.kwargs["status"] for call in audit.await_args_list] == ["failed"]
    assert audit.await_args.kwargs["event_type"] == "pii_redaction"


async def test_a_successful_redaction_is_still_audited_as_ok(tmp_path: Path) -> None:
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(image, format="PNG")
    service, screenshot, audit = _scrub_service(tmp_path, image.getvalue())
    ocr = {"available": True, "redaction_blocks": _EMAIL_BLOCKS}

    await service._scrub_screenshot_if_needed(service.manager.session, screenshot, ocr)

    assert Path(screenshot["path"]).read_bytes() != image.getvalue()
    assert "pii_scrub_skipped" not in screenshot
    assert [call.kwargs["status"] for call in audit.await_args_list] == ["ok"]

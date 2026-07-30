"""Tests for the text-first observation preset and OCR gating.

Covers:
- `text` preset skips screenshot capture (screenshot_path/url stay None, keys present)
- `fast`/`normal`/`rich` still capture a screenshot
- OCR is skipped on normal/rich only when text extraction already produced usable
  content AND screenshot PII scrubbing is not active
- OCR is never skipped while PII scrubbing is active, regardless of the config knob
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.browser.services.observation import BrowserObservationService
from app.browser_scripts import ACTIVE_ELEMENT_SCRIPT, INTERACTABLES_SCRIPT, PAGE_SUMMARY_SCRIPT


class FakeAccessibility:
    async def snapshot(self, interesting_only: bool = True) -> dict:
        return {"role": "WebArea", "name": "Example", "children": []}


class FakePage:
    def __init__(self, *, text_excerpt: str = "Hello world") -> None:
        self.url = "https://example.com"
        self.accessibility = FakeAccessibility()
        self._text_excerpt = text_excerpt

    async def title(self) -> str:
        return "Example"

    async def evaluate(self, script: str, *args: object) -> object:
        if script is INTERACTABLES_SCRIPT:
            return [{"element_id": "op-1", "label": "Submit"}]
        if script is PAGE_SUMMARY_SCRIPT:
            return {"text_excerpt": self._text_excerpt, "dom_outline": {"headings": []}}
        if script is ACTIVE_ELEMENT_SCRIPT:
            return {"tag": "body"}
        raise AssertionError(f"unexpected script passed to evaluate: {script!r}")


def _make_session(*, text_excerpt: str = "Hello world") -> SimpleNamespace:
    return SimpleNamespace(
        id="session-1",
        page=FakePage(text_excerpt=text_excerpt),
        console_messages=[],
        page_errors=[],
        request_failures=[],
        downloads=[],
    )


def _make_manager(*, ocr_skip: bool = True, scrubbing_active: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(ocr_skip_when_text_available=ocr_skip),
        pii_scrubber=SimpleNamespace(screenshot_enabled=scrubbing_active, audit_report=False),
        _capture_screenshot=AsyncMock(return_value={"path": "/tmp/shot.png", "url": "/artifacts/shot.png"}),
        _session_summary=AsyncMock(return_value={"id": "session-1"}),
        _current_takeover_url=MagicMock(return_value=None),
        remote_access=SimpleNamespace(session_info=MagicMock(return_value={})),
        tabs=SimpleNamespace(summaries=AsyncMock(return_value=[])),
        ocr=SimpleNamespace(extract_from_image=AsyncMock(return_value={"available": True, "blocks": []})),
    )


class TextPresetTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_preset_skips_screenshot(self) -> None:
        manager = _make_manager()
        service = BrowserObservationService(manager=manager)
        result = await service.observation_payload(_make_session(), preset="text")

        manager._capture_screenshot.assert_not_called()
        self.assertIsNone(result["screenshot_path"])
        self.assertIsNone(result["screenshot_url"])
        self.assertIn("screenshot_path", result)
        self.assertIn("screenshot_url", result)
        self.assertEqual(result["preset"], "text")

    async def test_text_preset_populates_text_and_accessibility(self) -> None:
        manager = _make_manager()
        service = BrowserObservationService(manager=manager)
        result = await service.observation_payload(_make_session(), preset="text")

        self.assertEqual(result["text_excerpt"], "Hello world")
        self.assertTrue(result["accessibility_outline"]["available"])
        self.assertEqual(result["interactables"], [{"element_id": "op-1", "label": "Submit"}])
        self.assertIsNone(result["ocr"])
        manager.ocr.extract_from_image.assert_not_called()

    async def test_fast_preset_still_captures_screenshot(self) -> None:
        manager = _make_manager()
        service = BrowserObservationService(manager=manager)
        result = await service.observation_payload(_make_session(), preset="fast")

        manager._capture_screenshot.assert_awaited_once()
        self.assertEqual(result["screenshot_path"], "/tmp/shot.png")
        self.assertEqual(result["screenshot_url"], "/artifacts/shot.png")

    async def test_normal_preset_still_captures_screenshot(self) -> None:
        manager = _make_manager()
        service = BrowserObservationService(manager=manager)
        result = await service.observation_payload(_make_session(), preset="normal")

        manager._capture_screenshot.assert_awaited_once()
        self.assertEqual(result["screenshot_path"], "/tmp/shot.png")
        self.assertEqual(result["screenshot_url"], "/artifacts/shot.png")


class OcrGatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ocr_skipped_when_text_available_and_scrubbing_inactive(self) -> None:
        manager = _make_manager(ocr_skip=True, scrubbing_active=False)
        service = BrowserObservationService(manager=manager)
        result = await service.observation_payload(_make_session(text_excerpt="Hello world"), preset="normal")

        manager.ocr.extract_from_image.assert_not_called()
        self.assertIsNone(result["ocr"])

    async def test_ocr_runs_when_text_extraction_produced_nothing(self) -> None:
        manager = _make_manager(ocr_skip=True, scrubbing_active=False)
        service = BrowserObservationService(manager=manager)
        result = await service.observation_payload(_make_session(text_excerpt=""), preset="normal")

        manager.ocr.extract_from_image.assert_awaited_once()
        self.assertEqual(result["ocr"], {"available": True, "blocks": []})

    async def test_ocr_never_skipped_while_pii_scrubbing_active(self) -> None:
        manager = _make_manager(ocr_skip=True, scrubbing_active=True)
        service = BrowserObservationService(manager=manager)
        result = await service.observation_payload(_make_session(text_excerpt="Hello world"), preset="normal")

        manager.ocr.extract_from_image.assert_awaited_once()
        self.assertEqual(result["ocr"], {"available": True, "blocks": []})

    async def test_ocr_skip_config_knob_forces_ocr_back_on(self) -> None:
        manager = _make_manager(ocr_skip=False, scrubbing_active=False)
        service = BrowserObservationService(manager=manager)
        await service.observation_payload(_make_session(text_excerpt="Hello world"), preset="normal")

        manager.ocr.extract_from_image.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

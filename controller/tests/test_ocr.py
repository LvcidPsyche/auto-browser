from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError

from app.ocr import OCRExtractor

_EMPTY = {"text": [], "conf": [], "left": [], "top": [], "width": [], "height": []}


class OCRTimeoutTests(unittest.TestCase):
    """ACT-27: image_to_data ran with no timeout, so a tesseract that never finished held its worker thread, and the
    observation or action that waited on it under the session lock, for ever."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.image = Path(directory.name) / "sample.png"
        Image.new("RGB", (20, 20), "white").save(self.image)

    def test_tesseract_runs_with_the_default_or_the_configured_timeout(self) -> None:
        from app.config import OCR_TIMEOUT_SECONDS

        for extractor, timeout in (
            (OCRExtractor(enabled=True, language="eng", max_blocks=5, text_limit=200), OCR_TIMEOUT_SECONDS),
            (OCRExtractor(enabled=True, language="eng", max_blocks=5, text_limit=200, timeout_seconds=7.0), 7.0),
        ):
            with self.subTest(timeout=timeout):
                with patch("app.ocr.pytesseract.image_to_data", return_value=_EMPTY) as run:
                    extractor._extract_sync(self.image)
                self.assertEqual(run.call_args.kwargs.get("timeout"), timeout)

    def test_a_timeout_is_logged_as_one_and_reported_as_a_failed_extraction(self) -> None:
        # pytesseract kills tesseract and raises RuntimeError("Tesseract process timeout"); the catch-all maps it to
        # ocr_extraction_failed, whose hint is about the temporary directory, so the timeout says itself.
        extractor = OCRExtractor(enabled=True, language="eng", max_blocks=5, text_limit=200, timeout_seconds=7.0)

        with patch("app.ocr.pytesseract.image_to_data", side_effect=RuntimeError("Tesseract process timeout")):
            with self.assertLogs("app.ocr", level="WARNING") as logs:
                payload = extractor._extract_sync(self.image)

        self.assertEqual(payload["error"], "ocr_extraction_failed")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("timed out after 7 seconds (OCR_TIMEOUT_SECONDS)", logs.output[0])

    def test_the_setting_defaults_to_the_constant_must_be_above_zero_and_reaches_the_extractor(self) -> None:
        from app.browser_manager import BrowserManager
        from app.config import OCR_TIMEOUT_SECONDS, Settings

        self.assertEqual(Settings(_env_file=None).ocr_timeout_seconds, OCR_TIMEOUT_SECONDS)
        with patch.dict(os.environ, {"OCR_TIMEOUT_SECONDS": "5"}):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.ocr_timeout_seconds, 5.0)
        # 0 would turn pytesseract's timeout off, and a negative one would fail every run.
        for value in ("0", "-1"):
            with self.subTest(value=value), patch.dict(os.environ, {"OCR_TIMEOUT_SECONDS": value}):
                with self.assertRaisesRegex(ValidationError, "OCR_TIMEOUT_SECONDS must be more than 0"):
                    Settings(_env_file=None)
        with tempfile.TemporaryDirectory() as root:
            settings.artifact_root = str(Path(root) / "artifacts")
            self.assertEqual(BrowserManager(settings).ocr.timeout_seconds, 5.0)


class OCRExtractorTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_from_image_returns_bounded_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            image_path = Path(tempdir) / "sample.png"
            Image.new("RGB", (200, 80), "white").save(image_path)
            extractor = OCRExtractor(enabled=True, language="eng", max_blocks=2, text_limit=20)

            with patch("app.ocr.pytesseract.image_to_data") as mock_image_to_data:
                mock_image_to_data.return_value = {
                    "text": ["Hello", "", "World", "Ignored"],
                    "conf": ["92", "-1", "88", "12"],
                    "left": [10, 0, 60, 0],
                    "top": [10, 0, 10, 0],
                    "width": [40, 0, 45, 0],
                    "height": [12, 0, 12, 0],
                }
                payload = await extractor.extract_from_image(image_path)

            self.assertTrue(payload["available"])
            self.assertEqual(payload["dimensions"], {"width": 200, "height": 80})
            self.assertEqual(payload["text_excerpt"], "Hello World")
            self.assertEqual(len(payload["blocks"]), 2)
            self.assertEqual(payload["blocks"][1]["text"], "World")

    async def test_disabled_ocr_returns_empty_payload(self) -> None:
        payload = await OCRExtractor(enabled=False, language="eng", max_blocks=5, text_limit=100).extract_from_image(
            "/tmp/ignored.png"
        )

        self.assertFalse(payload["available"])
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["blocks"], [])

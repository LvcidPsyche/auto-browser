from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytesseract
from PIL import Image
from pytesseract import Output, TesseractNotFoundError

# Tesseract confidence below this is treated as unreliable for the model-facing
# excerpt. It deliberately does NOT gate `redaction_blocks` — see _extract_sync.
_MIN_CONFIDENCE = 30

# Safety ceiling on the redaction block list so a text-dense page cannot grow it
# without bound. Two orders of magnitude above the model-facing cap, which is
# what matters: redaction must not be starved by a token budget.
_REDACTION_BLOCK_CAP = 5000


class OCRExtractor:
    def __init__(self, *, enabled: bool, language: str, max_blocks: int, text_limit: int):
        self.enabled = enabled
        self.language = language
        self.max_blocks = max(1, max_blocks)
        self.text_limit = max(200, text_limit)

    async def extract_from_image(self, image_path: str | Path) -> dict[str, Any]:
        if not self.enabled:
            return {
                "available": False,
                "enabled": False,
                "engine": "tesseract",
                "language": self.language,
                "text_excerpt": "",
                "blocks": [],
                "redaction_blocks": [],
            }
        return await asyncio.to_thread(self._extract_sync, Path(image_path))

    def _extract_sync(self, image_path: Path) -> dict[str, Any]:
        try:
            with Image.open(image_path) as image:
                width, height = image.size
                data = pytesseract.image_to_data(
                    image,
                    lang=self.language,
                    output_type=Output.DICT,
                )
        except TesseractNotFoundError:
            return self._error_payload(image_path, "tesseract_not_found")
        except Exception:  # pragma: no cover - defensive
            return self._error_payload(image_path, "ocr_extraction_failed")

        blocks: list[dict[str, Any]] = []
        redaction_blocks: list[dict[str, Any]] = []
        parts: list[str] = []
        char_count = 0
        for idx, raw_text in enumerate(data.get("text", [])):
            text = str(raw_text or "").strip()
            if not text:
                continue
            try:
                confidence = float(data.get("conf", [])[idx])
            except Exception:
                confidence = -1.0
            block = {
                "text": text,
                "confidence": round(confidence, 2),
                "bbox": {
                    "x": int(data.get("left", [0])[idx]),
                    "y": int(data.get("top", [0])[idx]),
                    "width": int(data.get("width", [0])[idx]),
                    "height": int(data.get("height", [0])[idx]),
                },
            }

            # Redaction sees every block, and deliberately ignores both the
            # confidence floor and max_blocks. image_to_data returns one block
            # per *word*, so capping this at ocr_max_blocks (default 20) meant
            # only the first ~20 words of a page could ever be redacted — a nav
            # bar exhausted the budget before reaching the page body. Dropping
            # low-confidence blocks here would be fail-open for privacy too.
            if len(redaction_blocks) < _REDACTION_BLOCK_CAP:
                redaction_blocks.append(block)

            # The model-facing payload keeps both limits: callers pay tokens for it.
            if confidence < _MIN_CONFIDENCE:
                continue
            if len(blocks) < self.max_blocks:
                blocks.append(block)
                if char_count < self.text_limit:
                    remaining = self.text_limit - char_count
                    chunk = text[:remaining]
                    if chunk:
                        parts.append(chunk)
                        char_count += len(chunk) + 1

        return {
            "available": True,
            "enabled": True,
            "engine": "tesseract",
            "language": self.language,
            "image_path": str(image_path),
            "dimensions": {"width": width, "height": height},
            "text_excerpt": " ".join(parts).strip(),
            "blocks": blocks,
            "redaction_blocks": redaction_blocks,
        }

    def _error_payload(self, image_path: Path, error: str) -> dict[str, Any]:
        return {
            "available": False,
            "enabled": self.enabled,
            "engine": "tesseract",
            "language": self.language,
            "image_path": str(image_path),
            "text_excerpt": "",
            "blocks": [],
            "redaction_blocks": [],
            "error": error,
        }

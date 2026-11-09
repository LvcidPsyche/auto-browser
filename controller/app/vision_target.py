"""
vision_target.py — Claude Vision API for element identification.

When a selector-based approach fails, the agent can describe what it wants
to click/type in natural language. This module sends the current screenshot
to Claude's vision API and asks it to identify the element's coordinates.

Returns: {"x": float, "y": float, "confidence": float, "description": str}

Usage:
    from .vision_target import VisionTargeter
    targeter = VisionTargeter(api_key=settings.anthropic_api_key,
                              model=settings.vision_model)
    result = await targeter.find_element(screenshot_path, "the blue Submit button")
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from .config import ANTHROPIC_DEFAULT_BASE_URL

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class VisionReplyError(RuntimeError):
    """The model's reply cannot be used as an answer: not found is an answer, this is not.

    Its message says what was wrong and never quotes the reply, which is model output about a page.
    """


def _refused(raw_text: str, why: str, error_class: str) -> VisionReplyError:
    """The error for an unusable reply, logged by its length and the failure's class: never the reply itself."""
    logger.warning("vision targeting: unusable reply of %d characters: %s", len(raw_text), error_class)
    return VisionReplyError(f"The vision model's reply could not be used: {why}.")


def _reply_object(raw_text: str) -> dict[str, Any]:
    """The reply as the JSON object the prompt asks for; anything else raises VisionReplyError."""
    text = raw_text
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        reply = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise _refused(raw_text, "it was not JSON", type(exc).__name__) from None
    if not isinstance(reply, dict):
        raise _refused(raw_text, "it was not a JSON object", type(reply).__name__)
    return reply


def _finite_number(value: Any) -> float | None:
    """``value`` as a float, if it is a JSON number a float can hold; a boolean is not one (Python's bool is an int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _checked_point(reply: dict[str, Any], raw_text: str, image_bytes: bytes) -> tuple[int, int, float]:
    """A ``found: true`` reply's point and confidence, checked against the screenshot that was sent."""
    numbers: dict[str, float] = {}
    # confidence has always been optional in a reply (0.0 if missing); a point without an x or a y is not a point.
    for name, value in (("x", reply.get("x")), ("y", reply.get("y")), ("confidence", reply.get("confidence", 0.0))):
        number = _finite_number(value)
        if number is None:
            raise _refused(raw_text, f"its {name} was not a finite number", type(value).__name__)
        numbers[name] = number
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            width, height = image.size
    except Exception as exc:
        # Whatever its decoder raises for an unreadable image: the point cannot be checked, so it is not vouched for.
        raise _refused(raw_text, "the screenshot's size could not be read", type(exc).__name__) from None
    if not (0 <= numbers["x"] < width and 0 <= numbers["y"] < height):
        raise _refused(raw_text, f"its point is outside the screenshot ({width}x{height})", "outside_screenshot")
    return int(numbers["x"]), int(numbers["y"]), numbers["confidence"]


_SYSTEM_PROMPT = """\
You are a precise UI element locator. You will be shown a screenshot of a web page
and asked to find a specific element. Your job is to return the exact center
coordinates (x, y) of that element in the image.

IMPORTANT rules:
- Return ONLY a JSON object with keys: x (int), y (int), confidence (0.0-1.0), found (bool), description (str)
- x and y are pixel coordinates relative to the top-left corner of the image
- confidence: 1.0 = certain, 0.0 = not found
- If the element is not visible, set found=false and x=0, y=0, confidence=0.0
- Do NOT include any text outside the JSON object
- description: brief description of what you found and where it is

Example response:
{"x": 640, "y": 320, "confidence": 0.97, "found": true, "description": "Blue Submit button in the center of the form"}
"""


class VisionTargeter:
    """Uses Claude Vision to locate UI elements from natural language descriptions."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = ANTHROPIC_DEFAULT_BASE_URL,
        model: str = _DEFAULT_MODEL,
        timeout_seconds: float = 30.0,
    ):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for vision-grounded targeting")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def find_element(
        self,
        screenshot_path: str | Path,
        description: str,
    ) -> dict[str, Any]:
        """
        Ask Claude Vision to find an element described in natural language.

        Args:
            screenshot_path: Path to the PNG screenshot file.
            description:     Natural language description of the target element.
                             e.g. "the blue Submit button", "email input field",
                             "the X close button in the top-right corner"

        Returns:
            {
              "x": int, "y": int,
              "found": bool,
              "confidence": float,
              "description": str,
              "selector_hint": str | None,
            }
        """
        path = Path(screenshot_path)
        if not path.exists():
            raise FileNotFoundError(f"Screenshot not found: {path}")

        image_bytes = path.read_bytes()
        image_b64 = base64.standard_b64encode(image_bytes).decode()

        payload = {
            "model": self.model,
            "max_tokens": 256,
            "system": _SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": f"Find this element: {description}",
                        },
                    ],
                }
            ],
        }

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/messages",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        raw_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                raw_text = block.get("text", "").strip()
                break

        reply = _reply_object(raw_text)
        found = reply.get("found")
        if not isinstance(found, bool):
            raise _refused(raw_text, "its found field was missing or neither true nor false", type(found).__name__)
        # A not-found reply's coordinates are not read: it may omit them or send junk.
        x, y, confidence = _checked_point(reply, raw_text, image_bytes) if found else (0, 0, 0.0)
        return {
            "x": x,
            "y": y,
            "found": found,
            "confidence": confidence,
            "description": str(reply.get("description", "")),
            "selector_hint": reply.get("selector_hint"),
            "model": self.model,
        }

    @classmethod
    def from_settings(cls, settings: Any) -> "VisionTargeter | None":
        """Create from auto-browser Settings. Returns None if no API key."""
        api_key = getattr(settings, "anthropic_api_key", None)
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            base_url=getattr(settings, "anthropic_base_url", ANTHROPIC_DEFAULT_BASE_URL),
            model=getattr(settings, "vision_model", None) or _DEFAULT_MODEL,
        )

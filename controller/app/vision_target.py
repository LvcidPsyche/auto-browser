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
import json
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

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
        base_url: str = "https://api.anthropic.com",
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

        image_b64 = base64.standard_b64encode(path.read_bytes()).decode()

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
                f"{self.base_url}/v1/messages",
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

        # Parse JSON response
        try:
            # Strip markdown code fences if present
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
            result = json.loads(raw_text)
        except (json.JSONDecodeError, IndexError) as exc:
            logger.warning("vision targeting: failed to parse response %r: %s", raw_text, exc)
            return {
                "x": 0,
                "y": 0,
                "found": False,
                "confidence": 0.0,
                "description": f"parse error: {exc}",
                "selector_hint": None,
                "raw_response": raw_text,
            }

        return {
            "x": int(result.get("x", 0)),
            "y": int(result.get("y", 0)),
            "found": bool(result.get("found", False)),
            "confidence": float(result.get("confidence", 0.0)),
            "description": str(result.get("description", "")),
            "selector_hint": result.get("selector_hint"),
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
            base_url=getattr(settings, "anthropic_base_url", "https://api.anthropic.com"),
            model=getattr(settings, "vision_model", None) or _DEFAULT_MODEL,
        )

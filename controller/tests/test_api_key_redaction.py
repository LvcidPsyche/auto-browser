"""API keys shown on a page (e.g. Google AI Studio's "API key created" dialog) never leave
the controller in an observation, a snapshot or actions.jsonl; find_api_keys is the one,
pattern-limited read."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from app.browser.services.observation import (
    API_KEY_PATTERNS,
    REDACTED_API_KEY,
    redact_api_keys,
)

KEY = "AIzaSyD" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"[:32]


class RedactionTests(unittest.TestCase):
    def test_key_shaped_strings_are_hidden_everywhere_in_a_payload(self) -> None:
        self.assertEqual(len(KEY), 39)
        payload = {
            "title": "AI Studio",
            "text_excerpt": f"API key created {KEY} Copy",
            "interactables": [{"label": KEY, "element_id": "op-1"}, {"label": "Copy"}],
            "ocr": {"text": f"key: {KEY}"},
            "count": 3,
        }
        clean = redact_api_keys(payload)
        self.assertNotIn(KEY, str(clean))
        self.assertIn(REDACTED_API_KEY, clean["text_excerpt"])
        self.assertEqual(clean["interactables"][1]["label"], "Copy")
        self.assertEqual(clean["count"], 3)

    def test_ordinary_text_is_untouched(self) -> None:
        text = "AIza is a prefix; AIzaShort is not a key"
        self.assertEqual(redact_api_keys(text), text)


class FindApiKeysTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_only_matches_of_the_named_provider(self) -> None:
        from app.browser.services.observation import BrowserObservationService

        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=[KEY, KEY, 42])
        page.url = "https://aistudio.google.com/apikey"
        session = type("S", (), {"page": page, "lock": asyncio.Lock()})()

        async def guarded(session, awaitable, *, what, timeout):
            return await awaitable

        manager = type("M", (), {})()
        manager.get_session = AsyncMock(return_value=session)
        manager.session_lifecycle = type("L", (), {"guarded": staticmethod(guarded)})()
        manager.settings = type("C", (), {"browser_call_timeout_seconds": 5})()
        service = BrowserObservationService(manager)

        result = await service.find_api_keys("s1", "google")
        self.assertEqual(result["keys"], [KEY])
        self.assertEqual(page.evaluate.await_args.args[1], API_KEY_PATTERNS["google"])
        with self.assertRaises(ValueError):
            await service.find_api_keys("s1", "openai")


if __name__ == "__main__":
    unittest.main()

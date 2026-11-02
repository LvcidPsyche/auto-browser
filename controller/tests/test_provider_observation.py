from __future__ import annotations

import unittest

from app.providers.base import BaseProviderAdapter


class ProviderObservationTests(unittest.TestCase):
    def test_compact_observation_keeps_new_perception_fields(self) -> None:
        observation = {
            "session": {"id": "session-1"},
            "url": "https://example.com",
            "title": "Example Domain",
            "active_element": {"tag": "body"},
            "text_excerpt": "Example Domain Learn more",
            "dom_outline": {"headings": [{"level": "h1", "text": "Example Domain"}]},
            "accessibility_outline": {
                "available": True,
                "focused": {"role": "link", "name": "More information"},
                "nodes": [{"role": "heading", "name": "Example Domain"}],
            },
            "ocr": {
                "available": True,
                "text_excerpt": "Example Domain Learn more",
                "blocks": [{"text": "Example", "confidence": 98.0}],
            },
            "interactables": [
                {
                    "element_id": "op-1",
                    "label": "More information",
                    "role": "link",
                    "tag": "a",
                    "type": None,
                    "disabled": False,
                    "href": "https://iana.org/domains/example",
                    "bbox": {"x": 10, "y": 20, "width": 90, "height": 20},
                    "selector_hint": '[data-operator-id="op-1"]',
                    "ignored": "drop me",
                }
            ],
            "console_messages": [],
            "page_errors": [],
            "request_failures": [],
            "tabs": [
                {"index": 0, "active": True, "url": "https://example.com", "title": "Example Domain"},
                {"index": 1, "active": False, "url": "https://example.com/export", "title": "Export"},
            ],
            "takeover_url": "http://127.0.0.1:6080/vnc.html",
        }

        compact = BaseProviderAdapter.compact_observation(observation)

        self.assertEqual(compact["text_excerpt"], "Example Domain Learn more")
        self.assertIn("headings", compact["dom_outline"])
        self.assertTrue(compact["accessibility_outline"]["available"])
        self.assertTrue(compact["ocr"]["available"])
        self.assertEqual(len(compact["tabs"]), 2)
        self.assertNotIn("ignored", compact["interactables"][0])

    def test_compact_observation_keeps_the_stopped_navigation_label(self) -> None:
        """The agent loop (/agent/step, /agent/run, queued and cron runs) prompts from this: a navigation the
        pre-request gate stopped must reach it, or the model sees Chrome's error page with no word on why."""
        label = {
            "origin": "https://refused.example",
            "reason": "off_list",
            "explanation": "A navigation to https://refused.example, a host ALLOWED_HOSTS does not allow, was stopped.",
        }
        observation = {
            "url": "chrome-error://chromewebdata/",
            "title": "refused.example",
            "tabs": [{"index": 0, "active": True, "url": "chrome-error://chromewebdata/", "blocked_navigation": label}],
            "blocked_navigation": label,
        }

        compact = BaseProviderAdapter.compact_observation(observation)

        self.assertEqual(compact["blocked_navigation"], label)

    def test_compact_observation_has_no_label_key_when_nothing_was_stopped(self) -> None:
        compact = BaseProviderAdapter.compact_observation({"url": "https://example.com/", "title": "Example"})

        self.assertNotIn("blocked_navigation", compact)

"""Observation scripts on a fake page, for tests that are not about element ids.

The observation service runs its scripts in each page's isolated world over CDP
(app/browser/element_ids.py), which a fake page cannot provide. A test that
fakes the page gives its manager this instead: each script goes to the fake
page's own evaluate, as the service sent it before. tests/test_element_ids.py
covers the real thing against Chromium.
"""

from __future__ import annotations

from typing import Any


class FakePageElementIds:
    async def evaluate(self, page: Any, script: str, arg: Any = None) -> Any:
        return await page.evaluate(script, arg)

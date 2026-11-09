"""Isolated-world scripts on a fake page, for tests that are not about element ids.

The observation service, and the gateway's get_html text_only and find_elements
(NAV-12), run their scripts in each page's isolated world over CDP
(app/browser/element_ids.py), which a fake page cannot provide. A test that
fakes the page gives its manager this instead: each script goes to the fake
page's own evaluate, as the service sent it before. tests/test_element_ids.py
and tests/test_page_reads_isolated_world_chromium.py cover the real thing
against Chromium.
"""

from __future__ import annotations

from typing import Any


class FakePageElementIds:
    async def evaluate(
        self,
        page: Any,
        script: str,
        arg: Any = None,
        *,
        timeout_ms: int | None = None,
        setup_timeout_ms: int | None = None,
    ) -> Any:
        return await page.evaluate(script, arg)

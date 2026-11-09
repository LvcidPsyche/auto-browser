"""ACT-29: page_summary's two reads finish together, so a failed one leaves nothing running against the page.

The summary script and the ARIA snapshot ran under asyncio.gather, which raises the first error at once and leaves
the other awaitable running: page_summary failed while the snapshot still read the page, after its caller had let go
of the session lock. They now run under _gather_settled, as the observation's other paired reads do.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.browser.services.observation import BrowserObservationService


class _SlowAriaPage:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.snapshots_finished = 0

    async def aria_snapshot(self, **_: object) -> str:
        await self.release.wait()
        self.snapshots_finished += 1
        return '- heading "Example" [level=1] [ref=e1]\n'


async def test_a_failed_summary_script_waits_for_the_aria_snapshot_beside_it() -> None:
    page = _SlowAriaPage()
    element_ids = SimpleNamespace(evaluate=AsyncMock(side_effect=RuntimeError("summary script failed")))
    service = BrowserObservationService(manager=SimpleNamespace(element_ids=element_ids))

    summary = asyncio.create_task(service.page_summary(page))
    await asyncio.sleep(0.05)

    assert not summary.done(), "page_summary gave up while the ARIA snapshot beside it still read the page"
    page.release.set()
    with pytest.raises(RuntimeError, match="summary script failed"):
        await summary
    assert page.snapshots_finished == 1

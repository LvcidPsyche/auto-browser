"""A call to the page awaited within a bound, its end read whatever the caller does.

One function for the actions' bounded calls and the click target's read by selector: as two copies they drifted
once, and the drift was a bug (the actions' copy read the end of a call it gave up on only where the call had not
answered, B1-fix1 item 6).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any


def outcome_left_behind(call: asyncio.Future[Any]) -> None:
    """Retrieve what a call ended with, so asyncio does not report it as never retrieved."""
    if not call.cancelled():
        call.exception()


async def within(call: Awaitable[Any], seconds: float) -> Any:
    """``call``'s answer within ``seconds``, else TimeoutError; the call is cancelled where it has not answered by
    then, or by a cancel of the caller, which is raised. asyncio.wait_for, but for the cancel: on Python 3.11, the
    image's, wait_for answered a caller cancelled in the loop turn its call answered with that answer, and the type
    that was cancelled went on (a keystroke typed, a take read on). asyncio.wait raises the cancel as it comes.
    No time (a take's read once its bound has passed) is the bound's error, as _arrives_by's is."""
    step = asyncio.ensure_future(call)
    try:
        if seconds <= 0:
            # asyncio.wait with none would still find a call that answers within its first step done.
            raise TimeoutError
        done, _ = await asyncio.wait({step}, timeout=seconds)
    finally:
        if not step.done():
            step.cancel()
        # On every way out, an ended call too: its end is nobody's to read once the caller is gone, and a call that
        # failed in the turn its caller was cancelled left the failure, the page's text in it, for asyncio to report.
        step.add_done_callback(outcome_left_behind)
    if not done:
        raise TimeoutError
    return step.result()

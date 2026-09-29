"""Per-tab ring buffers for console/network/page-error diagnostics.

Every open tab gets its own bounded buffers, keyed by the tab's stable id
(see ``app/browser/tab_view.py``), so an employee working in tab A never
sees tab B's console errors or failed requests -- the same isolation
``X-Tab-Id`` already gives clicks/types.

This sits alongside (not instead of) ``BrowserSession.console_messages`` /
``page_errors`` / ``request_failures``, which stay session-wide for the
existing ``observe()`` payload. Nothing here removes those; this is the new,
tab-scoped read used by ``GET /sessions/{id}/diagnostics`` and by the
automatic digest attached to a failed action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_RING_LIMIT = 50
# An old session with hundreds of transient tabs (an employee that opens and
# closes many) should not grow this dict forever -- prune the oldest entry
# once it gets absurdly large.
MAX_TRACKED_TABS = 200


@dataclass
class TabDiagnosticsBuffer:
    console: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[dict[str, Any]] = field(default_factory=list)
    request_failures: list[dict[str, Any]] = field(default_factory=list)
    response_errors: list[dict[str, Any]] = field(default_factory=list)


def bounded_append(items: list[Any], value: Any, limit: int = DEFAULT_RING_LIMIT) -> None:
    items.append(value)
    if len(items) > limit:
        del items[: len(items) - limit]


def get_buffer(real_session: Any, tab_id: str) -> TabDiagnosticsBuffer:
    """The buffer for one tab of ``real_session`` (the unwrapped session),
    created on first use."""
    buffers: dict[str, TabDiagnosticsBuffer] = real_session.tab_diagnostics
    buf = buffers.get(tab_id)
    if buf is None:
        buf = TabDiagnosticsBuffer()
        buffers[tab_id] = buf
        if len(buffers) > MAX_TRACKED_TABS:
            oldest = next(iter(buffers))
            if oldest != tab_id:
                buffers.pop(oldest, None)
    return buf

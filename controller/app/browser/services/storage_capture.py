"""Read a persistent profile's login state without opening a tab in it.

Playwright's `BrowserContext.storage_state()` collects localStorage for every
origin the context has visited by opening a NEW PAGE (Target.createTarget, a
foreground tab), routing it through each origin in turn and closing it. On a
persistent profile that page is a real tab in the owner's headed window: the
3-minute auto-persist (compare + save = two calls) flashed a tab and stole
focus twice per tick, and every origin visit spawned a fresh renderer process
-- exactly the thread churn that pushed browser-node over its pid cap on
2026-09-25.

For a persistent profile the on-disk profile IS the remembered login; the
encrypted export is a backup and the seed for a brand-new profile. Cookies
(where every sign-in lives) are read with one browser-level CDP call, and
localStorage only from tabs that are already open -- nothing is created.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_READ_LOCAL_STORAGE = """() => {
  try {
    const origin = window.location.origin;
    if (!origin || origin === "null") return null;
    const items = [];
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const name = window.localStorage.key(i);
      items.push({ name, value: window.localStorage.getItem(name) });
    }
    return { origin, localStorage: items };
  } catch (e) {
    return null;
  }
}"""


class TablessStorageState:
    """Duck-types `BrowserContext.storage_state()` for AuthStateManager."""

    def __init__(self, context: Any, *, page_timeout_seconds: float = 5.0) -> None:
        self._context = context
        self._page_timeout_seconds = page_timeout_seconds

    async def storage_state(self, path: str | Path | None = None) -> dict[str, Any]:
        cookies = await self._context.cookies()
        origins: dict[str, dict[str, Any]] = {}
        for page in list(self._context.pages):
            try:
                if page.is_closed():
                    continue
                entry = await asyncio.wait_for(page.evaluate(_READ_LOCAL_STORAGE), self._page_timeout_seconds)
            except Exception as exc:
                # A tab mid-navigation, showing a dialog, or crashed: skip it,
                # the cookies (the sign-ins) are already captured.
                logger.debug("localStorage read skipped for one tab: %s", exc)
                continue
            if isinstance(entry, dict) and entry.get("localStorage"):
                origins[entry["origin"]] = entry
        state = {"cookies": cookies, "origins": list(origins.values())}
        if path is not None:
            Path(path).write_text(json.dumps(state), encoding="utf-8")
        return state


def storage_state_source(session: Any) -> Any:
    """What to read a session's storage state from without disturbing it."""
    if getattr(session, "persistent_profile_name", None):
        return TablessStorageState(session.context)
    return session.context

"""Judge a window by its CDP target's birth, not by its first request and not by Playwright's report.

Playwright reports a popup only once its first navigation has committed. A window a refused page opens
whose first navigation commits nothing (a ``204``/``205``, a failure, a hang) or makes no request at all
(``window.open('javascript:void(0)')``, a blocked top-level ``data:`` URL, a blank the opener writes into)
stays unreported, on the blank it opened with, which carries the opener's origin. Meanwhile the agent's
navigate clears the opener, and a timer the opener planted sends the window to an allowed URL carrying the
secret: the opener is clean by the time the window is reported, so the old opener-at-report verdict let it
through, and the secret reached the agent through ``list_tabs``, observe and the console.

The browser tells the controller about the window at its target's creation, before any navigation, through
CDP (``Target.setDiscoverTargets`` / ``Target.targetCreated``). ``TargetInfo`` carries the window's
``openerId`` and ``browserContextId``. A page target born with an opener while its session counts as refused
(its opener is refused, another tab is, or a tab that counted closed within the grace) is **born refused**,
whatever its first navigation does. Then:

* a born-refused window Playwright reports is tainted under the existing refused-popup rule and stays refused;
* a born-refused window Playwright never reports is closed through CDP (``Target.closeTarget``) as soon as its
  session stops counting as refused (the agent's navigate, a blocked action's rollback, or a close): nothing
  it planted can then commit an allowed URL or navigate the agent's own tab.

One entry per live page target, dropped on ``Target.targetDestroyed``; nothing per request, so a navigation
flood cannot grow the state. A target with no opener (the controller's own ``context.new_page()``: a session's
first page, a tab the open-tab tool opens) is never born refused, so those flows are untouched.

The watcher keeps its own browser-level CDP session rather than sharing the pre-request gate's. It is then
identical whether the gate is on, off (``PRE_REQUEST_BLOCKING=false``, where the old gap was a full bypass),
lost, or in an attach mode, and needs nothing from the gate's lifecycle. When a target's session or opener
cannot be determined it fails closed (the window is refused), never open.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# The whole attach (the browser session and target discovery), and a deliberate detach.
ATTACH_TIMEOUT_SECONDS = 10.0
DETACH_TIMEOUT_SECONDS = 2.0
# Reading a new context's browserContextId when the gate did not already supply one.
CONTEXT_ID_TIMEOUT_SECONDS = 2.0
# CDP target types that are windows of their own; only these ever become birth entries.
PAGE_TARGET_TYPE = "page"


@dataclass
class _Birth:
    """One live page target the watcher has seen. Dropped on ``Target.targetDestroyed``."""

    target_id: str
    context_id: str | None
    opener_id: str | None
    born_refused: bool
    # Set once Playwright reports the window (mark_reported): a reported window is judged by the
    # refused-popup rule and is never closed from here.
    reported: bool = False
    closed: bool = False


class WindowBirthWatcher:
    """One browser's window-birth watcher: a browser-level CDP session that judges page targets at birth."""

    def __init__(
        self,
        browser: Any,
        *,
        session_counts_as_refused: Callable[[Any], bool],
        on_late_refused: Callable[[Any, Any], None],
    ) -> None:
        self._browser = browser
        self._counts = session_counts_as_refused
        self._on_late_refused = on_late_refused
        self._cdp: Any = None
        self._live = False
        self._closed = False
        # One entry per live page target, by targetId.
        self._births: dict[str, _Birth] = {}
        # Sessions on this browser, by their browserContextId: a targetCreated carries only the context id.
        self._sessions: dict[str, Any] = {}
        # A window Playwright reported before its targetCreated arrived (the event-order race): tainted when
        # its birth lands if it is born refused. Weak, so a closed page does not linger here.
        self._reported_before_birth: "weakref.WeakValueDictionary[str, Any]" = weakref.WeakValueDictionary()

    @property
    def live(self) -> bool:
        return self._live and not self._closed

    async def attach(self) -> None:
        cdp = await asyncio.wait_for(self._browser.new_browser_cdp_session(), timeout=ATTACH_TIMEOUT_SECONDS)
        self._cdp = cdp
        cdp.on("Target.targetCreated", self._on_target)
        cdp.on("Target.targetInfoChanged", self._on_target)
        cdp.on("Target.targetDestroyed", self._on_target_destroyed)
        cdp.on("close", self._on_close)
        await asyncio.wait_for(
            cdp.send("Target.setDiscoverTargets", {"discover": True}), timeout=ATTACH_TIMEOUT_SECONDS
        )
        self._live = True

    async def detach(self) -> None:
        self._live = False
        self._closed = True
        if self._cdp is None:
            return
        try:
            await asyncio.wait_for(self._cdp.detach(), timeout=DETACH_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.debug("window-birth watcher: could not detach its session: %s", f"{type(exc).__name__}: {exc}"[:200])

    def _on_close(self, _session: Any = None) -> None:
        # The browser's session went down with it (disconnect, close): the watcher is dead, and its births
        # died with the targets. A later send would error; mark it not live so callers skip it.
        self._closed = True
        self._live = False

    # ── sessions ──────────────────────────────────────────────────────────────

    def register_session(self, context_id: str, session: Any) -> None:
        self._sessions[context_id] = session

    def forget_session(self, session: Any) -> None:
        for context_id in [cid for cid, known in self._sessions.items() if known is session]:
            del self._sessions[context_id]

    def _context_id_of(self, session: Any) -> str | None:
        for context_id, known in self._sessions.items():
            if known is session:
                return context_id
        return None

    # ── CDP events ──────────────────────────────────────────────────────────────

    def _on_target(self, event: dict[str, Any]) -> None:
        info = event.get("targetInfo") or {}
        if info.get("type") != PAGE_TARGET_TYPE:
            return
        target_id = info.get("targetId")
        if not target_id:
            return
        context_id = info.get("browserContextId")
        opener_id = info.get("openerId")
        existing = self._births.get(target_id)
        if existing is not None:
            # A target we already judged: keep its birth verdict (a later info change must not downgrade it),
            # only fill in a context id that was not known at creation.
            if existing.context_id is None and context_id is not None:
                existing.context_id = context_id
            return
        session = self._sessions.get(context_id) if context_id is not None else None
        # A page target in a context the controller did not create (an operator's own tab under an attach
        # mode's CDP_CONNECT_URL) is not ours: no entry, so it is never judged or closed.
        if session is None:
            return
        # No opener: the controller's own context.new_page() (a session's first page, an open-tab tool tab),
        # not a window a page opened. Those are never born refused. An opener while the session counts as
        # refused is: its opener is refused, another tab is, or one that counted closed within the grace.
        born_refused = bool(opener_id) and self._counts(session)
        self._births[target_id] = _Birth(
            target_id=target_id, context_id=context_id, opener_id=opener_id, born_refused=born_refused
        )
        if born_refused:
            page = self._reported_before_birth.pop(target_id, None)
            if page is not None:
                # The report raced ahead of the birth: taint the window now (it was held refused meanwhile).
                self._births[target_id].reported = True
                self._on_late_refused(session, page)

    def _on_target_destroyed(self, event: dict[str, Any]) -> None:
        target_id = event.get("targetId")
        if target_id:
            self._births.pop(target_id, None)
            self._reported_before_birth.pop(target_id, None)

    # ── reporting and closing ──────────────────────────────────────────────────

    def mark_reported(self, session: Any, target_id: str | None) -> bool:
        """Record that Playwright reported the window with ``target_id`` and return whether it is born refused.

        Fails closed: a window whose targetId could not be read, or whose birth has not arrived yet (the
        event-order race), is refused while the session counts as refused. A born-refused birth that is
        still unreported is what the sweep closes, so marking it reported here is what spares a reported
        window from being closed.
        """
        if target_id is None:
            return self._counts(session)
        birth = self._births.get(target_id)
        if birth is None:
            # The report arrived before the targetCreated event: remember the page so its birth can taint it,
            # and refuse it meanwhile if the session counts.
            return self._counts(session)
        birth.reported = True
        return birth.born_refused

    def remember_reported_page(self, target_id: str | None, page: Any) -> None:
        """Hold a reported page against its targetId so a birth that lands later (the race) can taint it."""
        if target_id is not None and target_id not in self._births:
            with contextlib.suppress(TypeError):
                self._reported_before_birth[target_id] = page

    async def close_unreported(self, session: Any) -> None:
        """Close every born-refused window of ``session`` Playwright has not reported, once the session no
        longer counts as refused. While it still counts the windows are still justified, so nothing is closed."""
        if not self.live or self._counts(session):
            return
        context_id = self._context_id_of(session)
        if context_id is None:
            return
        for birth in list(self._births.values()):
            if birth.context_id != context_id or not birth.born_refused or birth.reported or birth.closed:
                continue
            birth.closed = True
            try:
                await self._cdp.send("Target.closeTarget", {"targetId": birth.target_id})
            except Exception as exc:
                birth.closed = False
                logger.warning(
                    "window-birth watcher: could not close an unreported refused window: %s",
                    f"{type(exc).__name__}: {exc}"[:200],
                )


class WindowBirths:
    """The window-birth watcher of every Browser the controller uses, attached where a session is watched."""

    def __init__(
        self,
        *,
        session_counts_as_refused: Callable[[Any], bool],
        resolve_target_id: Callable[[Any], Awaitable[str | None]],
        on_late_refused: Callable[[Any, Any], None],
    ) -> None:
        self._counts = session_counts_as_refused
        self._resolve_target_id = resolve_target_id
        self._on_late_refused = on_late_refused
        # By id(browser): a browser is held until released, so its id is not reused meanwhile.
        self._watchers: dict[int, tuple[Any, WindowBirthWatcher]] = {}
        self._lock = asyncio.Lock()

    def _get(self, browser: Any) -> WindowBirthWatcher | None:
        entry = self._watchers.get(id(browser)) if browser is not None else None
        return entry[1] if entry is not None and entry[1].live else None

    async def watch(self, session: Any, context: Any, page: Any, *, context_id: str | None) -> None:
        """Watch a session's windows: attach a birth watcher to its browser (once per browser) and map its
        browserContextId to the session. ``context_id`` is the gate's when it owns the context, else read here.

        A failure to attach is logged, not raised: the birth watcher is defence in depth over the page guard,
        which still refuses a window that commits a refused URL. (The pre-request gate's own absence already
        refuses a gated session; this watcher runs with the gate off too.)
        """
        browser = getattr(session, "browser", None)
        if browser is None:
            return
        try:
            if context_id is None:
                context_id = await self._read_context_id(context, page)
            watcher = await self._ensure(browser)
            if watcher is not None and context_id is not None:
                watcher.register_session(context_id, session)
        except Exception as exc:
            logger.warning(
                "window-birth watcher: could not watch session %s: %s",
                getattr(session, "id", "?"),
                f"{type(exc).__name__}: {exc}"[:200],
            )

    async def _ensure(self, browser: Any) -> WindowBirthWatcher | None:
        async with self._lock:
            existing = self._watchers.get(id(browser))
            if existing is not None and existing[1].live:
                return existing[1]
            watcher = WindowBirthWatcher(
                browser,
                session_counts_as_refused=self._counts,
                on_late_refused=self._on_late_refused,
            )
            await watcher.attach()
            self._watchers[id(browser)] = (browser, watcher)
            return watcher

    @staticmethod
    async def _read_context_id(context: Any, page: Any) -> str | None:
        session = await context.new_cdp_session(page)
        try:
            info = (await asyncio.wait_for(session.send("Target.getTargetInfo"), timeout=CONTEXT_ID_TIMEOUT_SECONDS))[
                "targetInfo"
            ]
        finally:
            with contextlib.suppress(Exception):
                await session.detach()
        return info.get("browserContextId")

    def active_for(self, session: Any) -> bool:
        """Whether a live birth watcher judges this session's windows (false in the unit fakes, where the
        opener-at-report verdict governs on its own)."""
        return self._get(getattr(session, "browser", None)) is not None

    async def mark_reported(self, session: Any, page: Any) -> bool:
        """Resolve the reported window's targetId, mark its birth reported, and return whether it is born refused."""
        watcher = self._get(getattr(session, "browser", None))
        if watcher is None:
            return False
        target_id = await self._resolve_target_id(page)
        watcher.remember_reported_page(target_id, page)
        return watcher.mark_reported(session, target_id)

    async def close_unreported(self, session: Any) -> None:
        watcher = self._get(getattr(session, "browser", None))
        if watcher is not None:
            await watcher.close_unreported(session)

    def forget_session(self, session: Any) -> None:
        watcher = self._get(getattr(session, "browser", None))
        if watcher is not None:
            watcher.forget_session(session)

    async def release(self, browser: Any) -> None:
        async with self._lock:
            entry = self._watchers.pop(id(browser), None)
        if entry is not None:
            await entry[1].detach()

    async def release_all(self) -> None:
        for browser, _watcher in list(self._watchers.values()):
            await self.release(browser)

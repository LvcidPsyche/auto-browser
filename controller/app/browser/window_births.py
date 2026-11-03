"""Judge a window by its CDP target's birth, not by its first request and not by Playwright's report.

Playwright reports a popup only once its first navigation has committed. A window a refused page opens
whose first navigation commits nothing (a ``204``/``205``, a failure, a hang) or makes no request at all
(``window.open('javascript:void(0)')``, a blocked top-level ``data:`` URL, a blank the opener writes into)
stays unreported, on the blank it opened with, which carries the opener's origin. Meanwhile the agent's
navigate clears the opener, and a script the opener planted sends the window to an allowed URL carrying the
secret: the opener is clean by the time the window is reported, so the old opener-at-report verdict let it
through, and the secret reached the agent through ``list_tabs``, observe and the console.

The browser tells the controller about the window at its target's creation, before any navigation, through
CDP (``Target.setDiscoverTargets`` / ``Target.targetCreated``). ``TargetInfo`` carries the window's
``openerId`` and ``browserContextId``. The verdict at birth, **born refused**, is keyed on the opener:

* the opener is a born-refused window itself (reported or not): born refused, whatever the session does
  meanwhile (a window a refused page's window opens after the agent's navigate is still that page's);
* the opener is a page the controller knows (the session's first page, a tab the open-tab tool opened, a
  reported popup: each has its targetId cached): born refused when that page is refused now, or when a page
  that counted for the session closed within the grace (``page_provenance.CLOSED_PAGE_GRACE_SECONDS``);
* the opener cannot be matched to a page (its targetId is not cached yet, or it is a clean window Playwright
  has not reported): born refused when the session counts as refused, and that verdict is marked as the
  session's, so the refusal names another tab as its cause;
* no opener (the controller's own ``context.new_page()``: a session's first page, an open-tab tool tab):
  never born refused.

Then:

* a born-refused window Playwright reports is refused as a popup of a refused opener is (``opener_judged``);
  a reported window whose opener was clean at report is held refused until its birth verdict is known, so
  nothing it logs or shows reaches the agent meanwhile;
* a born-refused window Playwright never reports is closed through CDP (``Target.closeTarget``) as soon as
  it is no longer justified: its opener is clean (the agent's navigate, a blocked action's rollback) or gone
  (past the grace), or, for a window refused by the session fallback, the session no longer counts. The
  sweep runs at the agent's navigate, at a rollback, after a counting page's close, and on a timer while any
  such window is live, so whatever ended the refusal the window does not outlive it. It never closes a page
  Playwright has reported: every reported page's target is marked, and the sweep skips the session's pages.

One entry per live page target, dropped on ``Target.targetDestroyed``; nothing per request, so a navigation
flood cannot grow the state.

The watcher keeps its own browser-level CDP session rather than sharing the pre-request gate's. It is then
identical whether the gate is on, off (``PRE_REQUEST_BLOCKING=false``, where the old gap was a full bypass),
lost, or in an attach mode. While ALLOWED_HOSTS refuses any host it fails closed: a session whose browser
has no watcher (the attach failed, or the context's id could not be read) is not created
(``WindowBirthWatcherUnavailable``, as the gate's ``RequestGateUnavailable``); a reported window whose
targetId cannot be read, or whose birth the watcher never saw, is refused; and when the watcher's session dies
the popups of every session on that browser are refused until a new watcher attaches, which re-registers them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from ..action_errors import WINDOW_BIRTH_WATCHER_UNAVAILABLE, BrowserActionError
from ..utils import spawn_background_task

logger = logging.getLogger(__name__)

# The whole attach (the browser session and target discovery), and a deliberate detach.
ATTACH_TIMEOUT_SECONDS = 10.0
DETACH_TIMEOUT_SECONDS = 2.0
# After a failed attach, how long before the next session tries again on that browser.
ATTACH_RETRY_BACKOFF_SECONDS = 2.0
# Reading a new context's browserContextId when the gate did not already supply one.
CONTEXT_ID_TIMEOUT_SECONDS = 2.0
# How often a watcher re-checks its live born-refused unreported windows, while it has any.
RECHECK_INTERVAL_SECONDS = 1.0
# How long the sweep waits for the targetId reads of the session's reported pages before it closes anything.
REPORTED_IDS_TIMEOUT_SECONDS = 3.0
# CDP target types that are windows of their own; only these ever become birth entries.
PAGE_TARGET_TYPE = "page"

WATCHER_REQUIRED = (
    "Sessions are refused on this browser while ALLOWED_HOSTS refuses any host; ALLOWED_HOSTS=* runs without it."
)


class WindowBirthWatcherUnavailable(BrowserActionError):
    """No window-birth watcher on a browser (the attach failed, or a new context's id could not be read):
    sessions there are refused, as they are without the pre-request gate. Retryable: the next session attaches."""

    def __init__(self, message: str) -> None:
        super().__init__(
            f"{message.rstrip('.')}. {WATCHER_REQUIRED}",
            code=WINDOW_BIRTH_WATCHER_UNAVAILABLE,
            status_code=503,
            retryable=True,
        )


class BirthVerdict(Enum):
    CLEAN = "clean"
    REFUSED = "refused"
    # Refused by the session-wide fallback (the opener could not be matched to a page): another tab is the cause.
    REFUSED_BY_SESSION = "refused_by_session"


@dataclass
class _Birth:
    """One live page target the watcher has seen. Dropped on ``Target.targetDestroyed``."""

    target_id: str
    context_id: str | None
    opener_id: str | None
    born_refused: bool
    # The verdict came from the session-wide fallback, not from the opener itself.
    by_session: bool = False
    # Set once Playwright reported the window (mark_reported): a reported window is never closed from here.
    reported: bool = False
    closed: bool = False
    # The reported window's Page, weakly: a window it opens is judged by it.
    page: Any = None

    def live_page(self) -> Any:
        page = self.page() if self.page is not None else None
        is_closed = getattr(page, "is_closed", None)
        if page is None or (callable(is_closed) and is_closed() is True):
            return None
        return page


class WindowBirthWatcher:
    """One browser's window-birth watcher: a browser-level CDP session that judges page targets at birth."""

    def __init__(
        self,
        browser: Any,
        *,
        opener_refused: Callable[[Any, Any], bool],
        session_counts_as_refused: Callable[[Any], bool],
        page_by_target_id: Callable[[Any, str], Any],
        on_lost: Callable[["WindowBirthWatcher"], None] | None = None,
        on_recheck: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        self._browser = browser
        # The sweep for one session (WindowBirths.close_unreported, which adds the reported-page protection).
        self._on_recheck = on_recheck
        # Whether a window whose opener is this page is born refused: the page is refused, or a counting page
        # closed within the grace (the manager's rule).
        self._opener_refused = opener_refused
        self._counts = session_counts_as_refused
        # A page of the session by its cached targetId, with no I/O (the manager's cache).
        self._page_by_target_id = page_by_target_id
        self._on_lost = on_lost
        self._cdp: Any = None
        self._live = False
        self._closed = False
        self._deliberate = False
        # One entry per live page target, by targetId.
        self._births: dict[str, _Birth] = {}
        # Sessions on this browser, by their browserContextId: a targetCreated carries only the context id.
        self._sessions: dict[str, Any] = {}
        # A window Playwright reported before its targetCreated arrived: refused already (mark_reported fails
        # closed); its birth, when it lands, is marked reported so the sweep never closes it. Weak, so a closed
        # page does not linger here.
        self._reported_before_birth: "weakref.WeakValueDictionary[str, Any]" = weakref.WeakValueDictionary()
        self._recheck: asyncio.Task[Any] | None = None

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
        try:
            await asyncio.wait_for(
                cdp.send("Target.setDiscoverTargets", {"discover": True}), timeout=ATTACH_TIMEOUT_SECONDS
            )
        except BaseException:
            # Cancellation too: a browser session left attached, with its handlers, would be orphaned per retry.
            await self._detach_cdp()
            raise
        if self._closed:
            raise RuntimeError("the window-birth watcher's session closed while it attached")
        self._live = True

    async def detach(self) -> None:
        self._live = False
        await self._detach_cdp()

    async def _detach_cdp(self) -> None:
        self._deliberate = True
        self._closed = True
        self._stop_recheck()
        if self._cdp is None:
            return
        try:
            await asyncio.wait_for(self._cdp.detach(), timeout=DETACH_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.debug("window-birth watcher: could not detach its session: %s", f"{type(exc).__name__}: {exc}"[:200])

    def _on_close(self, _session: Any = None) -> None:
        # The browser's session went down (disconnect, close, or on its own): the watcher is dead. Its births
        # died with the targets if the browser did; if the browser lives, a replacement attaches (on_lost).
        was_live = self.live
        self._closed = True
        self._live = False
        self._stop_recheck()
        if was_live and not self._deliberate and self._on_lost is not None:
            self._on_lost(self)

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

    @property
    def session_count(self) -> int:
        return len(self._sessions)

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
        born_refused, by_session = self._judge(session, opener_id)
        birth = self._births[target_id] = _Birth(
            target_id=target_id,
            context_id=context_id,
            opener_id=opener_id,
            born_refused=born_refused,
            by_session=by_session,
        )
        page = self._reported_before_birth.pop(target_id, None)
        if page is not None:
            # The report raced ahead of the birth: the window was refused at report; it is a reported page.
            birth.reported = True
            with contextlib.suppress(TypeError):
                birth.page = weakref.ref(page)
        if born_refused and not birth.reported:
            self._start_recheck()

    def _judge(self, session: Any, opener_id: str | None) -> tuple[bool, bool]:
        """(born refused, by the session fallback) for a window born with ``opener_id``: see the module doc."""
        if not opener_id:
            return False, False
        opener_birth = self._births.get(opener_id)
        if opener_birth is not None and opener_birth.born_refused:
            return True, False
        opener = self._opener_page(session, opener_id)
        if opener is not None:
            return self._opener_refused(session, opener), False
        return self._counts(session), True

    def _opener_page(self, session: Any, opener_id: str) -> Any:
        """The open Page with this targetId: from the manager's cache, or the birth's own reference."""
        page = self._page_by_target_id(session, opener_id)
        if page is not None:
            return page
        birth = self._births.get(opener_id)
        return birth.live_page() if birth is not None else None

    def _on_target_destroyed(self, event: dict[str, Any]) -> None:
        target_id = event.get("targetId")
        if target_id:
            self._births.pop(target_id, None)
            self._reported_before_birth.pop(target_id, None)

    # ── reporting ───────────────────────────────────────────────────────────────

    def mark_reported(self, session: Any, target_id: str | None, page: Any) -> BirthVerdict:
        """Record that Playwright reported the window with ``target_id`` and return its birth verdict.

        Fails closed: a window whose targetId could not be read, or whose birth has not arrived yet, is refused
        (in real Chromium the birth precedes the report; a report with no birth is unexplained). Marking the birth
        reported is what spares the window from the sweep.
        """
        if target_id is None:
            return BirthVerdict.REFUSED
        birth = self._births.get(target_id)
        if birth is None:
            with contextlib.suppress(TypeError):
                self._reported_before_birth[target_id] = page
            return BirthVerdict.REFUSED
        birth.reported = True
        with contextlib.suppress(TypeError):
            birth.page = weakref.ref(page)
        if not birth.born_refused:
            return BirthVerdict.CLEAN
        return BirthVerdict.REFUSED_BY_SESSION if birth.by_session else BirthVerdict.REFUSED

    # ── closing unreported born-refused windows ─────────────────────────────────

    def _session_births(self, session: Any) -> list[_Birth]:
        context_id = self._context_id_of(session)
        if context_id is None:
            return []
        return [birth for birth in self._births.values() if birth.context_id == context_id]

    def _unjustified(self, session: Any) -> list[_Birth]:
        """The session's born-refused windows Playwright has not reported that nothing justifies any more."""
        return [
            birth
            for birth in self._session_births(session)
            if birth.born_refused and not birth.reported and not birth.closed and not self._justified(session, birth)
        ]

    def _justified(self, session: Any, birth: _Birth, depth: int = 0) -> bool:
        """Whether the window is still a refused page's: its opener is refused now, or is itself a live
        unreported born-refused window that is, or, for a session-fallback verdict, the session still counts.
        An opener that is gone or cannot be matched justifies nothing past the grace (the session count)."""
        if birth.by_session:
            return self._counts(session)
        opener_birth = self._births.get(birth.opener_id) if birth.opener_id else None
        if (
            opener_birth is not None
            and opener_birth.born_refused
            and not opener_birth.reported
            and not opener_birth.closed
            and depth < 8
        ):
            return self._justified(session, opener_birth, depth + 1)
        opener = self._opener_page(session, birth.opener_id) if birth.opener_id else None
        if opener is not None:
            return self._opener_refused(session, opener)
        return self._counts(session)

    def has_unreported_born_refused(self, session: Any | None = None) -> bool:
        births = self._session_births(session) if session is not None else list(self._births.values())
        return any(birth.born_refused and not birth.reported and not birth.closed for birth in births)

    async def close_unreported(self, session: Any, reported_target_ids: Callable[[Any], Awaitable[set[str]]]) -> None:
        """Close every born-refused window of ``session`` that Playwright has not reported and that is no longer
        justified (``_justified``). ``reported_target_ids`` gives the targetIds of the session's reported pages,
        which are never closed whatever the registry says."""
        if not self.live or not self._unjustified(session):
            return
        protected = await reported_target_ids(session)
        for birth in self._unjustified(session):
            if birth.target_id in protected:
                birth.reported = True
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

    def _start_recheck(self) -> None:
        if self._recheck is not None and not self._recheck.done():
            return
        with contextlib.suppress(RuntimeError):  # no running loop (never in the controller; the fakes' setup)
            self._recheck = asyncio.get_running_loop().create_task(self._recheck_loop())

    def _stop_recheck(self) -> None:
        if self._recheck is not None and not self._recheck.done():
            self._recheck.cancel()
        self._recheck = None

    async def _recheck_loop(self) -> None:
        """While any born-refused unreported window is live: re-check every RECHECK_INTERVAL_SECONDS and close
        those nothing justifies any more, whatever ended the refusal (a page leaving its blank, a failed refused
        hop) and whether or not an agent action followed it."""
        while self.live and self.has_unreported_born_refused():
            await asyncio.sleep(RECHECK_INTERVAL_SECONDS)
            if not self.live:
                return
            for session in list(self._sessions.values()):
                if self._unjustified(session) and self._on_recheck is not None:
                    await self._on_recheck(session)

    # ── readiness ───────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        return {
            "state": "attached" if self.live else "lost",
            "sessions": len(self._sessions),
            "born_refused_unreported": sum(
                1 for birth in self._births.values() if birth.born_refused and not birth.reported and not birth.closed
            ),
        }


class WindowBirths:
    """The window-birth watcher of every Browser the controller uses, attached where a session is watched."""

    def __init__(
        self,
        *,
        enabled: Callable[[], bool],
        opener_refused: Callable[[Any, Any], bool],
        session_counts_as_refused: Callable[[Any], bool],
        page_by_target_id: Callable[[Any, str], Any],
        session_pages: Callable[[Any], list[Any]],
        resolve_target_id: Callable[[Any], Awaitable[str | None]],
        known_target_id: Callable[[Any], str | None],
        seed_target_id: Callable[[Any, str], None],
    ) -> None:
        # Whether ALLOWED_HOSTS refuses any host: when every host is allowed nothing is ever born refused, so
        # the watcher attaches to nothing (unlike the gate, it runs with PRE_REQUEST_BLOCKING off).
        self._enabled = enabled
        self._opener_refused = opener_refused
        self._counts = session_counts_as_refused
        self._page_by_target_id = page_by_target_id
        self._session_pages = session_pages
        self._resolve_target_id = resolve_target_id
        self._known_target_id = known_target_id
        # Cache a page's targetId so a later window's openerId can be matched to it with no I/O; the gate seeds
        # the first page when it is on, this seeds it when it is off.
        self._seed_target_id = seed_target_id
        # By id(browser): a browser is held until released, so its id is not reused meanwhile.
        self._watchers: dict[int, tuple[Any, WindowBirthWatcher]] = {}
        # Why, and when, the last attach to a browser failed, until one succeeds (readiness, the retry backoff).
        self._attach_failures: dict[int, tuple[Any, str, float]] = {}
        # Every watched session with its context id, by id(session): re-registered when its browser's watcher
        # is replaced after a loss, and the sessions whose popups are held refused while it has none.
        self._registered: dict[int, tuple[Any, str]] = {}
        # Each reported page's birth verdict, resolved once (its targetId read) and shared by every caller.
        self._reports: "weakref.WeakKeyDictionary[Any, asyncio.Task[BirthVerdict]]" = weakref.WeakKeyDictionary()
        self._lock = asyncio.Lock()

    def _get(self, browser: Any) -> WindowBirthWatcher | None:
        entry = self._watchers.get(id(browser)) if browser is not None else None
        return entry[1] if entry is not None and entry[1].live else None

    async def watch(self, session: Any, context: Any, page: Any, *, context_id: str | None) -> None:
        """Watch a session's windows: attach a birth watcher to its browser (once per browser) and map its
        browserContextId to the session. ``context_id`` is the gate's when it owns the context, else read here.

        Raises WindowBirthWatcherUnavailable (fail closed, as the gate's require does) when the watcher cannot
        attach or the context's id cannot be read: the session is not created on a browser whose windows are
        not judged. Nothing is attached while ALLOWED_HOSTS allows every host.
        """
        browser = getattr(session, "browser", None)
        if browser is None or not self._enabled():
            return
        if context_id is None:
            # The gate did not supply the context id (PRE_REQUEST_BLOCKING off): read it, and seed the first
            # page's targetId so a window it opens can be matched to its opener with no I/O.
            try:
                context_id, first_target_id = await self._read_context_id(context, page)
            except Exception as exc:
                raise WindowBirthWatcherUnavailable(
                    f"the window-birth watcher could not read the new context: {type(exc).__name__}: {exc}"
                ) from exc
            if not context_id or not first_target_id:
                raise WindowBirthWatcherUnavailable("the window-birth watcher could not read the new context's id")
            self._seed_target_id(page, first_target_id)
        watcher = await self._ensure(browser)
        self._registered[id(session)] = (session, context_id)
        watcher.register_session(context_id, session)

    async def _ensure(self, browser: Any) -> WindowBirthWatcher:
        async with self._lock:
            existing = self._watchers.get(id(browser))
            if existing is not None and existing[1].live:
                return existing[1]
            failed = self._attach_failures.get(id(browser))
            if failed is not None and time.monotonic() - failed[2] < ATTACH_RETRY_BACKOFF_SECONDS:
                raise WindowBirthWatcherUnavailable(
                    f"the window-birth watcher could not attach {ATTACH_RETRY_BACKOFF_SECONDS:g} s ago: {failed[1]}"
                )
            watcher = WindowBirthWatcher(
                browser,
                opener_refused=self._opener_refused,
                session_counts_as_refused=self._counts,
                page_by_target_id=self._page_by_target_id,
                on_lost=self._lost,
                on_recheck=self.close_unreported,
            )
            try:
                await watcher.attach()
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:300]
                self._attach_failures[id(browser)] = (browser, message, time.monotonic())
                raise WindowBirthWatcherUnavailable(f"the window-birth watcher could not attach: {message}") from exc
            self._attach_failures.pop(id(browser), None)
            self._watchers[id(browser)] = (browser, watcher)
            # A replacement after a loss: every live session on this browser is this watcher's again.
            for session, context_id in list(self._registered.values()):
                if getattr(session, "browser", None) is browser:
                    watcher.register_session(context_id, session)
            return watcher

    def _lost(self, watcher: WindowBirthWatcher) -> None:
        """The watcher's session died on its own: attach a replacement now, so the hold on the sessions'
        popups (holds_refused) is short; until it is up, or if the browser is gone, they stay refused."""
        browser = watcher._browser
        logger.warning("window-birth watcher: its browser session closed; the sessions' popups are refused meanwhile")

        async def reattach() -> None:
            try:
                await self._ensure(browser)
            except Exception as exc:
                logger.warning("window-birth watcher: could not re-attach after a loss: %s", f"{exc}"[:300])

        with contextlib.suppress(RuntimeError):
            spawn_background_task(reattach())

    @staticmethod
    async def _read_context_id(context: Any, page: Any) -> tuple[str | None, str | None]:
        """The first page's (browserContextId, targetId), read in one CDP round trip."""
        session = await context.new_cdp_session(page)
        try:
            info = (await asyncio.wait_for(session.send("Target.getTargetInfo"), timeout=CONTEXT_ID_TIMEOUT_SECONDS))[
                "targetInfo"
            ]
        finally:
            with contextlib.suppress(Exception):
                await session.detach()
        return info.get("browserContextId"), info.get("targetId")

    # ── what a session is, to the watcher ───────────────────────────────────────

    def watching(self, session: Any) -> bool:
        """Whether this session was registered with a watcher (its windows are judged at birth), live or lost."""
        return id(session) in self._registered

    def active_for(self, session: Any) -> bool:
        """Whether a live birth watcher judges this session's windows now."""
        return self.watching(session) and self._get(getattr(session, "browser", None)) is not None

    def holds_refused(self, session: Any) -> bool:
        """Whether every popup of this session is refused because its watcher is lost and not yet replaced: a
        window born meanwhile was never judged, and a replacement cannot judge it after the fact."""
        return self.watching(session) and self._get(getattr(session, "browser", None)) is None

    # ── reporting ───────────────────────────────────────────────────────────────

    def note_reported(self, session: Any, page: Any) -> None:
        """A page Playwright reported: resolve its targetId and mark its birth reported, in the background, so a
        window it opens is judged by it and the sweep never closes it. Its verdict is what ``verdict`` returns."""
        self._report(session, page)

    async def verdict(self, session: Any, page: Any) -> BirthVerdict:
        """The reported page's birth verdict, once its targetId is read (one read per page, shared)."""
        task = self._report(session, page)
        return await task if task is not None else BirthVerdict.CLEAN

    def _report(self, session: Any, page: Any) -> "asyncio.Task[BirthVerdict] | None":
        watcher = self._get(getattr(session, "browser", None))
        if watcher is None:
            return None
        try:
            task = self._reports.get(page)
        except TypeError:  # a page that cannot be weakly referenced (a test double)
            return spawn_background_task(self._resolve_and_mark(watcher, session, page))
        if task is None or task.done() and task.cancelled():
            task = spawn_background_task(self._resolve_and_mark(watcher, session, page))
            self._reports[page] = task
        return task

    async def _resolve_and_mark(self, watcher: WindowBirthWatcher, session: Any, page: Any) -> BirthVerdict:
        target_id = await self._resolve_target_id(page)
        return watcher.mark_reported(session, target_id, page)

    async def _reported_target_ids(self, session: Any) -> set[str]:
        """The targetIds of the session's pages whose id the controller already knows, so the sweep never
        closes one Playwright reported.

        A page Playwright reported is given a report task (``note_reported`` in ``attach_popup``) that resolves
        and caches its targetId; its first page and open-tab tabs are cached when opened. The reads in flight
        are waited for (bounded), then the cached id of every page is taken. A page whose id is not cached is
        never read here: it was never reported or opened by the controller (exactly an unreported window the
        sweep may close), and resolving an arbitrary window's id would open — and, since the session is kept,
        retain — a CDP session per window, which a refused page flooding windows would turn into resource
        exhaustion."""
        pages = list(self._session_pages(session))
        reads = [task for task in (self._reports.get(page) for page in pages) if task is not None and not task.done()]
        if reads:
            with contextlib.suppress(Exception):
                await asyncio.wait(reads, timeout=REPORTED_IDS_TIMEOUT_SECONDS)
        ids: set[str] = set()
        for page in pages:
            target_id = self._known_target_id(page)
            if target_id is not None:
                ids.add(target_id)
        return ids

    async def close_unreported(self, session: Any) -> None:
        watcher = self._get(getattr(session, "browser", None))
        if watcher is not None:
            await watcher.close_unreported(session, self._reported_target_ids)

    def forget_session(self, session: Any) -> None:
        self._registered.pop(id(session), None)
        watcher = self._get(getattr(session, "browser", None))
        if watcher is not None:
            watcher.forget_session(session)

    async def release(self, browser: Any) -> None:
        async with self._lock:
            self._attach_failures.pop(id(browser), None)
            entry = self._watchers.pop(id(browser), None)
        if entry is not None:
            await entry[1].detach()

    async def release_all(self) -> None:
        for browser, _watcher in list(self._watchers.values()):
            await self.release(browser)

    # ── readiness ───────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Each browser's watcher as /readiness reports it: attached, lost, or attach failed."""
        if not self._enabled():
            return {"state": "off", "off_reason": "allows_every_host", "watchers": []}
        watchers = [watcher.status() for _browser, watcher in self._watchers.values()]
        watchers += [
            {"state": "attach_failed", "error": message[:300]}
            for key, (_browser, message, _at) in self._attach_failures.items()
            if key not in self._watchers
        ]
        states = {watcher["state"] for watcher in watchers}
        state = next((name for name in ("attach_failed", "lost", "attached") if name in states), "idle")
        return {"state": state, "watchers": watchers}

"""The pre-request gates of every browser the controller holds (RequestGates), and what only they do.

Each browser gets one RequestGate (request_gate.py), attached at acquisition and replaced after a loss, the
replacement inheriting what the lost one owned. Besides the registry, this keeps a launched browser's prefetch
preference (prefetch.py), each owned page's service-worker bypass, the pages' targetIds, the blocks attributed to
a page by request sequence, and the state /readiness reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from typing import Any, Callable

from ..action_errors import SESSION_CLOSED_GATE_LOST, SessionNotFoundError
from ..utils import spawn_background_task
from .prefetch import PREFETCH_OFF, turn_prefetch_off
from .request_gate import GateBlock, RequestGate, RequestGateUnavailable

logger = logging.getLogger(__name__)

# A launched browser's prefetch preference while it is being set (readiness does not warn on it).
PREFETCH_PENDING = "pending"
# Opening a page's service-worker bypass (it runs under the session lock in tabs.open); a waiter gets the same bound.
BYPASS_TIMEOUT_SECONDS = 5.0
# Network.enable on the bypass session keeps no bodies: the bypass needs the domain on, not its buffers (measured on
# Chromium 151, round 4: 0 keeps the bypass working; Network.disable ends it).
BYPASS_NETWORK_BUFFER_BYTES = 0
# Chrome's error page: where a navigation the gate stopped leaves its page.
CHROME_ERROR_SCHEME = "chrome-error:"
# How long reading a page's targetId may take (it runs under the session lock, in error handling too).
TARGET_ID_TIMEOUT_SECONDS = 2.0


async def _open_bypass(page: Any) -> Any:
    session = await page.context.new_cdp_session(page)
    await session.send(
        "Network.enable",
        {"maxTotalBufferSize": BYPASS_NETWORK_BUFFER_BYTES, "maxResourceBufferSize": BYPASS_NETWORK_BUFFER_BYTES},
    )
    await session.send("Network.setBypassServiceWorker", {"bypass": True})
    return session


def _page_closed(page: Any) -> bool:
    is_closed = getattr(page, "is_closed", None)
    return callable(is_closed) and is_closed() is True


# Page-session opens a timed-out targetId read left running: kept until they resolve, then detached.
_OPENING: set[asyncio.Task[Any]] = set()


async def _read_target_id(page: Any) -> str:
    # A task, shielded: when the caller's bound fires while the session is still opening, the open goes on (the
    # browser creates the session anyway) and the session is detached once it exists, rather than left attached.
    opening = asyncio.ensure_future(page.context.new_cdp_session(page))
    try:
        session = await asyncio.shield(opening)
    except BaseException:
        if not opening.done():
            _OPENING.add(opening)
            opening.add_done_callback(_detach_once_opened)
        raise
    try:
        return (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
    finally:
        # Not awaited here: a detach that stalls must not hold the bound above, or the session lock.
        spawn_background_task(_detach_quietly(session))


def _detach_once_opened(opening: asyncio.Task[Any]) -> None:
    _OPENING.discard(opening)
    if opening.cancelled() or opening.exception() is not None:
        return  # nothing opened; the read already reported its own failure
    spawn_background_task(_detach_quietly(opening.result()))


async def _detach_quietly(session: Any) -> None:
    with contextlib.suppress(Exception):
        await session.detach()


class RequestGates:
    """The gate of every Browser the controller uses, attached where the browser is acquired.

    ``attach`` runs at acquisition and only logs a failure; ``require`` runs before a session opens its
    context there, attaches a gate when none is live (one never attached, or one lost), and raises when it
    cannot, so no session runs on a browser without one (decision 7). ``release`` detaches on purpose, before
    the controller closes or drops a browser. A gate lost on its own goes to ``on_lost`` (decision 2).
    """

    def __init__(
        self,
        *,
        url_refused: Callable[[str], bool],
        enabled: Callable[[], bool],
        origin_switch_declared: Callable[[], bool],
        default_attach_mode: Callable[[], bool],
        on_lost: Callable[[RequestGate], None],
        on_block: Callable[[GateBlock], None] | None = None,
        off_reason: Callable[[], str | None] | None = None,
    ) -> None:
        self._url_refused = url_refused
        # Why pre-request blocking is off (BrowserManager's rule), for readiness: one source for that rule.
        self._off_reason = off_reason
        self._on_block = on_block
        self._enabled = enabled
        self._origin_switch_declared = origin_switch_declared
        self._default_attach_mode = default_attach_mode
        self._on_lost = on_lost
        # By id: a browser is held until it is released, so its id is not reused meanwhile.
        self._gates: dict[int, tuple[Any, RequestGate]] = {}
        self._attach_modes: dict[int, tuple[Any, bool]] = {}
        # Why the last attach to a browser failed, until one succeeds (readiness).
        self._attach_failures: dict[int, tuple[Any, str]] = {}
        # Each page's targetId, read once (a block names the page by it).
        self._target_ids: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
        # The prefetch preference read back from each launched browser (decision 5), for readiness.
        self._prefetch: dict[int, tuple[Any, Any]] = {}
        # Each owned page's session holding Network.setBypassServiceWorker; the bypass lasts as long as it does.
        self._bypass_sessions: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
        # Pages whose bypass session is being opened: a second call meanwhile waits for that one (one per page).
        self._bypass_pending: weakref.WeakKeyDictionary[Any, asyncio.Future] = weakref.WeakKeyDictionary()
        self.bypass_failures = 0
        self._lock = asyncio.Lock()

    def enabled(self) -> bool:
        return self._enabled()

    def get(self, browser: Any) -> RequestGate | None:
        entry = self._gates.get(id(browser))
        return entry[1] if entry is not None else None

    async def attach(self, browser: Any, *, attach_mode: bool) -> None:
        """At acquisition: attach the browser's gate, or log why not (its sessions are then refused)."""
        self._attach_modes[id(browser)] = (browser, attach_mode)
        if not self._enabled():
            return
        if not attach_mode:
            # Read by readiness as "not yet": the step below can take PREF_TIMEOUT_SECONDS.
            self._prefetch[id(browser)] = (browser, PREFETCH_PENDING)
        try:
            await self.require(browser)
        except RequestGateUnavailable as exc:
            logger.warning("%s", exc.message)
        if not attach_mode:
            # The browser's preference, not the gate's: set even when this attach failed and a later one succeeds.
            await self._prefetch_off(browser)

    async def _prefetch_off(self, browser: Any) -> None:
        """A launched browser's prefetch, off at acquisition (decision 5); an attach mode's is never touched."""
        value = await turn_prefetch_off(browser)
        self._prefetch[id(browser)] = (browser, value)
        if value != PREFETCH_OFF:
            logger.warning(
                "pre-request gate: speculation-rules prefetch could not be turned off in a launched browser (read "
                "back %r): a prefetch of a host ALLOWED_HOSTS does not allow is sent, and only the page is refused.",
                value,
            )

    async def bypass_service_workers(self, page: Any) -> None:
        """Keep service workers from serving an owned page's navigations: it then makes a request the gate sees.

        Not a popup's first navigation: Playwright reports a popup only once that has started (step 0, m3).
        """
        pending = self._bypass_pending.get(page)
        if pending is not None:
            # The caller that must not navigate first (tabs.open) waits for the one in flight (the page event's).
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), timeout=BYPASS_TIMEOUT_SECONDS)
            return
        if page in self._bypass_sessions:
            return
        done = asyncio.get_running_loop().create_future()
        self._bypass_pending[page] = done
        try:
            session = await asyncio.wait_for(_open_bypass(page), timeout=BYPASS_TIMEOUT_SECONDS)
        except Exception as exc:
            if not _page_closed(page):
                # A page that closed first needed no bypass: not a failure readiness should warn about.
                self.bypass_failures += 1
                logger.warning(
                    "pre-request gate: could not bypass service workers on a page: %s", f"{type(exc).__name__}: {exc}"
                )
            return
        finally:
            self._bypass_pending.pop(page, None)
            if not done.done():
                done.set_result(None)
        self._bypass_sessions[page] = session
        page_ref = weakref.ref(page)  # the session's listener must not keep the page alive
        session.on("close", lambda _session: self._bypass_lost(page_ref, session))

    def _bypass_lost(self, page_ref: Any, session: Any) -> None:
        """A bypass session closed while its page is open: the bypass is gone with it."""
        page = page_ref()
        if page is None or self._bypass_sessions.get(page) is not session:
            return
        self._bypass_sessions.pop(page, None)
        if not _page_closed(page):
            self.bypass_failures += 1
            logger.warning("pre-request gate: a page's service-worker bypass session closed while the page is open")

    async def require(self, browser: Any) -> RequestGate | None:
        """The browser's live gate, attached now if need be; None while pre-request blocking is off."""
        if not self._enabled():
            return None
        async with self._lock:
            gate = self.get(browser)
            if gate is not None and gate.live:
                return gate
            known = self._attach_modes.get(id(browser))
            previous = gate
            gate = RequestGate(
                browser,
                url_refused=self._url_refused,
                attach_mode=known[1] if known is not None else self._default_attach_mode(),
                origin_switch_declared=self._origin_switch_declared(),
                on_lost=self._on_lost,
                on_block=self._on_block,
                inherit_from=previous,
                on_page_target=self._seed_target_id,
            )
            try:
                await gate.attach()
            except RequestGateUnavailable as exc:
                self._attach_failures[id(browser)] = (browser, exc.message)
                raise
            self._attach_failures.pop(id(browser), None)
            self._gates[id(browser)] = (browser, gate)
            return gate

    async def release(self, browser: Any) -> None:
        """Detach a browser's gate on purpose, before the controller closes or drops that browser. After an attach
        in flight (require), so the gate it attaches is not left registered."""
        async with self._lock:
            self._attach_modes.pop(id(browser), None)
            self._attach_failures.pop(id(browser), None)
            self._prefetch.pop(id(browser), None)
            entry = self._gates.pop(id(browser), None)
            if entry is not None:
                await entry[1].detach()

    def ending(self, browser: Any, ending: bool = True) -> None:
        """Before the controller closes a browser it owns: its gate stays attached (still gating) until the close
        has succeeded, and the close's end of the gate's session is not reported as a loss."""
        gate = self.get(browser)
        if gate is not None:
            gate.ending(ending)

    def assert_live(self, session: Any) -> None:
        """Refuse to start anything on a gated session whose browser has no live gate (lost, and not yet replaced):
        the error the next call gets once containment has closed it."""
        if getattr(session, "gate_context_id", None) is None:
            return
        gate = self.get(getattr(session, "browser", None))
        if gate is not None and gate.live:
            return
        raise SessionNotFoundError(session.id, status="closed", reason=SESSION_CLOSED_GATE_LOST)

    async def release_all(self) -> None:
        for browser, _gate in list(self._gates.values()):
            await self.release(browser)

    # ── what a session's pages ran into ──────────────────────────────────────

    def mark(self, browser: Any) -> int:
        """The browser's request sequence now: an operation's "before", read without touching the page."""
        gate = self.get(browser)
        return gate.sequence if gate is not None else 0

    async def blocks_on_page(self, browser: Any, page: Any, *, since: int) -> list[GateBlock]:
        """The blocks on ``page`` after the mark ``since``: its own, never another tab's, nor an earlier one."""
        gate = self.get(browser)
        if gate is None:
            return []
        return [block for block in gate.page_blocks(await self.target_id_of(page)) if block.seq > since]

    async def last_block_on_page(self, browser: Any, page: Any) -> GateBlock | None:
        """The block that is ``page``'s last main-frame request (its tab shows Chrome's error page for it); none
        once a later request of the page continued, whatever became of that one."""
        gate = self.get(browser)
        if gate is None:
            return None
        target_id = await self.target_id_of(page)
        blocks = gate.page_blocks(target_id)
        if not blocks or blocks[-1].seq != gate.last_request(target_id):
            return None
        return blocks[-1]

    async def error_page_block(self, browser: Any, page: Any) -> GateBlock | None:
        """The block that left ``page`` on Chrome's error page, while it is the page's last main-frame request;
        none on any other page, or once a later request of the page continued (last_block_on_page)."""
        if not str(getattr(page, "url", "") or "").startswith(CHROME_ERROR_SCHEME):
            return None
        return await self.last_block_on_page(browser, page)

    def _seed_target_id(self, page: Any, target_id: str) -> None:
        with contextlib.suppress(TypeError):  # a page that cannot be weakly referenced (a test double)
            self._target_ids[page] = target_id

    def known_target_id(self, page: Any) -> str | None:
        """A page's targetId if it was read already: no I/O (the audit's attribution reads it)."""
        return self._target_ids.get(page)

    async def target_id_of(self, page: Any) -> str | None:
        """A page's targetId (the frameId of its main frame's requests), read once through a page session."""
        known = self._target_ids.get(page)
        if known is not None:
            return known
        try:
            target_id = await asyncio.wait_for(_read_target_id(page), timeout=TARGET_ID_TIMEOUT_SECONDS)
        except Exception as exc:
            # Nothing is attributed to the page then, and the caller's own error stands.
            logger.debug("could not read a page's targetId: %s", f"{type(exc).__name__}: {exc}"[:200])
            return None
        self._target_ids[page] = target_id
        return target_id

    # ── readiness ────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Each browser's gate as /readiness reports it: attached, lost or attach failed, and its attach mode."""
        if not self._enabled():
            reason = (self._off_reason() if self._off_reason is not None else None) or "disabled"
            return {"state": "off", "off_reason": reason, "gates": []}
        gates: list[dict[str, Any]] = [
            {
                "state": "attached" if gate.live else "lost",
                "attach_mode": gate.attach_mode,
                "switch_declared": gate.origin_switch_declared,
                "handler_errors": gate.handler_errors,
                "lookup_error_blocks": gate.lookup_error_blocks,
                **({"prefetch_pref": self._prefetch[key][1]} if key in self._prefetch else {}),
                **(
                    {"error": self._attach_failures[key][1][:300]}
                    if not gate.live and key in self._attach_failures
                    else {}
                ),
            }
            for key, (_browser, gate) in self._gates.items()
        ]
        gates += [
            {
                "state": "attach_failed",
                "error": message[:300],
                **({"prefetch_pref": self._prefetch[key][1]} if key in self._prefetch else {}),
            }
            for key, (_browser, message) in self._attach_failures.items()
            if key not in self._gates
        ]
        states = {gate["state"] for gate in gates}
        state = next((name for name in ("attach_failed", "lost", "attached") if name in states), "idle")
        return {"state": state, "bypass_failures": self.bypass_failures, "gates": gates}

"""Pre-request blocking: stop a top-level navigation to a host off ALLOWED_HOSTS before Chromium sends it.

One browser-level CDP session per Browser (``Browser.new_browser_cdp_session``):
``Target.setDiscoverTargets`` (page targetIds and their browserContextId), then ``Fetch.enable`` for
Document requests at the Request stage. Chromium applies a browser-level Fetch handler to every target
after the frame's own (so it chains with Playwright's interception), and pauses each redirect hop again
before it is sent.

A paused request is failed (``BlockedByClient``) only when it is the main-frame request of a page in a
browser context the controller registered (``register_context``) and the controller's one matcher
(``BrowserManager._url_off_list``) refuses its URL. Everything else continues: allowed URLs, iframes
(allowed by design), and every context the controller did not create, the operator's own tabs under
CDP_CONNECT_URL among them.

Classification, by frameId (``Fetch.requestPaused`` carries no context id):
- a frameId the gate knows as a page target is a main frame, judged by that page's context;
- any other frameId is looked up (``Target.getTargetInfo``): ``iframe`` continues, an error naming no
  such target is an in-process iframe and continues, any other target type is judged like a page, and
  any other error cannot rule out an owned page: the request fails if its URL is refused.

The reply comes first: the handler takes no lock, does no I/O and awaits nothing but the one lookup and
its reply; records and logs follow the reply. When the gate's session detaches on its own Chromium
resumes what it held, so the gate reports itself lost (``on_lost``) and its owner contains the browser.

Attach modes only (a Chrome the controller did not launch): without
``--disable-features=BlockOriginHeaderModificationOnRedirect`` Chromium fails a cross-origin POST
redirect hop that any Fetch handler continued, with a bare net::ERR_INVALID_ARGUMENT. Unless the operator
declares the switch (CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED), the gate fails such a hop of an owned page
itself, with an explanation that names the fix. The Origin header is never rewritten: a missing Origin
where Chrome sends ``null`` is a CSRF downgrade.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ..action_errors import BROWSER_ACTION_BLOCKED, PRE_REQUEST_GATE_UNAVAILABLE, BrowserActionError
from ..network_inspector import url_origin
from ..utils import spawn_background_task

logger = logging.getLogger(__name__)

FETCH_PATTERNS = [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]
BLOCKED_ERROR_REASON = "BlockedByClient"
# Chromium's error text for a targetId that names no target: an in-process iframe's frameId.
NO_TARGET_TEXT = "No target with given id"
REDIRECT_ORIGIN_SWITCH = "--disable-features=BlockOriginHeaderModificationOnRedirect"
REDIRECT_ORIGIN_SETTING = "CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED"
# The URL of each recent request of an owned page, for the redirect hop that follows it.
REQUEST_URLS_KEPT = 1024
# Blocks kept per owned context, for what the agent is told about them.
BLOCKS_KEPT = 50
# How long a frame's lookup may take before it counts as failed (a refused URL then fails).
LOOKUP_TIMEOUT_SECONDS = 5.0

OFF_LIST = "off_list"
CROSS_ORIGIN_POST_REDIRECT = "cross_origin_post_redirect"
UNCLASSIFIED = "unclassified"


GATE_REQUIRED = (
    "Sessions are refused on this browser while PRE_REQUEST_BLOCKING is on; PRE_REQUEST_BLOCKING=false runs "
    "on the guard that refuses a page after it loads alone."
)


class RequestGateUnavailable(BrowserActionError):
    """The gate is not attached to a browser, or could not register a context: sessions there are refused.

    Retryable: the next session attaches a new gate (RequestGates.require).
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            f"{message.rstrip('.')}. {GATE_REQUIRED}",
            code=PRE_REQUEST_GATE_UNAVAILABLE,
            status_code=503,
            retryable=True,
        )


@dataclass(frozen=True)
class GateBlock:
    context_id: str | None
    target_id: str | None
    # The refused URL's origin only: its path and query are the page's choice.
    origin: str
    reason: str
    explanation: str
    at: float = field(default_factory=time.monotonic)


def navigation_blocked_error(block: GateBlock, *, action: str) -> BrowserActionError:
    """What the agent is told about a navigation the gate stopped outside the action pipeline (a start URL, a new
    tab's URL): a block, not a retryable failure, naming only the origin."""
    return BrowserActionError(
        block.explanation,
        code=BROWSER_ACTION_BLOCKED,
        action=action,
        status_code=403,
        retryable=False,
        url=block.origin,
    )


def block_summary(block: GateBlock) -> dict[str, str]:
    return {"origin": block.origin, "reason": block.reason, "explanation": block.explanation}


@dataclass
class _OwnedContext:
    blocked: int = 0
    blocks: deque[GateBlock] = field(default_factory=lambda: deque(maxlen=BLOCKS_KEPT))


@dataclass(frozen=True)
class _Verdict:
    fail: bool
    reason: str | None = None
    context_id: str | None = None
    owned_page: bool = False
    lookup_error: str | None = None


_CONTINUE = _Verdict(fail=False)


def _parsed_origin(url: str) -> str | None:
    """A URL's origin, or None when urlsplit refuses it (a malformed IPv6 host)."""
    try:
        return url_origin(url)
    except ValueError:
        return None


def _origin_of(url: str) -> str:
    return _parsed_origin(url) or "(unparseable URL)"


def _has_origin_header(request: dict[str, Any]) -> bool:
    return any(name.lower() == "origin" for name in (request.get("headers") or {}))


class RequestGate:
    """The gate for one Browser. Not reattached: after a loss its owner attaches a new one."""

    def __init__(
        self,
        browser: Any,
        *,
        url_refused: Callable[[str], bool],
        attach_mode: bool,
        origin_switch_declared: bool,
        on_lost: Callable[["RequestGate"], None],
        on_block: Callable[[GateBlock], None] | None = None,
        inherit_from: "RequestGate | None" = None,
    ) -> None:
        self.browser = browser
        self._on_block = on_block
        self._url_refused = url_refused
        self._attach_mode = attach_mode
        self._origin_switch_declared = origin_switch_declared
        self._on_lost = on_lost
        self._cdp: Any = None
        self._live = False
        # Set by the session's close, whenever it comes: during attach() too.
        self._closed = False
        self._deliberate = False
        self._targets: dict[str, tuple[str | None, str | None]] = {}
        # A gate attached after a loss shares the lost one's contexts: what outlived containment stays gated,
        # and the close listener register_context wired forgets it here too.
        self._owned: dict[str, _OwnedContext] = inherit_from._owned if inherit_from is not None else {}
        self._request_urls: OrderedDict[str, str] = OrderedDict()
        self.unattributed_blocks = 0
        self.handler_errors = 0

    @property
    def live(self) -> bool:
        return self._live and not self._closed

    @property
    def attach_mode(self) -> bool:
        return self._attach_mode

    @property
    def origin_switch_declared(self) -> bool:
        return self._origin_switch_declared

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def attach(self) -> None:
        try:
            cdp = await self.browser.new_browser_cdp_session()
        except Exception as exc:
            raise RequestGateUnavailable(f"the pre-request gate could not attach: {exc}") from exc
        self._cdp = cdp
        cdp.on("Target.targetCreated", self._on_target)
        cdp.on("Target.targetInfoChanged", self._on_target)
        cdp.on("Target.targetDestroyed", self._on_target_destroyed)
        cdp.on("Fetch.requestPaused", self._on_paused)
        cdp.on("close", self._on_close)
        try:
            await cdp.send("Target.setDiscoverTargets", {"discover": True})
            await cdp.send("Fetch.enable", {"patterns": FETCH_PATTERNS})
        except Exception as exc:
            self._deliberate = True
            with contextlib.suppress(Exception):
                await cdp.detach()
            raise RequestGateUnavailable(f"the pre-request gate could not attach: {exc}") from exc
        if self._closed:
            raise RequestGateUnavailable("the pre-request gate's session closed while it attached")
        self._live = True
        if self._attach_mode and not self._origin_switch_declared:
            logger.warning(
                "pre-request gate attached to a Chrome the controller did not launch, without %s=true: it stops "
                "cross-origin 307/308 POST redirects of the controller's sessions itself, and the same redirects "
                "in the operator's own tabs fail in Chrome (net::ERR_INVALID_ARGUMENT) while the gate is attached. "
                "Start that Chrome with %s and set %s=true.",
                REDIRECT_ORIGIN_SETTING,
                REDIRECT_ORIGIN_SWITCH,
                REDIRECT_ORIGIN_SETTING,
            )

    async def detach(self) -> None:
        """Detach on purpose (the controller is closing or replacing the browser): not a loss."""
        self._deliberate = True
        self._live = False
        if self._cdp is not None:
            with contextlib.suppress(Exception):
                await self._cdp.detach()

    def _on_close(self, _session: Any = None) -> None:
        was_live = self.live
        self._closed = True
        if was_live and not self._deliberate:
            self._on_lost(self)

    # ── ownership ────────────────────────────────────────────────────────────

    async def register_context(self, context: Any, page: Any) -> str:
        """Own a context the controller created, from its first page, before that page navigates."""
        if not self.live:
            raise RequestGateUnavailable("the pre-request gate is not attached to this browser")
        try:
            session = await context.new_cdp_session(page)
            try:
                info = (await session.send("Target.getTargetInfo"))["targetInfo"]
            finally:
                with contextlib.suppress(Exception):
                    await session.detach()
            context_id = info["browserContextId"]
        except Exception as exc:
            raise RequestGateUnavailable(f"the pre-request gate could not read the new context: {exc}") from exc
        if not self.live:
            raise RequestGateUnavailable("the pre-request gate detached while the session was starting")
        self._owned[context_id] = _OwnedContext()
        if hasattr(context, "on"):
            # Owned until it has really closed: a page still navigating while it closes, or after a close
            # that failed, stays gated. browserContextIds are not reused, so a context that never closes is harmless.
            context.on("close", lambda _context: self.forget_context(context_id))
        return context_id

    def forget_context(self, context_id: str) -> None:
        self._owned.pop(context_id, None)

    def owns(self, context_id: str | None) -> bool:
        return context_id in self._owned

    def blocks(self, context_id: str) -> list[GateBlock]:
        owned = self._owned.get(context_id)
        return list(owned.blocks) if owned is not None else []

    def blocked_count(self, context_id: str) -> int:
        owned = self._owned.get(context_id)
        return owned.blocked if owned is not None else 0

    # ── events ───────────────────────────────────────────────────────────────

    def _on_target(self, event: dict[str, Any]) -> None:
        info = event.get("targetInfo") or {}
        target_id = info.get("targetId")
        if target_id:
            self._targets[target_id] = (info.get("type"), info.get("browserContextId"))

    def _on_target_destroyed(self, event: dict[str, Any]) -> None:
        self._targets.pop(event.get("targetId"), None)

    def _on_paused(self, event: dict[str, Any]) -> None:
        # Delivered inline by Playwright's event emitter; the decision and its reply run in a task.
        spawn_background_task(self._handle(event))

    async def _handle(self, event: dict[str, Any]) -> None:
        request = event.get("request") or {}
        url = request.get("url") or ""
        handler_error: BaseException | None = None
        try:
            verdict = await self._verdict(event, request, url)
        except Exception as exc:
            # Nothing could be ruled out: a refused URL fails, as for a page that could be owned.
            handler_error = exc
            verdict = _Verdict(fail=not self._known_allowed(url), reason=UNCLASSIFIED)
        if verdict.owned_page:
            # Decision state, not bookkeeping: the next hop's verdict reads it, so it is written before the reply.
            self._remember_url(event.get("requestId"), url)
        reply_error: BaseException | None = None
        try:
            if verdict.fail:
                await self._cdp.send(
                    "Fetch.failRequest", {"requestId": event.get("requestId"), "errorReason": BLOCKED_ERROR_REASON}
                )
            else:
                await self._cdp.send("Fetch.continueRequest", {"requestId": event.get("requestId")})
        except Exception as exc:
            reply_error = exc
        # Bookkeeping, only after the reply.
        self._after_reply(event, url, verdict, handler_error, reply_error)

    async def _verdict(self, event: dict[str, Any], request: dict[str, Any], url: str) -> _Verdict:
        kind, context_id, lookup_error = await self._classify(event.get("frameId"))
        if kind in ("iframe", "no_target"):
            return _CONTINUE
        if kind == "lookup_error":
            fail = not self._known_allowed(url)
            return _Verdict(fail=fail, reason=UNCLASSIFIED if fail else None, lookup_error=lookup_error)
        if context_id not in self._owned:
            return _CONTINUE
        if self._url_refused(url):
            return _Verdict(fail=True, reason=OFF_LIST, context_id=context_id, owned_page=True)
        if self._cross_origin_post_hop(event, request, url):
            return _Verdict(fail=True, reason=CROSS_ORIGIN_POST_REDIRECT, context_id=context_id, owned_page=True)
        return _Verdict(fail=False, context_id=context_id, owned_page=True)

    async def _classify(self, frame_id: str | None) -> tuple[str, str | None, str | None]:
        known = self._targets.get(frame_id) if frame_id else None
        if known is not None and known[0] == "page":
            return "page", known[1], None
        if not frame_id:
            return "lookup_error", None, "the paused request names no frame"
        try:
            reply = await asyncio.wait_for(
                self._cdp.send("Target.getTargetInfo", {"targetId": frame_id}), timeout=LOOKUP_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return "lookup_error", None, f"Target.getTargetInfo did not answer within {LOOKUP_TIMEOUT_SECONDS:g} s"
        except Exception as exc:
            text = str(exc)
            return ("no_target" if NO_TARGET_TEXT in text else "lookup_error"), None, text[:300]
        info = reply["targetInfo"]
        return info.get("type") or "unknown", info.get("browserContextId"), None

    def _known_allowed(self, url: str) -> bool:
        try:
            return not self._url_refused(url)
        except Exception:
            return False

    def _cross_origin_post_hop(self, event: dict[str, Any], request: dict[str, Any], url: str) -> bool:
        if not self._attach_mode or self._origin_switch_declared:
            return False
        previous_id = event.get("redirectedRequestId")
        if not previous_id or (request.get("method") or "").upper() != "POST" or not _has_origin_header(request):
            return False
        previous = self._request_urls.get(previous_id)
        if previous is None:
            return True
        # An origin that cannot be read is not known to be the same: the hop fails with the gate's own error.
        before, after = _parsed_origin(previous), _parsed_origin(url)
        return before is None or after is None or before != after

    def _remember_url(self, request_id: str | None, url: str) -> None:
        if not request_id:
            return
        self._request_urls[request_id] = url
        self._request_urls.move_to_end(request_id)
        while len(self._request_urls) > REQUEST_URLS_KEPT:
            self._request_urls.popitem(last=False)

    def _after_reply(
        self,
        event: dict[str, Any],
        url: str,
        verdict: _Verdict,
        handler_error: BaseException | None,
        reply_error: BaseException | None,
    ) -> None:
        if handler_error is not None:
            self.handler_errors += 1
            logger.error("pre-request gate: the handler failed for a paused request", exc_info=handler_error)
        if verdict.lookup_error is not None:
            logger.warning("pre-request gate: could not classify a paused request's frame: %s", verdict.lookup_error)
        if reply_error is not None:
            logger.warning("pre-request gate: Chromium did not take the reply to a paused request: %s", reply_error)
        if verdict.fail:
            self._record_block(event, url, verdict)

    def _record_block(self, event: dict[str, Any], url: str, verdict: _Verdict) -> None:
        origin = _origin_of(url)
        if verdict.reason == CROSS_ORIGIN_POST_REDIRECT:
            explanation = (
                f"A cross-origin POST redirect to {origin} was stopped: the attached Chrome must be started with "
                f"{REDIRECT_ORIGIN_SWITCH} for the controller to let it through, and the operator declares that "
                f"with {REDIRECT_ORIGIN_SETTING}=true. Without the switch Chrome fails such a redirect itself."
            )
        else:
            explanation = (
                f"A navigation to {origin}, a host ALLOWED_HOSTS does not allow, was stopped before it was sent."
            )
        owned = self._owned.get(verdict.context_id) if verdict.context_id else None
        block = GateBlock(
            context_id=verdict.context_id if owned is not None else None,
            target_id=event.get("frameId"),
            origin=origin,
            reason=verdict.reason or UNCLASSIFIED,
            explanation=explanation,
        )
        if owned is None:
            self.unattributed_blocks += 1
            return
        owned.blocked += 1
        owned.blocks.append(block)
        if self._on_block is not None:
            try:
                self._on_block(block)
            except Exception:
                logger.exception("pre-request gate: the block listener failed")


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
    ) -> None:
        self._url_refused = url_refused
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
        self._lock = asyncio.Lock()

    def get(self, browser: Any) -> RequestGate | None:
        entry = self._gates.get(id(browser))
        return entry[1] if entry is not None else None

    async def attach(self, browser: Any, *, attach_mode: bool) -> None:
        """At acquisition: attach the browser's gate, or log why not (its sessions are then refused)."""
        self._attach_modes[id(browser)] = (browser, attach_mode)
        if not self._enabled():
            return
        try:
            await self.require(browser)
        except RequestGateUnavailable as exc:
            logger.warning("%s", exc.message)

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
        """Detach a browser's gate on purpose, before the controller closes or drops that browser."""
        self._attach_modes.pop(id(browser), None)
        self._attach_failures.pop(id(browser), None)
        entry = self._gates.pop(id(browser), None)
        if entry is not None:
            await entry[1].detach()

    async def release_all(self) -> None:
        for browser, _gate in list(self._gates.values()):
            await self.release(browser)

    # ── what a session's pages ran into ──────────────────────────────────────

    def blocked_count(self, browser: Any, context_id: str | None) -> int:
        """A context's block count now: what an action compares against after its operation."""
        gate = self.get(browser)
        return gate.blocked_count(context_id) if gate is not None and context_id else 0

    async def blocks_on_page(self, browser: Any, context_id: str | None, page: Any, *, since: int) -> list[GateBlock]:
        """The blocks recorded on ``page`` after the context's count was ``since``: its own, never another tab's."""
        gate = self.get(browser)
        if gate is None or not context_id:
            return []
        new = gate.blocked_count(context_id) - since
        if new <= 0:
            return []
        recent = gate.blocks(context_id)[-new:]
        target_id = await self.target_id_of(page)
        return [block for block in recent if block.target_id == target_id]

    async def last_block_on_page(self, browser: Any, context_id: str | None, page: Any) -> GateBlock | None:
        """The most recent block on ``page`` (a tab whose navigation was stopped shows Chrome's error page)."""
        gate = self.get(browser)
        if gate is None or not context_id:
            return None
        blocks = gate.blocks(context_id)
        if not blocks:
            return None
        target_id = await self.target_id_of(page)
        return next((block for block in reversed(blocks) if block.target_id == target_id), None)

    async def target_id_of(self, page: Any) -> str | None:
        """A page's targetId (the frameId of its main frame's requests), read once through a page session."""
        known = self._target_ids.get(page)
        if known is not None:
            return known
        try:
            session = await page.context.new_cdp_session(page)
            try:
                target_id = (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
            finally:
                with contextlib.suppress(Exception):
                    await session.detach()
        except Exception as exc:
            logger.debug("could not read a page's targetId: %s", exc)
            return None
        self._target_ids[page] = target_id
        return target_id

    # ── readiness ────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Each browser's gate as /readiness reports it: attached, lost or attach failed, and its attach mode."""
        if not self._enabled():
            return {"state": "off", "gates": []}
        gates: list[dict[str, Any]] = [
            {
                "state": "attached" if gate.live else "lost",
                "attach_mode": gate.attach_mode,
                "switch_declared": gate.origin_switch_declared,
            }
            for _browser, gate in self._gates.values()
        ]
        gates += [
            {"state": "attach_failed", "error": message[:300]}
            for key, (_browser, message) in self._attach_failures.items()
            if key not in self._gates
        ]
        states = {gate["state"] for gate in gates}
        state = next((name for name in ("attach_failed", "lost", "attached") if name in states), "idle")
        return {"state": state, "gates": gates}

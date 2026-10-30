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
import re
import time
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ..action_errors import (
    BROWSER_ACTION_BLOCKED,
    PRE_REQUEST_GATE_UNAVAILABLE,
    SESSION_CLOSED_GATE_LOST,
    BrowserActionError,
    SessionNotFoundError,
)
from ..network_inspector import url_origin
from ..utils import spawn_background_task
from .prefetch import PREFETCH_OFF, turn_prefetch_off

logger = logging.getLogger(__name__)

FETCH_PATTERNS = [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]
BLOCKED_ERROR_REASON = "BlockedByClient"
# Chromium's error text for a targetId that names no target: an in-process iframe's frameId.
NO_TARGET_TEXT = "No target with given id"
REDIRECT_ORIGIN_SWITCH = "--disable-features=BlockOriginHeaderModificationOnRedirect"
REDIRECT_ORIGIN_SETTING = "CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED"
# The URL of each recent request of an owned page, for the redirect hop that follows it.
REQUEST_URLS_KEPT = 1024
# Blocks kept per owned context, and per page, for what the agent is told about them.
BLOCKS_KEPT = 50
# Pages whose blocks are kept (each dropped when its target is destroyed); the oldest goes past this.
PAGES_KEPT = 512
# The gate's whole attach (its session, target discovery, Fetch.enable), and a deliberate detach.
ATTACH_TIMEOUT_SECONDS = 10.0
DETACH_TIMEOUT_SECONDS = 2.0
# Opening a page's service-worker bypass (it runs under the session lock in tabs.open); a waiter gets the same bound.
BYPASS_TIMEOUT_SECONDS = 5.0
# Network.enable on the bypass session keeps no bodies: the bypass needs the domain on, not its buffers (measured on
# Chromium 151, round 4: 0 keeps the bypass working; Network.disable ends it).
BYPASS_NETWORK_BUFFER_BYTES = 0
# How Playwright reports a request the gate failed (Fetch.failRequest BlockedByClient): the first line of its
# error, as "Page.goto: net::ERR_BLOCKED_BY_CLIENT at <url>" (or without the "Page.goto: " prefix).
BLOCKED_BY_CLIENT_TEXT = "net::ERR_BLOCKED_BY_CLIENT"
_STOPPED_FIRST_LINE = re.compile(r"^(?:[\w.]+: )?net::ERR_BLOCKED_BY_CLIENT(?: at |$)")
# What the agent is told when the gate's failure is certain but no block could be tied to the page.
UNATTRIBUTED_STOP = "A navigation was stopped before it was sent by the pre-request gate."
# How long a frame's lookup may take before it counts as failed (a refused URL then fails).
LOOKUP_TIMEOUT_SECONDS = 5.0
# How long reading a page's targetId may take (it runs under the session lock, in error handling too).
TARGET_ID_TIMEOUT_SECONDS = 2.0

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
    # The gate-wide sequence of the request it stopped (shared by replacement gates): what "since" compares.
    seq: int = 0
    at: float = field(default_factory=time.monotonic)


def navigation_blocked_error(block: GateBlock | None, *, action: str) -> BrowserActionError:
    """What the agent is told about a navigation the gate stopped outside the action pipeline (a start URL, a new
    tab's URL): a block, not a retryable failure, naming only the origin, or no URL when no block was attributed."""
    return BrowserActionError(
        block.explanation if block is not None else UNATTRIBUTED_STOP,
        code=BROWSER_ACTION_BLOCKED,
        action=action,
        status_code=403,
        retryable=False,
        url=block.origin if block is not None else None,
    )


def block_summary(block: GateBlock) -> dict[str, str]:
    return {"origin": block.origin, "reason": block.reason, "explanation": block.explanation}


def stopped_by_gate(exc: BaseException) -> bool:
    """Whether a Playwright failure is a request the gate failed, by the first line of its error only: a selector,
    or a call log, that contains the token is not one, and keeps its own error."""
    text = getattr(exc, "message", None) or str(exc)
    return bool(_STOPPED_FIRST_LINE.match(str(text).splitlines()[0] if text else ""))


@dataclass
class _OwnedContext:
    blocked: int = 0
    blocks: deque[GateBlock] = field(default_factory=lambda: deque(maxlen=BLOCKS_KEPT))


@dataclass
class _PageBlocks(_OwnedContext):
    """A page's blocks, keyed by its targetId whether or not its context was known, and the sequence of its last
    main-frame request the gate judged (continued or failed)."""

    last_request: int = 0


@dataclass
class _Sequence:
    """The gate-wide request sequence, one object shared by a gate and its replacements."""

    value: int = 0

    def next(self) -> int:
        self.value += 1
        return self.value


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
        on_page_target: Callable[[Any, str], None] | None = None,
    ) -> None:
        self.browser = browser
        self._on_block = on_block
        # Told each registered context's first page and its targetId (RequestGates seeds its cache from it).
        self._on_page_target = on_page_target
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
        # Blocks by page targetId, shared the same way: a caller's count from before a loss still compares.
        self._pages: OrderedDict[str, _PageBlocks] = inherit_from._pages if inherit_from is not None else OrderedDict()
        self._sequence: _Sequence = inherit_from._sequence if inherit_from is not None else _Sequence()
        # Shared too: a hop paused at the replacement right after a loss finds the URL it redirects from.
        self._request_urls: OrderedDict[str, str] = (
            inherit_from._request_urls if inherit_from is not None else OrderedDict()
        )
        self.unattributed_blocks = 0
        self.handler_errors = 0
        # Blocks of a frame the gate could not look up (failed because their URL is refused).
        self.lookup_error_blocks = 0

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
            await asyncio.wait_for(self._attach(), timeout=ATTACH_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise RequestGateUnavailable(
                f"the pre-request gate did not attach within {ATTACH_TIMEOUT_SECONDS:g} s"
            ) from exc
        self._attached()

    async def _attach(self) -> None:
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
        except BaseException as exc:
            # Cancellation (the attach bound among others) too: a session left attached would hold requests.
            self._deliberate = True
            await self._detach_cdp()
            if isinstance(exc, Exception):
                raise RequestGateUnavailable(f"the pre-request gate could not attach: {exc}") from exc
            raise

    def _attached(self) -> None:
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
        await self._detach_cdp()

    async def _detach_cdp(self) -> None:
        if self._cdp is None:
            return
        try:
            await asyncio.wait_for(self._cdp.detach(), timeout=DETACH_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("pre-request gate: could not detach its session: %s", f"{type(exc).__name__}: {exc}"[:200])

    def ending(self, ending: bool = True) -> None:
        """The controller is closing this gate's browser: the close takes the gate's session down with it, which is
        not a loss. ``ending(False)`` undoes it when that close failed, so a later loss is reported again."""
        self._deliberate = ending

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
        if self._on_page_target is not None and info.get("targetId"):
            self._on_page_target(page, info["targetId"])
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

    def page_blocks(self, target_id: str | None) -> list[GateBlock]:
        page = self._pages.get(target_id) if target_id else None
        return list(page.blocks) if page is not None else []

    def page_blocked_count(self, target_id: str | None) -> int:
        page = self._pages.get(target_id) if target_id else None
        return page.blocked if page is not None else 0

    @property
    def sequence(self) -> int:
        """The last request sequence handed out: an operation's "before" mark, with no page read."""
        return self._sequence.value

    def last_request(self, target_id: str | None) -> int:
        page = self._pages.get(target_id) if target_id else None
        return page.last_request if page is not None else 0

    def _page(self, target_id: str) -> _PageBlocks:
        page = self._pages.get(target_id)
        if page is None:
            page = self._pages[target_id] = _PageBlocks()
        self._pages.move_to_end(target_id)
        while len(self._pages) > PAGES_KEPT:
            self._pages.popitem(last=False)
        return page

    # ── events ───────────────────────────────────────────────────────────────

    def _on_target(self, event: dict[str, Any]) -> None:
        info = event.get("targetInfo") or {}
        target_id = info.get("targetId")
        if target_id:
            self._targets[target_id] = (info.get("type"), info.get("browserContextId"))

    def _on_target_destroyed(self, event: dict[str, Any]) -> None:
        self._targets.pop(event.get("targetId"), None)
        self._pages.pop(event.get("targetId"), None)

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
        if verdict.owned_page or verdict.lookup_error is not None:
            # Decision state, not bookkeeping: the next hop's verdict reads it, so it is written before the reply.
            # A frame that could not be looked up may be an owned page's.
            self._remember_url(event.get("requestId"), url)
        # On record before the reply (memory only): whoever reads once Playwright reports the failure finds it.
        seq = self._note_request(event, verdict)
        block = self._record_block(event, url, verdict, seq) if verdict.fail else None
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
        # The audit and the logs, only after the reply.
        self._after_reply(verdict, block, handler_error, reply_error)

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
        if known is not None and known[0] == "page" and known[1] is not None:
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
        verdict: _Verdict,
        block: GateBlock | None,
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
        if block is not None and self._on_block is not None:
            try:
                self._on_block(block)
            except Exception:
                logger.exception("pre-request gate: the block listener failed")

    def _note_request(self, event: dict[str, Any], verdict: _Verdict) -> int:
        """Number a main-frame request the gate judged (an owned page's, or one it could not classify), and keep it
        as its page's last: the tab list labels a block only while it is the page's last such request."""
        seq = self._sequence.next()
        target_id = event.get("frameId")
        if target_id and (verdict.owned_page or verdict.fail or verdict.lookup_error is not None):
            self._page(target_id).last_request = seq
        return seq

    def _record_block(self, event: dict[str, Any], url: str, verdict: _Verdict, seq: int = 0) -> GateBlock:
        """Keep the block by its page, and by its context when that is known. Memory only: it runs before the reply."""
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
            seq=seq,
        )
        if block.target_id:
            page = self._page(block.target_id)
            page.blocked += 1
            page.blocks.append(block)
        if verdict.lookup_error is not None:
            self.lookup_error_blocks += 1
        if owned is None:
            self.unattributed_blocks += 1
        else:
            owned.blocked += 1
            owned.blocks.append(block)
        return block


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


async def _read_target_id(page: Any) -> str:
    session = await page.context.new_cdp_session(page)
    try:
        return (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
    finally:
        # Not awaited here: a detach that stalls must not hold the bound above, or the session lock.
        spawn_background_task(_detach_quietly(session))


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

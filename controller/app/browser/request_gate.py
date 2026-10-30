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
# Blocks kept per owned context, and per page, for what the agent is told about them.
BLOCKS_KEPT = 50
# Pages whose blocks are kept (each dropped when its target is destroyed); the oldest goes past this.
PAGES_KEPT = 512
# The gate's whole attach (its session, target discovery, Fetch.enable), and a deliberate detach.
ATTACH_TIMEOUT_SECONDS = 10.0
DETACH_TIMEOUT_SECONDS = 2.0
# How Playwright reports a request the gate failed (Fetch.failRequest BlockedByClient): the first line of its
# error, as "Page.goto: net::ERR_BLOCKED_BY_CLIENT at <url>" (or without the "Page.goto: " prefix).
BLOCKED_BY_CLIENT_TEXT = "net::ERR_BLOCKED_BY_CLIENT"
_STOPPED_FIRST_LINE = re.compile(r"^(?:[\w.]+: )?net::ERR_BLOCKED_BY_CLIENT(?: at |$)")
# What the agent is told when the gate's failure is certain but no block could be tied to the page.
UNATTRIBUTED_STOP = "A navigation was stopped before it was sent by the pre-request gate."
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
        block: GateBlock | None = None
        try:
            if verdict.owned_page or verdict.lookup_error is not None:
                # Decision state, not bookkeeping: the next hop's verdict reads it, so it is written before the
                # reply. A frame that could not be looked up may be an owned page's.
                self._remember_url(event.get("requestId"), url)
            # On record before the reply (memory only): whoever reads once Playwright reports the failure finds it.
            seq = self._note_request(event, verdict)
            block = self._record_block(event, url, verdict, seq) if verdict.fail else None
        except Exception as exc:
            # The verdict stands and its reply still goes out; the failure is logged after it.
            handler_error = handler_error or exc
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

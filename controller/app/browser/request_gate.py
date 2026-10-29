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

import contextlib
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

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

OFF_LIST = "off_list"
CROSS_ORIGIN_POST_REDIRECT = "cross_origin_post_redirect"
UNCLASSIFIED = "unclassified"


class RequestGateUnavailable(RuntimeError):
    """The gate is not attached to a browser, or could not register a context: sessions there are refused."""


@dataclass(frozen=True)
class GateBlock:
    context_id: str | None
    target_id: str | None
    # The refused URL's origin only: its path and query are the page's choice.
    origin: str
    reason: str
    explanation: str
    at: float = field(default_factory=time.monotonic)


@dataclass
class _OwnedContext:
    key: str
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


def _origin_of(url: str) -> str:
    try:
        return url_origin(url)
    except ValueError:  # urlsplit refuses some URLs Chromium sends (a malformed IPv6 host)
        return "(unparseable URL)"


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
    ) -> None:
        self.browser = browser
        self._url_refused = url_refused
        self._attach_mode = attach_mode
        self._origin_switch_declared = origin_switch_declared
        self._on_lost = on_lost
        self._cdp: Any = None
        self._live = False
        self._deliberate = False
        self._targets: dict[str, tuple[str | None, str | None]] = {}
        self._owned: dict[str, _OwnedContext] = {}
        self._request_urls: OrderedDict[str, str] = OrderedDict()
        self.unattributed_blocks = 0
        self.handler_errors = 0

    @property
    def live(self) -> bool:
        return self._live

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def attach(self) -> None:
        cdp = await self.browser.new_browser_cdp_session()
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
        self._live = True

    async def detach(self) -> None:
        """Detach on purpose (the controller is closing or replacing the browser): not a loss."""
        self._deliberate = True
        self._live = False
        if self._cdp is not None:
            with contextlib.suppress(Exception):
                await self._cdp.detach()

    def _on_close(self, _session: Any = None) -> None:
        was_live, self._live = self._live, False
        if was_live and not self._deliberate:
            self._on_lost(self)

    # ── ownership ────────────────────────────────────────────────────────────

    async def register_context(self, context: Any, page: Any, *, key: str) -> str:
        """Own a context the controller created, from its first page, before that page navigates."""
        if not self._live:
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
        if not self._live:
            raise RequestGateUnavailable("the pre-request gate detached while the session was starting")
        self._owned[context_id] = _OwnedContext(key=key)
        if hasattr(context, "on"):
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
            reply = await self._cdp.send("Target.getTargetInfo", {"targetId": frame_id})
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
        return previous is None or url_origin(previous) != url_origin(url)

    def _after_reply(
        self,
        event: dict[str, Any],
        url: str,
        verdict: _Verdict,
        handler_error: BaseException | None,
        reply_error: BaseException | None,
    ) -> None:
        request_id = event.get("requestId")
        if verdict.owned_page and request_id:
            self._request_urls[request_id] = url
            self._request_urls.move_to_end(request_id)
            while len(self._request_urls) > REQUEST_URLS_KEPT:
                self._request_urls.popitem(last=False)
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

"""The isolated JavaScript world that holds a page's element ids, and the CDP session that reaches it.

Each page gets a world of the controller's own (``Page.createIsolatedWorld``):
it shares the page's DOM but none of its JavaScript, so page scripts can
neither read the registry nor replace anything the world calls
(``elementFromPoint``, ``getBoundingClientRect``, ``focus``, ``Map``,
``crypto``). Chromium answers with the same world for the same name until the
frame's document changes, and with a new, empty one after.

Every call is two CDP messages on a session the controller opens for the page.
Each message has its own deadline (``ACTION_TIMEOUT_MS``, plus the script's own
wait for a script that waits): a bound per message, not a deadline for a whole
action. A renderer that crashed leaves the session open with nothing answering,
and a call with no deadline hung the request; now it fails with
``page_not_responding``. A session that timed out is detached and dropped, one
that went away is dropped, and the next call opens a new one.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError

from ..action_errors import PAGE_NOT_RESPONDING, BrowserActionError
from ..utils import spawn_background_task

if TYPE_CHECKING:
    from playwright.async_api import CDPSession, Page

logger = logging.getLogger(__name__)

WORLD_NAME = "auto-browser-element-ids"

# What a world call does to the page. It decides whether the call is sent once
# more after its session or frame went away, when its answer may have been lost
# after it ran. Every caller names one.
# READ only reads, and IDEMPOTENT changes the page the same way however often it
# runs (scrolling into view, removing the upload marker): both are sent again.
# CHANGES_PAGE (focus, a selection, placing the upload marker) never is.
READ = "read"
IDEMPOTENT = "idempotent"
CHANGES_PAGE = "changes_page"
_SENT_AGAIN = frozenset({READ, IDEMPOTENT})
# How long a session dropped after a timeout gets to detach before it is left to Playwright.
_DETACH_TIMEOUT_S = 1.0
# Attaches still running after their deadline, kept referenced until they finish and are detached.
_LATE_ATTACHES: set[asyncio.Future[Any]] = set()

# Ids remembered per page for telling an id from an earlier document from one
# the tab never had. Only the wording and code of a refusal depend on it, and
# past this many ids that is best-effort.
_ISSUED_LIMIT = 10_000
# What a CDP error says when the frame id the world was created with is gone
# (a prerender activation can swap the main frame).
_NO_FRAME = "No frame for given id found"
# What a CDP error says when Playwright has disposed of the session.
_SESSION_GONE = ("has been closed", "Target closed", "Session closed")
# A change sent to the page whose answer was lost after it was sent.
_CHANGE_UNCONFIRMED = "change_unconfirmed"

# Returns the world's registry, creating it on first use. Evaluated in the
# controller's isolated world only.
ELEMENT_REGISTRY_SCRIPT = r"""
(() => {
  const key = '__autoBrowserElementIds';
  // Only an own property the world defined is the registry: an element with this
  // id reaches globalThis[key] through Window named access, in this world too.
  if (Object.prototype.hasOwnProperty.call(globalThis, key)) {
    const existing = globalThis[key];
    if (existing && existing.kind === key) return existing;
    throw new Error('auto-browser: the element-id registry is not the controller\'s');
  }
  const hex = (length) => {
    const bytes = new Uint8Array(length);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  };
  // Read through the prototypes' own getters: a form's named controls shadow its properties, in this world too.
  const isConnected = Object.getOwnPropertyDescriptor(Node.prototype, 'isConnected').get;
  const ownerDocument = Object.getOwnPropertyDescriptor(Node.prototype, 'ownerDocument').get;
  const byId = new Map();
  const byElement = new WeakMap();
  let touched = [];
  const registry = {
    kind: key,
    // Names this document. An id issued under another tag is from an earlier document.
    tag: hex(8),
    // Entries whose element was collected are dropped once the map reaches
    // this size, and the size doubles each time, so a page that keeps its
    // nodes costs constant amortised work per id rather than a scan per id.
    pruneAt: 4096,
    // One-time upload markers: nonce -> the file names the element held when its change event fired.
    uploads: new Map(),
    // A drag's scroll watches: nonce -> whether anything in the document scrolled, and the listener.
    scrollWatches: new Map(),
    issue(el) {
      let id = byElement.get(el);
      if (!id) {
        if (byId.size >= registry.pruneAt) registry.prune();
        do { id = 'op-' + hex(12); } while (byId.has(id));
        byElement.set(el, id);
        byId.set(id, new WeakRef(el));
      }
      touched.push(id);
      return id;
    },
    prune() {
      for (const [known, ref] of byId) if (!ref.deref()) byId.delete(known);
      registry.pruneAt = Math.max(registry.pruneAt, byId.size * 2);
    },
    idOf(el) {
      return (el && byElement.get(el)) || null;
    },
    // The element an id names while it is in this document. One the page moved into
    // another document, a same-origin frame's, is gone: its box there is the frame's.
    find(id) {
      const ref = byId.get(id);
      if (!ref) return { status: 'unknown' };
      const el = ref.deref();
      const here = el && isConnected.call(el) && ownerDocument.call(el) === document;
      return here ? { status: 'ok', element: el } : { status: 'gone' };
    },
    // The element an id named, attached or not.
    element(id) {
      const ref = byId.get(id);
      return (ref && ref.deref()) || null;
    },
    takeTouched() {
      const ids = touched;
      touched = [];
      return ids;
    },
  };
  Object.defineProperty(globalThis, key, { value: registry });
  return registry;
})()
"""


@lru_cache(maxsize=32)
def observation_declaration(script: str) -> str:
    """A function for the world that runs ``(arg, elementIds) => ...`` and reports the ids it issued."""
    return (
        "(arg) => {\n"
        f"  const registry = {ELEMENT_REGISTRY_SCRIPT};\n"
        "  registry.takeTouched();\n"
        f"  const value = ({script})(arg, registry);\n"
        "  return { value, issued: registry.takeTouched(), tag: registry.tag };\n"
        "}"
    )


@lru_cache(maxsize=32)
def element_declaration(script: str, *, attached: bool = True) -> str:
    """A function for the world that runs ``(el, arg, elementIds) => ...`` on the element an id names.

    It answers ``{status, tag, value}``: the script runs only when the id is
    known, the element is attached (unless ``attached`` is false) and, when a
    tag is given, the document is still the one that tag names.
    """
    lookup = (
        "registry.find(elementId)"
        if attached
        else "(registry.element(elementId) ? { status: 'ok', element: registry.element(elementId) } "
        ": registry.find(elementId))"
    )
    return (
        "async (elementId, tag, arg) => {\n"
        f"  const registry = {ELEMENT_REGISTRY_SCRIPT};\n"
        "  if (tag !== null && registry.tag !== tag) return { status: 'changed', tag: registry.tag };\n"
        f"  const found = {lookup};\n"
        "  if (found.status !== 'ok') return { status: found.status, tag: registry.tag };\n"
        f"  return {{ status: 'ok', tag: registry.tag, value: await ({script})(found.element, arg, registry) }};\n"
        "}"
    )


def _script_error(details: dict[str, Any]) -> str:
    exception = details.get("exception") or {}
    text = str(exception.get("description") or details.get("text") or "script error")
    return f"{text.splitlines()[0]}. Observe again before acting on this element."


class PageWorld:
    """One page's isolated world, reached over a CDP session of its own."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._cdp: CDPSession | None = None
        self._frame_id: str | None = None
        self._lock = asyncio.Lock()
        # element id -> tag of the document it was issued in.
        self._issued: OrderedDict[str, str] = OrderedDict()

    @property
    def timeout_ms(self) -> int:
        return int(self._settings.action_timeout_ms)

    async def call(self, page: "Page", declaration: str, *args: Any, effect: str, timeout_ms: int | None = None) -> Any:
        """Run ``declaration`` in the world with ``args``, within ``timeout_ms`` per message.

        ``effect`` says what the call does to the page, and so whether it is
        sent once more after the session went away or the frame id changed
        (when its answer may have been lost after it ran): a ``READ`` or an
        ``IDEMPOTENT`` change (scrolling into view, removing the upload marker)
        is; a call that ``CHANGES_PAGE`` (focus, a selection, placing the upload
        marker) never is: when its answer is lost after the function was sent,
        it raises ``change_unconfirmed`` (not retryable), since the change may
        have been made. A press or keys never go through the world at all. A
        message that gets no answer within its deadline raises
        ``page_not_responding`` and is never sent again.
        """
        timeout_ms = self.timeout_ms if timeout_ms is None else timeout_ms
        try:
            return await self._call(page, declaration, args, timeout_ms)
        except _AnswerLost as exc:
            if effect not in _SENT_AGAIN:
                raise BrowserActionError(
                    "The page's session went away after a change was sent to it (a focus, a selection or the "
                    "upload marker), so the change may have been made. Observe the page before acting again.",
                    code=_CHANGE_UNCONFIRMED,
                    retryable=False,
                ) from exc
            logger.debug("element ids: calling the world again after: %s", exc.message)
            return await self._call(page, declaration, args, timeout_ms)
        except PlaywrightError as exc:
            if effect not in _SENT_AGAIN or not (_NO_FRAME in exc.message or _session_gone(exc)):
                raise
            logger.debug("element ids: calling the world again after: %s", exc.message)
            return await self._call(page, declaration, args, timeout_ms)

    async def _call(self, page: "Page", declaration: str, args: tuple[Any, ...], timeout_ms: int) -> Any:
        cdp, frame_id = await self._session(page)
        # Asked for on every call rather than cached: Chromium answers with the
        # same world until the frame's document changes and a new one after, and
        # a cached context could outlive its document (the back/forward cache)
        # and answer for a page that is no longer shown.
        world = await self._send(cdp, "Page.createIsolatedWorld", {"frameId": frame_id, "worldName": WORLD_NAME})
        try:
            reply = await self._send(
                cdp,
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": declaration,
                    "executionContextId": world["executionContextId"],
                    "arguments": [{"value": arg} for arg in args],
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                timeout_ms,
            )
        except PlaywrightError as exc:
            if _NO_FRAME in exc.message or _session_gone(exc):
                # The function was sent: it may have run before its answer was lost.
                raise _AnswerLost(exc.message) from exc
            raise
        if reply.get("exceptionDetails"):
            raise PlaywrightError(_script_error(reply["exceptionDetails"]))
        return (reply.get("result") or {}).get("value")

    async def _session(self, page: "Page") -> tuple["CDPSession", str]:
        if self._cdp is not None and self._frame_id is not None:
            return self._cdp, self._frame_id
        async with self._lock:
            if self._cdp is None:
                self._cdp = await self._attach(page)
            if self._frame_id is None:
                tree = await self._send(self._cdp, "Page.getFrameTree")
                self._frame_id = tree["frameTree"]["frame"]["id"]
            return self._cdp, self._frame_id

    async def _attach(self, page: "Page") -> "CDPSession":
        attaching = asyncio.ensure_future(page.context.new_cdp_session(page))
        _LATE_ATTACHES.add(attaching)
        attaching.add_done_callback(_LATE_ATTACHES.discard)
        try:
            return await self._within(asyncio.shield(attaching), "a CDP session", self.timeout_ms)
        except BaseException:
            # Timed out or cancelled: the attach can still complete, and its session is then detached, not left open.
            attaching.add_done_callback(_detach_when_attached)
            raise

    async def _send(
        self, cdp: "CDPSession", method: str, params: dict[str, Any] | None = None, timeout_ms: int | None = None
    ) -> dict[str, Any]:
        try:
            return await self._within(cdp.send(method, params), method, timeout_ms or self.timeout_ms, cdp)
        except PlaywrightError as exc:
            # A call still in flight on a session another call already dropped
            # leaves the session that replaced it alone.
            if self._cdp is cdp and _NO_FRAME in exc.message:
                self._frame_id = None
            elif self._cdp is cdp and _session_gone(exc):
                self._reset()
            raise

    async def _within(self, awaitable: Any, what: str, timeout_ms: int, cdp: "CDPSession | None" = None) -> Any:
        try:
            return await asyncio.wait_for(awaitable, timeout_ms / 1000)
        except asyncio.TimeoutError:
            if cdp is None or self._cdp is cdp:
                self._reset()
            if cdp is not None:
                spawn_background_task(_detach(cdp))
            raise BrowserActionError(
                f"The page did not answer within {timeout_ms} ms ({what}): it may have crashed or hung, "
                "and nothing more was sent to it. Reload the page or open a new tab before acting again.",
                code=PAGE_NOT_RESPONDING,
                status_code=504,
                retryable=False,
            ) from None

    def _reset(self) -> None:
        self._cdp = None
        self._frame_id = None

    def remember(self, element_ids: list[str], tag: str) -> None:
        for element_id in element_ids:
            self._issued[element_id] = tag
            self._issued.move_to_end(element_id)
        while len(self._issued) > _ISSUED_LIMIT:
            self._issued.popitem(last=False)

    def lookup_error(self, element_id: str, status: str, tag: str) -> BrowserActionError:
        observe_again = "Call browser.observe and use an element_id from the new observation."
        issued_in = self._issued.get(element_id)
        if status == "changed" or (status == "unknown" and issued_in is not None and issued_in != tag):
            message, code = (
                f"element_id {element_id} came from an earlier document: this tab has loaded a new one "
                "since the observation that returned it.",
                "stale",
            )
        elif status == "gone" or (status == "unknown" and issued_in == tag):
            message, code = f"element_id {element_id} named an element that is no longer on the page.", "stale"
        else:
            message, code = f"element_id {element_id} was not issued for the page in the current tab.", "unknown"
        return BrowserActionError(f"{message} {observe_again}", code=f"{code}_element_id", retryable=True)


class _AnswerLost(PlaywrightError):
    """The session or frame went away after the function was sent to the world."""


def _session_gone(exc: PlaywrightError) -> bool:
    return any(fragment in exc.message for fragment in _SESSION_GONE)


def _detach_when_attached(attaching: "asyncio.Future[CDPSession]") -> None:
    if attaching.cancelled():
        return
    if attaching.exception() is not None:
        logger.debug("element ids: a late CDP attach failed: %r", attaching.exception())
        return
    spawn_background_task(_detach(attaching.result()))


async def _detach(cdp: "CDPSession") -> None:
    """Close a session dropped after a timeout, so each one does not stay open for the life of the page."""
    try:
        await asyncio.wait_for(cdp.detach(), _DETACH_TIMEOUT_S)
    except (PlaywrightError, asyncio.TimeoutError) as exc:
        # A hung renderer may not answer this either; Playwright closes the session with the page.
        logger.debug("element ids: a dropped CDP session was not detached: %r", exc)

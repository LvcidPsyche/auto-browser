"""
network_inspector.py — CDP-level network capture for browser sessions.

Attaches Playwright request/response event listeners to a page and accumulates
structured network entries with optional PII scrubbing applied to bodies.

Each entry:
  {
    "id":            str  — unique request ID (uuid)
    "session_id":    str
    "timestamp":     ISO-8601 UTC string
    "method":        str  — GET / POST / ...
    "url":           str  — full request URL
    "resource_type": str  — document / xhr / fetch / script / ...
    "status":        int | None
    "content_type":  str | None
    "request_headers": dict
    "response_headers": dict | None
    "request_body":  str | None  — text, None if binary or disabled
    "response_body": str | None  — text, None if binary or too large; filled in
                                   after requestfinished if the body is still being
                                   read, so hooks may see None (the hook's entry is
                                   the log's entry and is updated in place)
    "duration_ms":   float | None
    "failed":        bool
    "failure_text":  str | None
    "pii_redacted":  bool
  }

A request made by a page ALLOWED_HOSTS refuses (see `_from_refused_page`) is
recorded with `"withheld": WITHHELD`: its URL cut to the origin, and no
headers or bodies.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Request, Response

    from .pii_scrub import PiiScrubber
from .utils import UTC, spawn_background_task

logger = logging.getLogger(__name__)

_SKIP_RESOURCE_TYPES = frozenset({"image", "font", "media", "websocket"})
WITHHELD = "page off ALLOWED_HOSTS"
_STANDARD_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

HookFn = Callable[[dict[str, Any]], Awaitable[None]]


class NetworkInspector:
    """
    Manages network capture for one browser session.

    Attach to a page with `attach(page)`.
    Detach with `detach()` — removes listeners.
    Read log with `entries()`.
    """

    def __init__(
        self,
        session_id: str,
        *,
        max_entries: int = 500,
        capture_bodies: bool = True,
        body_max_bytes: int = 16384,
        scrubber: "PiiScrubber | None" = None,
        url_refused: Callable[[str], bool] | None = None,
        page_refused: Callable[[Any], bool] | None = None,
        session_refused: Callable[[], bool] | None = None,
    ):
        self.session_id = session_id
        self.max_entries = max_entries
        self.capture_bodies = capture_bodies
        self.body_max_bytes = body_max_bytes
        self.scrubber = scrubber
        # Whether ALLOWED_HOSTS refuses a top-level page at this URL (the session's
        # manager._url_off_list); None records every request whole. page_refused
        # judges the page that made a request (manager._page_refused, which also
        # knows where the page came from); without it, the page's URL is judged.
        # session_refused says whether a page of the session can run a refused host's code
        # (manager._session_has_refused_page).
        self.url_refused = url_refused
        self.page_refused = page_refused
        self.session_refused = session_refused

        self._log: deque[dict[str, Any]] = deque(maxlen=max_entries)
        # request_id → partial entry. A request its document still had in flight when the page navigated
        # away is not settled here: Playwright emits neither requestfailed nor requestfinished for it,
        # whether its response had arrived or not (Playwright 1.62, Chromium, probed 2026-10-27 with the
        # guard's page.goto("about:blank")), so there is no event to settle it on. It stays pending until
        # the eviction in _handle_request logs it as no longer tracked, withheld if it was: the log
        # under-reports, and nothing a refused page sent is shown.
        self._pending: dict[str, dict[str, Any]] = {}
        self._page: "Page | BrowserContext | None" = None
        self._lock = asyncio.Lock()
        self._hooks: dict[str, HookFn] = {}

    def attach(self, page: "Page | BrowserContext") -> None:
        """Register listeners on a Playwright Page, or a BrowserContext for all its pages."""
        self._page = page
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)
        page.on("requestfinished", self._on_request_finished)

    def detach(self) -> None:
        """Remove listeners from the attached page and flush any in-flight entries."""
        if self._page is None:
            return
        try:
            self._page.remove_listener("request", self._on_request)
            self._page.remove_listener("response", self._on_response)
            self._page.remove_listener("requestfailed", self._on_request_failed)
            self._page.remove_listener("requestfinished", self._on_request_finished)
        except Exception as exc:
            logger.debug("network inspector listener removal failed (page likely closed): %s", exc)
        self._page = None
        # Drain pending entries — requestfailed/requestfinished will never fire after detach
        spawn_background_task(self._flush_pending())

    def entries(
        self, limit: int = 100, method: str | None = None, url_contains: str | None = None
    ) -> list[dict[str, Any]]:
        """Return captured network entries, most recent first."""
        items = list(self._log)
        if method:
            items = [e for e in items if e.get("method", "").upper() == method.upper()]
        if url_contains:
            items = [e for e in items if url_contains in e.get("url", "")]
        return list(reversed(items))[:limit]

    async def _flush_pending(self) -> None:
        """Move all pending entries into the log as failed — called on detach."""
        async with self._lock:
            for entry in self._pending.values():
                entry["failed"] = True
                entry["failure_text"] = "session detached"
                entry["duration_ms"] = _elapsed_ms(entry)
                entry.pop("_started_at", None)
                self._log.append(entry)
            self._pending.clear()

    def clear(self) -> None:
        self._log.clear()
        self._pending.clear()

    def summary(self) -> dict[str, Any]:
        items = list(self._log)
        return {
            "total": len(items),
            "failed": sum(1 for e in items if e.get("failed")),
            "pii_redacted": sum(1 for e in items if e.get("pii_redacted")),
            "hooks": len(self._hooks),
        }

    def register_hook(self, url_pattern: str, fn: HookFn) -> None:
        self._hooks[url_pattern] = fn

    def remove_hook(self, url_pattern: str) -> bool:
        return self._hooks.pop(url_pattern, None) is not None

    def list_hooks(self) -> list[str]:
        return list(self._hooks.keys())

    async def _fire_hooks(self, entry: dict[str, Any]) -> None:
        url = entry.get("url") or ""
        for pattern, fn in list(self._hooks.items()):
            if fnmatch.fnmatch(url, pattern):
                try:
                    await fn(entry)
                except Exception as exc:
                    logger.warning("network inspector hook %r raised: %s", pattern, exc)

    # ── Listeners (synchronous wrappers → schedule async work) ─────────────

    def _on_request(self, request: "Request") -> None:
        # Decided while the event is delivered: by the time the task runs, the page
        # guard may already have sent the page that made the request to about:blank.
        spawn_background_task(self._handle_request(request, refused=self._from_refused_page(request)))

    def _from_refused_page(self, request: "Request") -> bool:
        """Whether a page ALLOWED_HOSTS refuses made the request, or it navigates a top-level page to one.

        A top-level navigation is also withheld when its frame is not available yet
        (a popup's first request, whatever its URL) and when it starts while a page
        of the session counts as refused (session_refused). Any other request with no
        frame is judged by its service worker's script URL and by session_refused;
        one with no worker either comes from a page not reported yet and is refused.
        Iframes and subresources an allowed page loads from other hosts are not refused.
        A request that cannot be judged is.
        """
        if self.url_refused is None:
            return False
        try:
            return self._refused(request, self.url_refused)
        except Exception as exc:
            # Fail closed: withheld, not logged whole.
            logger.debug("network inspector could not attribute a request; withheld: %s", type(exc).__name__)
            return True

    def _refused(self, request: "Request", url_refused: Callable[[str], bool]) -> bool:
        try:
            frame = request.frame
        except PlaywrightError:
            frame = None
        top_level = frame is None or frame.parent_frame is None
        if request.is_navigation_request() and request.resource_type == "document" and top_level:
            if url_refused(request.url):
                return True
            # A popup's first navigation, whose frame is not available yet, is withheld by the frameless
            # rule below: its page cannot be judged yet, and a refused opener may have chosen the URL.
            # A navigation that starts while a page of the session counts as refused may be its choice.
            if self.session_refused is not None and self.session_refused():
                return True
        if frame is None:
            worker = request.service_worker
            if worker is None:
                # In this driver only a page the context has not reported yet makes these: a refused
                # page's window that never commits (a 204) stays unreported, and the refused page can
                # script its blank document. Such a page cannot be judged, so the request is withheld.
                return True
            # A service worker's request: it outlives the page that registered it, and serves every
            # page of its scope, a refused one among them, so it is withheld while a page of the
            # session counts as refused.
            return url_refused(worker.url) or (self.session_refused is not None and self.session_refused())
        if self.page_refused is not None:
            return self.page_refused(frame.page)
        return url_refused(frame.page.url)

    def _on_response(self, response: "Response") -> None:
        spawn_background_task(self._handle_response(response))

    def _on_request_failed(self, request: "Request") -> None:
        spawn_background_task(self._handle_request_failed(request))

    def _on_request_finished(self, request: "Request") -> None:
        spawn_background_task(self._handle_request_finished(request))

    # ── Async handlers ─────────────────────────────────────────────────────

    async def _handle_request(self, request: "Request", *, refused: bool = False) -> None:
        try:
            resource_type = getattr(request, "resource_type", "other") or "other"
            if resource_type in _SKIP_RESOURCE_TYPES:
                return

            req_id = str(uuid4())
            method = request.method or "GET"
            if refused:
                # The page chooses the method token too: only a standard one is kept.
                method = method.upper() if method.upper() in _STANDARD_METHODS else "OTHER"
                url, pii_hit, req_body, headers = url_origin(request.url or ""), False, None, {}
            else:
                url, pii_hit, req_body, headers = self._request_content(request, method)

            entry: dict[str, Any] = {
                "id": req_id,
                "session_id": self.session_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "method": method,
                "url": url,
                "resource_type": resource_type,
                "status": None,
                "content_type": None,
                "request_headers": headers,
                "response_headers": None,
                "request_body": req_body,
                "response_body": None,
                "duration_ms": None,
                "failed": False,
                "failure_text": None,
                "pii_redacted": pii_hit,
                "_started_at": datetime.now(UTC).timestamp(),
            }
            if refused:
                entry["withheld"] = WITHHELD

            # Store request ID on the Playwright Request object for cross-referencing
            try:
                request._pii_entry_id = req_id  # type: ignore[attr-defined]
            except Exception as exc:
                logger.debug("could not tag request object for %s: %s", url, exc)

            untracked: list[dict[str, Any]] = []
            async with self._lock:
                # Requests that never finish (long polls, streams a page keeps
                # opening) stayed here for the life of the session. Past the
                # log's size the oldest is logged as untracked instead.
                while len(self._pending) >= self.max_entries:
                    stale = self._pending.pop(next(iter(self._pending)))
                    stale["failed"] = True
                    stale["failure_text"] = "no longer tracked: too many requests in flight"
                    stale["duration_ms"] = _elapsed_ms(stale)
                    stale.pop("_started_at", None)
                    self._log.append(stale)
                    untracked.append(stale)
                self._pending[req_id] = entry
            # Logged like any finished entry, so hooks see it like one.
            for stale in untracked:
                await self._fire_hooks(stale)

        except Exception as exc:
            logger.debug("network inspector request error: %s", exc)

    def _request_content(self, request: "Request", method: str) -> tuple[str, bool, str | None, dict[str, str]]:
        """(scrubbed URL, whether anything was redacted, request body, masked headers) of a request."""
        # Scrubbed before anything else, so no log line below carries a raw query.
        url, pii_hit = scrub_url(request.url or "", self.scrubber, self.body_max_bytes)

        # Capture request body (if enabled and POST/PUT/PATCH)
        req_body: str | None = None
        if self.capture_bodies and method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            try:
                raw = request.post_data
                if raw and len(raw) <= self.body_max_bytes:
                    req_body = raw
            except Exception as exc:
                logger.debug("could not read request body for %s: %s", url, exc)

        # Scrub request body
        if req_body and self.scrubber:
            ct = request.headers.get("content-type") or ""
            req_body, body_hit = _scrub_body(self.scrubber, req_body, ct, url)
            pii_hit = pii_hit or body_hit

        # Sanitize headers — remove Authorization / Cookie values
        headers, header_hit = _mask_sensitive_headers(dict(request.headers or {}), self.scrubber, self.body_max_bytes)
        return url, pii_hit or header_hit, req_body, headers

    async def _handle_response(self, response: "Response") -> None:
        try:
            request = response.request
            req_id = getattr(request, "_pii_entry_id", None)
            if req_id is None:
                return

            status = response.status
            resp_headers = dict(response.headers or {})
            content_type = resp_headers.get("content-type", "")

            async with self._lock:
                entry = self._pending.get(req_id)
                if entry is None:
                    return
                # Recorded before the body is awaited: requestfinished can log
                # the entry and fire hooks while the body is still being read,
                # and they saw status None.
                entry["status"] = status
                if entry.get("withheld"):
                    return
                entry["content_type"] = content_type
                entry["response_headers"], header_hit = _mask_sensitive_headers(
                    resp_headers, self.scrubber, self.body_max_bytes
                )
                entry["pii_redacted"] = entry["pii_redacted"] or header_hit

            # Capture response body (text/json only, size limited)
            resp_body: str | None = None
            pii_hit = entry.get("pii_redacted", False)
            if self.capture_bodies and _worth_reading_body(content_type, resp_headers, self.body_max_bytes):
                try:
                    # A response with no declared length (chunked, streamed) can
                    # only be size-checked after Playwright has buffered it, so
                    # that case still reads the whole body before it is dropped.
                    raw_bytes = await response.body()
                    if raw_bytes and len(raw_bytes) <= self.body_max_bytes:
                        resp_body = raw_bytes.decode("utf-8", errors="replace")
                except Exception as exc:
                    # Bodies are unavailable for redirects and torn-down contexts.
                    logger.debug("could not read response body for %s: %s", entry.get("url", "?"), exc)
                if resp_body and self.scrubber:
                    resp_body, body_hit = _scrub_body(self.scrubber, resp_body, content_type, entry.get("url", "?"))
                    pii_hit = pii_hit or body_hit

            async with self._lock:
                entry["response_body"] = resp_body
                entry["pii_redacted"] = pii_hit

        except Exception as exc:
            logger.debug("network inspector response error: %s", exc)

    async def _handle_request_failed(self, request: "Request") -> None:
        try:
            req_id = getattr(request, "_pii_entry_id", None)
            if req_id is None:
                return

            async with self._lock:
                entry = self._pending.pop(req_id, None)
                if entry is None:
                    return
                entry["failed"] = True
                failure = getattr(request, "failure", None)
                entry["failure_text"] = failure() if callable(failure) else str(failure or "")
                entry["duration_ms"] = _elapsed_ms(entry)
                entry.pop("_started_at", None)
                self._log.append(entry)
            await self._fire_hooks(entry)

        except Exception as exc:
            logger.debug("network inspector requestfailed error: %s", exc)

    async def _handle_request_finished(self, request: "Request") -> None:
        try:
            req_id = getattr(request, "_pii_entry_id", None)
            if req_id is None:
                return

            async with self._lock:
                entry = self._pending.pop(req_id, None)
                if entry is None:
                    return
                entry["duration_ms"] = _elapsed_ms(entry)
                entry.pop("_started_at", None)
                self._log.append(entry)
            await self._fire_hooks(entry)

        except Exception as exc:
            logger.debug("network inspector requestfinished error: %s", exc)


# ── Helpers ────────────────────────────────────────────────────────────────

_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
        "x-amz-security-token",
        "proxy-authorization",
        "www-authenticate",
    }
)

# Headers whose value is a URL, and can carry its query string's secrets.
_URL_HEADER_NAMES = frozenset({"referer", "location"})


_QUERY_TOO_LONG = "[QUERY TOO LONG]"
_FRAGMENT_TOO_LONG = "[FRAGMENT TOO LONG]"


def _mask_sensitive_headers(
    headers: dict[str, str], scrubber: "PiiScrubber | None" = None, max_bytes: int = 16384
) -> tuple[dict[str, str], bool]:
    """Replace values of security-sensitive headers with [MASKED]; scrub the query and fragment of URL headers.

    Returns (headers, whether a URL header was redacted). Masking a header whose whole value is secret by
    name is not counted: only the scrub that looks at the content is.
    """
    masked: dict[str, str] = {}
    redacted = False
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _SENSITIVE_HEADER_NAMES:
            masked[name] = "[MASKED]"
        elif lowered in _URL_HEADER_NAMES:
            masked[name], hit = scrub_url(value, scrubber, max_bytes)
            redacted = redacted or hit
        else:
            masked[name] = value
    return masked, redacted


def scrub_url(url: str, scrubber: "PiiScrubber | None", max_bytes: int) -> tuple[str, bool]:
    """Scrub a URL's query string and fragment. A scrub that fails, or a part too long to scrub, costs the part.

    The scrub runs on the event loop, so the query and the fragment are each only scrubbed up to the same
    size cap as a body; a longer one is replaced, not stored. A query that is too long takes the fragment
    with it. A fragment carries secrets too: an OAuth implicit grant puts `access_token` there.
    """
    if not url or scrubber is None:
        return url, False
    head, hash_mark, fragment = url.partition("#")
    path, _, query = head.partition("?")
    # The cap is in bytes: a character can take up to four. "surrogatepass" keeps a lone surrogate in a URL
    # from raising.
    if _byte_length(query) > max_bytes:
        return f"{path}?{_QUERY_TOO_LONG}", True
    fragment_too_long = _byte_length(fragment) > max_bytes
    try:
        scrubbed, hits = scrubber.url(head if fragment_too_long else url)
    except Exception as exc:
        logger.warning("network inspector could not scrub the query of %s: %s", url_origin(url), type(exc).__name__)
        return path, True
    if not isinstance(scrubbed, str):
        return path, True
    if fragment_too_long:
        return f"{scrubbed}#{_FRAGMENT_TOO_LONG}", True
    return scrubbed, bool(hits)


def _byte_length(text: str) -> int:
    return len(text.encode("utf-8", errors="surrogatepass"))


def url_origin(url: str) -> str:
    """scheme://host:port of a URL, without its user, path, query or fragment; a blob: URL's is the page's."""
    parts = urlsplit(url)
    if parts.scheme == "blob":
        inner = urlsplit(parts.path)
        # blob:null/uuid: made by a page with an opaque origin, serialized "null" as browsers do.
        return url_origin(parts.path) if inner.scheme and inner.netloc else "null"
    if not parts.netloc:
        return f"{parts.scheme}:"
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


def _scrub_body(scrubber: "PiiScrubber", body: str, content_type: str, url: str) -> tuple[str | None, bool]:
    """Scrub a captured body; whenever the result cannot be trusted the body is dropped, not kept raw.

    Returns (body to store, whether anything was redacted).
    """
    try:
        scrubbed, hits = scrubber.network_body(body, content_type)
    except Exception as exc:
        logger.warning("network inspector could not scrub a body for %s: %s", url, type(exc).__name__)
        return None, True
    # The value is checked before the hits: a scrubber that reports no hits has not vouched for a
    # value that is not text, and the raw body must not stand in for it.
    if not isinstance(scrubbed, str) or not scrubbed:
        return None, True
    if not hits:
        return body, False
    return scrubbed, True


def _elapsed_ms(entry: dict[str, Any]) -> float | None:
    started = entry.get("_started_at")
    if started is None:
        return None
    elapsed = datetime.now(UTC).timestamp() - started
    return round(elapsed * 1000, 2)


_TEXT_CONTENT_MARKERS = ("json", "text", "html", "xml", "javascript")


def _worth_reading_body(content_type: str, headers: dict[str, str], max_bytes: int) -> bool:
    """Decide from the headers alone whether reading the body can be useful.

    Playwright's `response.body()` buffers the whole payload in memory, so a
    binary download or a declared-oversize body must be ruled out before the
    call, not after.
    """
    if not any(marker in content_type.lower() for marker in _TEXT_CONTENT_MARKERS):
        return False
    declared = {k.lower(): v for k, v in headers.items()}.get("content-length")
    try:
        return declared is None or int(declared) <= max_bytes
    except ValueError:
        # A malformed length is not proof of size; fall back to the post-read check.
        return True

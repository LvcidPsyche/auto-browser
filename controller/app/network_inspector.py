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
    "response_body": str | None  — text, None if binary or too large
    "duration_ms":   float | None
    "failed":        bool
    "failure_text":  str | None
    "pii_redacted":  bool
  }
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from playwright.async_api import Page, Request, Response

    from .pii_scrub import PiiScrubber
from .utils import UTC

logger = logging.getLogger(__name__)

_SKIP_RESOURCE_TYPES = frozenset({"image", "font", "media", "websocket"})


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
    ):
        self.session_id = session_id
        self.max_entries = max_entries
        self.capture_bodies = capture_bodies
        self.body_max_bytes = body_max_bytes
        self.scrubber = scrubber

        self._log: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._pending: dict[str, dict[str, Any]] = {}  # request_id → partial entry
        self._page: "Page | None" = None
        self._lock = asyncio.Lock()

    def attach(self, page: "Page") -> None:
        """Register listeners on a Playwright Page."""
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
        except Exception:
            pass
        self._page = None
        # Drain pending entries — requestfailed/requestfinished will never fire after detach
        asyncio.ensure_future(self._flush_pending())

    def entries(self, limit: int = 100, method: str | None = None, url_contains: str | None = None) -> list[dict[str, Any]]:
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
        }

    # ── Listeners (synchronous wrappers → schedule async work) ─────────────

    def _on_request(self, request: "Request") -> None:
        asyncio.ensure_future(self._handle_request(request))

    def _on_response(self, response: "Response") -> None:
        asyncio.ensure_future(self._handle_response(response))

    def _on_request_failed(self, request: "Request") -> None:
        asyncio.ensure_future(self._handle_request_failed(request))

    def _on_request_finished(self, request: "Request") -> None:
        asyncio.ensure_future(self._handle_request_finished(request))

    # ── Async handlers ─────────────────────────────────────────────────────

    async def _handle_request(self, request: "Request") -> None:
        try:
            resource_type = getattr(request, "resource_type", "other") or "other"
            if resource_type in _SKIP_RESOURCE_TYPES:
                return

            req_id = str(uuid4())
            url = request.url or ""
            method = request.method or "GET"

            # Capture request body (if enabled and POST/PUT/PATCH)
            req_body: str | None = None
            if self.capture_bodies and method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                try:
                    raw = request.post_data
                    if raw and len(raw) <= self.body_max_bytes:
                        req_body = raw
                except Exception:
                    pass

            # Scrub request body
            pii_hit = False
            if req_body and self.scrubber:
                ct = (request.headers.get("content-type") or "")
                scrubbed, hits = self.scrubber.network_body(req_body, ct)
                if hits:
                    req_body = scrubbed if isinstance(scrubbed, str) else (scrubbed.decode("utf-8", "replace") if scrubbed else req_body)
                    pii_hit = True

            # Sanitize headers — remove Authorization / Cookie values
            headers = dict(request.headers or {})
            headers = _mask_sensitive_headers(headers)

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

            # Store request ID on the Playwright Request object for cross-referencing
            try:
                request._pii_entry_id = req_id  # type: ignore[attr-defined]
            except Exception:
                pass

            async with self._lock:
                self._pending[req_id] = entry

        except Exception as exc:
            logger.debug("network inspector request error: %s", exc)

    async def _handle_response(self, response: "Response") -> None:
        try:
            request = response.request
            req_id = getattr(request, "_pii_entry_id", None)
            if req_id is None:
                return

            async with self._lock:
                entry = self._pending.get(req_id)
                if entry is None:
                    return

            status = response.status
            resp_headers = dict(response.headers or {})
            content_type = resp_headers.get("content-type", "")

            # Capture response body (text/json only, size limited)
            resp_body: str | None = None
            pii_hit = entry.get("pii_redacted", False)
            if self.capture_bodies:
                try:
                    raw_bytes = await response.body()
                    if raw_bytes and len(raw_bytes) <= self.body_max_bytes:
                        is_text = any(
                            m in content_type.lower()
                            for m in ("json", "text", "html", "xml", "javascript")
                        )
                        if is_text:
                            resp_body = raw_bytes.decode("utf-8", errors="replace")
                            if self.scrubber:
                                scrubbed, hits = self.scrubber.network_body(resp_body, content_type)
                                if hits:
                                    resp_body = scrubbed if isinstance(scrubbed, str) else resp_body
                                    pii_hit = True
                except Exception:
                    pass

            async with self._lock:
                entry["status"] = status
                entry["content_type"] = content_type
                entry["response_headers"] = _mask_sensitive_headers(resp_headers)
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

        except Exception as exc:
            logger.debug("network inspector requestfinished error: %s", exc)


# ── Helpers ────────────────────────────────────────────────────────────────

_SENSITIVE_HEADER_NAMES = frozenset({
    "authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token",
    "x-csrf-token", "proxy-authorization", "www-authenticate",
})


def _mask_sensitive_headers(headers: dict[str, str]) -> dict[str, str]:
    """Replace values of security-sensitive headers with [MASKED]."""
    return {
        k: "[MASKED]" if k.lower() in _SENSITIVE_HEADER_NAMES else v
        for k, v in headers.items()
    }


def _elapsed_ms(entry: dict[str, Any]) -> float | None:
    started = entry.get("_started_at")
    if started is None:
        return None
    elapsed = datetime.now(UTC).timestamp() - started
    return round(elapsed * 1000, 2)

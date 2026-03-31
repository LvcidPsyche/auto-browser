"""
events.py — Per-session SSE event bus for auto-browser.

Subscribers receive a queue that gets populated when the browser manager
emits observe/action/approval events. The SSE endpoint drains the queue
and streams events to the client.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from .utils import UTC

logger = logging.getLogger(__name__)

_SESSION_QUEUES: dict[str, list[asyncio.Queue]] = defaultdict(list)
_GLOBAL_QUEUES: list[asyncio.Queue] = []


def _now() -> str:
    return datetime.now(UTC).isoformat()


def subscribe(session_id: str) -> asyncio.Queue:
    """Return a new queue that will receive events for *session_id*."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _SESSION_QUEUES[session_id].append(q)
    return q


def subscribe_all() -> asyncio.Queue:
    """Return a queue that receives events for every session."""
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _GLOBAL_QUEUES.append(q)
    return q


def unsubscribe(session_id: str, q: asyncio.Queue) -> None:
    try:
        _SESSION_QUEUES[session_id].remove(q)
    except ValueError:
        pass
    if not _SESSION_QUEUES[session_id]:
        del _SESSION_QUEUES[session_id]


def unsubscribe_all(q: asyncio.Queue) -> None:
    try:
        _GLOBAL_QUEUES.remove(q)
    except ValueError:
        pass


def _dispatch(session_id: str, event: dict[str, Any]) -> None:
    payload = json.dumps(event)
    # Push to session-scoped subscribers
    for q in list(_SESSION_QUEUES.get(session_id, [])):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            logger.debug("SSE queue full for session %s — dropping event", session_id)
    # Push to global subscribers
    for q in list(_GLOBAL_QUEUES):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


# ── Public emit helpers ──────────────────────────────────────────────────────

def emit_observe(session_id: str, url: str, title: str, screenshot_url: str | None = None) -> None:
    _dispatch(session_id, {
        "event": "observe",
        "session_id": session_id,
        "timestamp": _now(),
        "url": url,
        "title": title,
        "screenshot_url": screenshot_url,
    })


def emit_action(session_id: str, action: str, status: str, details: dict[str, Any] | None = None) -> None:
    _dispatch(session_id, {
        "event": "action",
        "session_id": session_id,
        "timestamp": _now(),
        "action": action,
        "status": status,
        "details": details or {},
    })


def emit_approval(session_id: str, approval_id: str, kind: str, status: str, reason: str) -> None:
    _dispatch(session_id, {
        "event": "approval",
        "session_id": session_id,
        "timestamp": _now(),
        "approval_id": approval_id,
        "kind": kind,
        "status": status,
        "reason": reason,
    })


def emit_session(session_id: str, status: str) -> None:
    _dispatch(session_id, {
        "event": "session",
        "session_id": session_id,
        "timestamp": _now(),
        "status": status,
    })

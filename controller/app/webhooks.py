"""
webhooks.py — Approval webhook dispatcher for auto-browser.

When APPROVAL_WEBHOOK_URL is configured, POSTs a signed JSON payload
every time an approval is created or its status changes.

Signature: X-Webhook-Signature: sha256=<hex>
The signature covers the raw request body using HMAC-SHA256 with
APPROVAL_WEBHOOK_SECRET as the key (Slack-compatible format).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from .models import ApprovalRecord

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


def _sign(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


async def dispatch_approval_event(
    approval: ApprovalRecord,
    *,
    webhook_url: str,
    webhook_secret: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget: POST approval event to webhook_url."""
    body: dict[str, Any] = {
        "event": "approval",
        "approval_id": approval.id,
        "session_id": approval.session_id,
        "kind": approval.kind,
        "status": approval.status,
        "reason": approval.reason,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
    }
    if extra:
        body.update(extra)

    raw = json.dumps(body, separators=(",", ":")).encode()
    headers: dict[str, str] = {"Content-Type": "application/json", "User-Agent": "auto-browser/1.0.1"}
    if webhook_secret:
        headers["X-Webhook-Signature"] = _sign(raw, webhook_secret)

    try:
        resp = await get_client().post(webhook_url, content=raw, headers=headers)
        if resp.status_code >= 400:
            logger.warning("webhook %s returned %d", webhook_url, resp.status_code)
    except Exception as exc:
        logger.warning("webhook dispatch failed: %s", exc)

"""
session_share.py — Time-limited observer URLs for browser session handoffs.

Generates HMAC-SHA256 signed tokens that grant read-only observation access
to a live session for a configurable TTL.

Token format (URL-safe base64 of JSON):
  {"session_id": "abc123", "exp": 1234567890, "scope": "observe"}

The token is signed with SHARE_TOKEN_SECRET to prevent forgery.

Endpoints (wired in main.py):
  POST /sessions/{id}/share       → create share token
  GET  /share/{token}             → view session (redirect or HTML)
  GET  /share/{token}/observe     → JSON observe payload (read-only)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Scope constants
SCOPE_OBSERVE = "observe"


class SessionShareManager:
    """Issues and validates time-limited session share tokens."""

    def __init__(self, *, secret: str | None = None, ttl_minutes: int = 60):
        if ttl_minutes <= 0:
            raise ValueError("ttl_minutes must be positive")
        if not secret:
            # Auto-generate an ephemeral secret if none configured.
            # Tokens created with this secret won't survive server restarts.
            import os
            secret = os.urandom(32).hex()
            logger.warning(
                "SHARE_TOKEN_SECRET not configured — using ephemeral secret. "
                "Share tokens will be invalid after server restart."
            )
        self._secret = secret.encode()
        self.ttl_seconds = ttl_minutes * 60

    # ── Token creation ─────────────────────────────────────────────────────

    def create_token(
        self,
        session_id: str,
        *,
        ttl_seconds: int | None = None,
        scope: str = SCOPE_OBSERVE,
    ) -> dict[str, Any]:
        """Create a signed share token for session_id.

        Returns:
            {
              "token": str,
              "session_id": str,
              "scope": str,
              "expires_at": int (unix timestamp),
              "expires_in_seconds": int,
            }
        """
        normalized_session_id = str(session_id).strip()
        if not normalized_session_id:
            raise ValueError("session_id must not be empty")
        if scope != SCOPE_OBSERVE:
            raise ValueError(f"unsupported share scope: {scope}")
        effective_ttl = self.ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if effective_ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        exp = int(time.time()) + effective_ttl
        payload = {"session_id": normalized_session_id, "exp": exp, "scope": scope}
        payload_json = json.dumps(payload, separators=(",", ":"))
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
        sig = self._sign(payload_b64)
        token = f"{payload_b64}.{sig}"
        return {
            "token": token,
            "session_id": normalized_session_id,
            "scope": scope,
            "expires_at": exp,
            "expires_in_seconds": exp - int(time.time()),
        }

    # ── Token validation ───────────────────────────────────────────────────

    def validate_token(self, token: str) -> dict[str, Any]:
        """Validate a share token and return its payload.

        Raises:
            ValueError: If token is invalid, expired, or tampered.

        Returns:
            {"session_id": str, "exp": int, "scope": str}
        """
        try:
            parts = token.split(".")
            if len(parts) != 2:
                raise ValueError("malformed token")
            payload_b64, sig = parts

            # Verify signature
            expected_sig = self._sign(payload_b64)
            if not hmac.compare_digest(sig, expected_sig):
                raise ValueError("invalid token signature")

            # Decode payload
            padding = "=" * (-len(payload_b64) % 4)
            payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode()
            payload = json.loads(payload_json)

        except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid share token: {exc}") from exc

        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("invalid share token: token missing session_id")
        scope = str(payload.get("scope") or "").strip()
        if scope != SCOPE_OBSERVE:
            raise ValueError("invalid share token: unsupported scope")
        exp_raw = payload.get("exp")
        if isinstance(exp_raw, bool):
            raise ValueError("invalid share token: token exp must be an integer")
        try:
            exp = int(exp_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid share token: token exp must be an integer") from exc

        # Check expiry
        if int(time.time()) > exp:
            raise ValueError("share token has expired")

        return {"session_id": session_id, "exp": exp, "scope": scope}

    def token_info(self, token: str) -> dict[str, Any]:
        """Return token info without raising — sets 'valid' field."""
        try:
            payload = self.validate_token(token)
            return {
                "valid": True,
                "session_id": payload["session_id"],
                "scope": payload.get("scope", SCOPE_OBSERVE),
                "expires_at": payload.get("exp"),
                "seconds_remaining": max(0, payload.get("exp", 0) - int(time.time())),
            }
        except ValueError as exc:
            return {"valid": False, "error": str(exc)}

    # ── Private ────────────────────────────────────────────────────────────

    def _sign(self, data: str) -> str:
        return hmac.new(self._secret, data.encode(), hashlib.sha256).hexdigest()[:32]

"""
session_share.py — Time-limited observer URLs for browser session handoffs.

Generates HMAC-SHA256 signed tokens that grant read-only observation access
to a live session for a configurable TTL.

Token format (URL-safe base64 of JSON):
  {"session_id": "abc123", "exp": 1234567890, "scope": "observe", "iat": 1234567890123}

The token is signed with SHARE_TOKEN_SECRET to prevent forgery.

Endpoints (wired in main.py):
  POST /sessions/{id}/share       → create share token
  DELETE /sessions/{id}/share     → revoke every share token of the session minted so far
  GET  /share/{token}             → view session (redirect or HTML)
  GET  /share/{token}/observe     → JSON observe payload (read-only)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .audit import get_current_operator
from .session_ownership import ANONYMOUS, verified_operator
from .utils import PRIVATE_FILE_MODE, atomic_write_text, error_text, error_text_for_log

logger = logging.getLogger(__name__)

# Scope constants
SCOPE_OBSERVE = "observe"

# The audit events of a link: its mint, its first view, and the revoke that ends it.
SHARE_MINTED_EVENT = "share_minted"
SHARE_VIEWED_EVENT = "share_viewed"
SHARE_REVOKED_EVENT = "share_revoked"
# The longest life a link is minted with, whoever asks: the request models stop ttl_minutes at it (ShareSessionRequest,
# ShareSessionInput), and a manager refuses a longer one unless the deployment's own default is longer
# (SHARE_TOKEN_TTL_MINUTES). A revocation is kept until no link it ended could still be good (max_ttl_seconds).
SHARE_TOKEN_MAX_TTL_SECONDS = 24 * 60 * 60

SHARE_REVOCATIONS_UNAVAILABLE = (
    "the store of revoked share links (SHARE_REVOCATION_STORE_PATH) cannot be read or written. While it cannot be "
    "read, no share link is honoured, minted or revoked; a mint that cannot be saved hands out no link, and a revoke "
    "that cannot be saved revokes nothing. The server log has the cause and where the file is."
)


class ShareRevocationStoreError(RuntimeError):
    """The store of revocations could not be read or written; the text is the fixed SHARE_REVOCATIONS_UNAVAILABLE
    (REST answers it as 503, and MCP as the tool's error)."""

    def __init__(self, *_args: object) -> None:
        # *_args: copy and pickle rebuild an exception from its args, which hold the fixed text.
        super().__init__(SHARE_REVOCATIONS_UNAVAILABLE)


class ShareAuditError(Exception):
    """A mint's event could not be written to the audit log, raised from the audit's own error: the server's fault
    whatever that error's class (the file store's trim raises UnicodeDecodeError, a ValueError, for a log that is not
    UTF-8), and never a refusal of the caller's request. No token is handed out."""


# A link's first view is audited once per token per process: the digests of this many tokens are remembered, and the
# one viewed longest ago is forgotten first. A forgotten token's next view is audited again.
SHARE_VIEWED_TOKENS_KEPT = 4096


def token_digest(token: str) -> str:
    """A share token's SHA-256: what stands for it wherever the token itself must not be written or kept (the audit
    log, and the keys of what is remembered per token). The token is the credential, and its digest opens nothing."""
    return hashlib.sha256(token.encode()).hexdigest()


def _is_stamp(value: object) -> bool:
    """Whether ``value`` is a time as a mint or a revoke writes it: a whole number of milliseconds, no bool."""
    return type(value) is int and value >= 0


@dataclass(frozen=True)
class _Revocations:
    """What has been revoked: a link is ended when it was minted at or before the time held for its session, or at or
    before ``every_session``, the mark a corrupt store leaves (0 where there is none). ``minted`` is the stamp the
    last mint gave (0 before any), which a restart starts its stamps after."""

    every_session: int = 0
    sessions: dict[str, int] = field(default_factory=dict)
    minted: int = 0

    def revoked_before(self, session_id: str) -> int:
        return max(self.every_session, self.sessions.get(session_id, 0))


def _revocations_read(data: object) -> _Revocations:
    """The store's JSON as revocations; ValueError for anything a save does not write. A store with no "minted" (the
    first 1.18.0 builds wrote none) has seen no mint."""
    if not isinstance(data, dict):
        raise ValueError(f"it holds a JSON {type(data).__name__}, not an object")
    every_session, sessions, minted = data.get("all", 0), data.get("sessions", {}), data.get("minted", 0)
    if not _is_stamp(every_session) or not isinstance(sessions, dict) or not all(map(_is_stamp, sessions.values())):
        raise ValueError("it holds something other than the times of revocations")
    if not _is_stamp(minted):
        raise ValueError("it holds something other than the time of a mint")
    return _Revocations(every_session, sessions, minted)


class SessionShareManager:
    """Issues and validates time-limited session share tokens."""

    def __init__(self, *, secret: str | None = None, ttl_minutes: int = 60, revocations_path: str | Path | None = None):
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
        self.max_ttl_seconds = max(self.ttl_seconds, SHARE_TOKEN_MAX_TTL_SECONDS)
        # The digests of the tokens whose first view is audited (record_view), the one viewed last at the end.
        self._viewed: OrderedDict[str, None] = OrderedDict()
        self._viewed_lock = asyncio.Lock()
        # Where revocations are kept (SHARE_REVOCATION_STORE_PATH); None keeps them in this process alone.
        self._revocations_path = Path(revocations_path) if revocations_path else None
        # A mint and a revoke each write the store, in a worker thread: one at a time, each taking its stamp under
        # this lock, so that the stamps are in the order of the writes.
        self._store_lock = threading.Lock()
        self._last_stamp = 0
        # None while the store cannot be read. Replaced whole, never changed: a validation reads it without the lock.
        self._revocations: _Revocations | None = None
        try:
            self._read_revocations()
        except ShareRevocationStoreError:
            pass  # Logged. Every link is refused until a mint or a revoke reads the store.

    # ── Token creation ─────────────────────────────────────────────────────

    def create_token(
        self,
        session_id: str,
        *,
        ttl_seconds: int | None = None,
        scope: str = SCOPE_OBSERVE,
    ) -> dict[str, Any]:
        """Create a signed share token for session_id. Blocking (it writes the store): call it in a worker thread.

        The token's stamp is written to the store before the token is returned, so one that cannot be saved raises
        ShareRevocationStoreError and hands out nothing, as does a store that cannot be read.

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
        if effective_ttl > self.max_ttl_seconds:
            # A revocation is dropped once this long has passed: a link with a longer life would outlive its own.
            raise ValueError(f"ttl_seconds must be at most {self.max_ttl_seconds}")
        with self._store_lock:
            # No link is honoured while the store cannot be read: it is read again before one more is handed out.
            held = self._revocations if self._revocations is not None else self._read_revocations()
            # iat: when it was minted, in milliseconds, for a revoke to end it by (validate_token). Kept as the last
            # mint's, so that a revoke after a restart takes a later stamp (_read_revocations).
            iat = self._next_stamp()
            revocations = replace(held, minted=iat)
            self._write_revocations(revocations)
            self._revocations = revocations
        exp = int(time.time()) + effective_ttl
        payload = {"session_id": normalized_session_id, "exp": exp, "scope": scope, "iat": iat}
        payload_json = json.dumps(payload, separators=(",", ":"))
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
        sig = self._sign(payload_b64)
        token = f"{payload_b64}.{sig}"
        return {
            "token": token,
            "session_id": normalized_session_id,
            "scope": scope,
            "expires_at": exp,
            "expires_in_seconds": effective_ttl,
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
            # compare_digest raises TypeError for a non-ASCII str, which escaped
            # token_info() as a 500 on an unauthenticated route.
            if not sig.isascii() or not hmac.compare_digest(sig, expected_sig):
                raise ValueError("invalid token signature")

            # Decode payload
            padding = "=" * (-len(payload_b64) % 4)
            payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode()
            payload = json.loads(payload_json)

        except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid share token: {error_text(exc)}") from exc

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

        revocations = self._revocations
        if revocations is None:
            raise ValueError("share token cannot be checked: the store of revocations cannot be read")
        revoked_before = revocations.revoked_before(session_id)
        # A token that does not say when it was minted (every one minted before 1.18.0) is older than any revocation.
        iat = payload.get("iat")
        if revoked_before and (not _is_stamp(iat) or iat <= revoked_before):
            raise ValueError("share token has been revoked")

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
        except ValueError:
            return {"valid": False, "error": "Invalid token"}

    # ── Revocation ─────────────────────────────────────────────────────────

    def revoke(self, session_id: str) -> int:
        """End every link of ``session_id`` minted so far, and return the time they are ended by: one minted afterwards
        works. Blocking (it writes the store): call it in a worker thread.

        The store is written before the revocation counts, so one that cannot be saved revokes nothing and raises
        ShareRevocationStoreError, as does a store that cannot be read. Revocations no link can still need are dropped
        from the store as it is written.
        """
        normalized_session_id = str(session_id).strip()
        if not normalized_session_id:
            raise ValueError("session_id must not be empty")
        with self._store_lock:
            held = self._revocations if self._revocations is not None else self._read_revocations()
            stamp = self._next_stamp()
            # The oldest link a revocation can still matter to was minted just before it, with the longest life, and
            # is good through the whole of its last second (exp is in whole seconds).
            oldest_needed = int(time.time() * 1000) - (self.max_ttl_seconds + 1) * 1000
            revocations = _Revocations(
                held.every_session if held.every_session >= oldest_needed else 0,
                {
                    **{name: at for name, at in held.sessions.items() if at >= oldest_needed},
                    normalized_session_id: stamp,
                },
                held.minted,
            )
            self._write_revocations(revocations)
            self._revocations = revocations
        return stamp

    def _next_stamp(self, *, ahead_ms: int = 0) -> int:
        """The time, in milliseconds, of a mint or a revoke, at least ``ahead_ms`` past the clock's: later than every
        stamp this controller has given before, in this process or an earlier one, so that a link minted after a
        revoke is never one it ends and a revoke ends every link minted before it. The store keeps the last stamp of a
        mint and of a revoke, each written before its link is handed out or its revocation counts, and
        _read_revocations starts after them; a corrupt store, which no longer says, leaves a mark in their place (see
        there). Called under ``_store_lock``, or by __init__."""
        self._last_stamp = max(int(time.time() * 1000) + ahead_ms, self._last_stamp + 1)
        return self._last_stamp

    def _read_revocations(self) -> _Revocations:
        """Read the store into ``_revocations``, at the start and again for a mint or a revoke while it could not be
        read (those hold ``_store_lock``). ShareRevocationStoreError when it still cannot be.

        A store that is not there holds no revocations. One that cannot be read is left where it is, as the cron
        store is. One that reads as something no save writes can no longer say which links were revoked, and an empty
        one in its place would bring them back: every link minted before now is revoked instead, and the store is
        written again saying so. It is not set aside as a corrupt cron store is: it held nothing to restore.
        """
        path = self._revocations_path
        try:
            revocations = _Revocations() if path is None else _revocations_read(json.loads(path.read_text("utf-8")))
        except FileNotFoundError:
            revocations = _Revocations()
        except OSError as exc:
            logger.error(
                "the store of revoked share links %s could not be read (%s); it was left where it is, and no share "
                "link is honoured, minted or revoked until a mint or a revoke can read it",
                path,
                error_text_for_log(exc),
            )
            raise ShareRevocationStoreError from exc
        except (ValueError, RecursionError) as exc:  # json.JSONDecodeError, UnicodeDecodeError: the file is corrupt
            # It no longer says what was minted either, and after a step back the clock cannot say which links are
            # still alive. A mark one link's longest life past the clock is later than every link minted before it
            # that this clock has not seen expire, unless the clock was stepped back by more than that life (every link
            # then lives as much longer). Every stamp after it is later still (the seed below), so stamps run ahead of
            # the clock until it catches up: nothing but the revocation check (validate_token) reads a link's stamp,
            # and its expiry is its own exp.
            revocations = _Revocations(every_session=self._next_stamp(ahead_ms=(self.max_ttl_seconds + 1) * 1000))
            logger.error(
                "the store of revoked share links %s is corrupt (%s) and no longer says which links were revoked: "
                "every link minted before now is revoked, and the store is written again",
                path,
                error_text_for_log(exc),
            )
            self._write_revocations(revocations)
        self._last_stamp = max(
            self._last_stamp, revocations.every_session, revocations.minted, *revocations.sessions.values()
        )
        self._revocations = revocations
        return revocations

    def _write_revocations(self, revocations: _Revocations) -> None:
        path = self._revocations_path
        if path is None:
            return
        stored = {"all": revocations.every_session, "sessions": revocations.sessions, "minted": revocations.minted}
        text = json.dumps(stored, indent=2)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, text, mode=PRIVATE_FILE_MODE)
        except OSError as exc:
            logger.error("the store of revoked share links %s could not be written (%s)", path, error_text_for_log(exc))
            raise ShareRevocationStoreError from exc

    # ── Audit ──────────────────────────────────────────────────────────────

    async def record_view(self, audit: Any, token: str, session_id: str) -> None:
        """Audit the first view of a valid token, once per token per process; a later view returns at once.

        For a share route that is about to show the session to the token's holder. The event is written before the
        view is answered, so a view whose event cannot be written fails, and the next one tries again. Views that
        arrive together write one event, and the others wait for it. A view cancelled while its event is written can
        be audited twice (the write goes on in a worker thread), never left out.

        The viewer holds the share token and no credential, so a name in its headers is its own text: the event is
        the anonymous operator's unless a named credential proved one.
        """
        digest = token_digest(token)
        if self._viewed_before(digest):
            return
        async with self._viewed_lock:
            if self._viewed_before(digest):
                return
            await audit.append(
                event_type=SHARE_VIEWED_EVENT,
                status="ok",
                action="view_share",
                session_id=session_id,
                operator=get_current_operator() if verified_operator() is not None else ANONYMOUS,
                details={"token_sha256": digest},
            )
            self._viewed[digest] = None
            while len(self._viewed) > SHARE_VIEWED_TOKENS_KEPT:
                self._viewed.popitem(last=False)

    def _viewed_before(self, digest: str) -> bool:
        if digest not in self._viewed:
            return False
        self._viewed.move_to_end(digest)
        return True

    # ── Private ────────────────────────────────────────────────────────────

    def _sign(self, data: str) -> str:
        return hmac.new(self._secret, data.encode(), hashlib.sha256).hexdigest()[:32]


async def mint_share_token(share_manager: Any, audit: Any, session_id: str, *, ttl_seconds: int) -> dict[str, Any]:
    """Mint a share token and audit it: the one mint behind POST /sessions/{id}/share and browser.share_session,
    each of which has first checked that its caller may use the session.

    The token is returned only once its stamp is in the store (create_token, in a worker thread: it reads and writes
    the store) and its event is written: a mint that cannot be audited raises ShareAuditError, and the token made
    for it is dropped unseen. The event names the token by its digest (token_digest).
    """
    token = await asyncio.to_thread(share_manager.create_token, session_id, ttl_seconds=ttl_seconds)
    try:
        await audit.append(
            event_type=SHARE_MINTED_EVENT,
            status="ok",
            action="share_session",
            session_id=token["session_id"],
            details={
                "token_sha256": token_digest(token["token"]),
                "scope": token["scope"],
                "expires_at": token["expires_at"],
            },
        )
    except Exception as cause:
        raise ShareAuditError("the mint of a share link was not audited, and no link was handed out") from cause
    return token


async def ensure_links_revocable(manager: Any, session_id: str) -> None:
    """KeyError unless the caller may use the session, as for a mint, and it is live or closed with its record kept:
    a closed session's links still open the frames taken for them. A live session's page is not touched."""
    try:
        await manager.get_session(session_id)
    except KeyError:
        await manager.get_session_record(session_id)


async def revoke_share_links(share_manager: Any, audit: Any, session_id: str) -> dict[str, Any]:
    """End every link of a session minted so far, and audit it: the one revoke behind DELETE /sessions/{id}/share and
    browser.revoke_session_share, each of which has first checked the session (ensure_links_revocable).

    The store is written before the event: a revoke whose event cannot be written raises with the links ended, and
    one cancelled between the two leaves them ended and no event.
    """
    revoked_before = await asyncio.to_thread(share_manager.revoke, session_id)
    await audit.append(
        event_type=SHARE_REVOKED_EVENT,
        status="ok",
        action="revoke_session_share",
        session_id=session_id,
        details={"revoked_before": revoked_before},
    )
    return {"session_id": session_id, "revoked": True, "revoked_before": revoked_before}

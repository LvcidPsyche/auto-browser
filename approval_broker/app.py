"""Fail-closed agent access to a loopback-only Auto Browser controller.

Only this process holds the upstream bearer credential. The owner opens the
one browser session with a fresh phone authenticator code; authenticated agents
may use that exact already-open session until the owner closes it. Run a single
broker worker behind a private owner-controlled ingress.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.asyncio.client import connect as ws_connect

ALLOWED_ACTIONS = frozenset({
    "click", "type", "press", "scroll", "navigate", "wait",
    "hover", "select_option", "reload", "go_back", "go_forward", "upload",
    # Answer the JavaScript dialog (alert/confirm/prompt/leave-page) open on
    # the active tab -- controller POST /sessions/{id}/actions/dialog.
    "dialog",
})
# Controller error codes whose meaning an agent needs to act on (answer the
# dialog, hand a captcha to the owner). Only the code and the dialog's own
# type/text are relayed -- never the rest of the controller's error body.
RELAYED_ERROR_CODES = frozenset({
    "dialog_open", "captcha_detected",
    # A tab-scoped call (X-Tab-Id) whose tab has closed: list/open tabs again.
    "tab_gone",
    # File transfer (controller app/file_transfer.py): what went wrong with a
    # download/upload is something the agent must act on (point at another
    # element, try another site, pick a smaller file).
    "file_bad_request", "file_unsupported_type", "file_too_large", "file_empty", "file_no_download",
    "file_download_failed", "file_download_timeout", "file_source_not_found", "file_fetch_blocked",
    "file_no_input", "file_unknown_transfer", "file_transfer_failed",
})
# Safe, bounded facts a file-transfer refusal may carry back to the agent.
_RELAYED_FILE_FIELDS = {"max_bytes": int, "size_bytes": int, "kind": str, "reason": str}
# A tab id the controller hands out (list_tabs / open_tab). Sent upstream as
# the X-Tab-Id header so the call runs in that tab only -- see SessionLock.
TAB_ID_PATTERN = re.compile(r"^t-[0-9a-f]{12}$")
# Who a tab belongs to (an employee label such as "emad").
TAB_OWNER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# The controller's REST paths use a dash for a couple of these operations while every
# tool-facing name in this broker (and the MCP tool list) uses an underscore -- translate
# here so ALLOWED_ACTIONS can keep the MCP-friendly spelling everywhere else.
_ACTION_PATH_OVERRIDES = {
    "select_option": "select-option",
    "go_back": "go-back",
    "go_forward": "go-forward",
}
# Tab visibility/switching/opening lives at the controller's own REST paths (/tabs,
# /tabs/activate, /tabs/open), not the generic /sessions/{id}/actions/{operation} used by
# ALLOWED_ACTIONS, so `operate()` special-cases them the same way it does "observe".
# Without these an agent can only ever see/act on whichever single tab the controller
# happens to be tracking -- including a tab it never opened and the owner may have
# switched away from -- with no way to find or reach any other open tab, or to open a link
# in a new one. Closing a tab is deliberately NOT here -- only the owner manages that.
TAB_OPERATIONS = frozenset({"list_tabs", "activate_tab", "open_tab"})
# Moving files between the owner's browser and an agent (controller
# app/file_transfer.py). download_file / upload_file are ordinary grant-bound
# operations; the bytes themselves travel over the streaming REST routes
# GET/PUT/DELETE /requests/{grant}/files[/{transfer}] -- never through JSON.
FILE_OPERATIONS = frozenset({"download_file", "upload_file"})
TRANSFER_MIME_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/avif",
    "video/mp4", "video/quicktime", "video/webm",
    "audio/mpeg", "audio/aac", "audio/mp4", "audio/wav", "audio/ogg", "audio/flac",
    "application/pdf",
})
DEFAULT_TRANSFER_MAX_BYTES = 200 * 1024 * 1024
_TRANSFER_ID_RE = re.compile(r"[0-9a-f]{24}")
TOTP_PERIOD = 30
TOTP_FAILURE_LIMIT = 5
TOTP_BLOCK_SECONDS = 300


def _relayed_error_detail(response: httpx.Response) -> Any:
    generic = "Browser controller rejected request"
    try:
        body = response.json()
    except ValueError:
        return generic
    if not isinstance(body, dict) or body.get("code") not in RELAYED_ERROR_CODES:
        return generic
    detail: dict[str, Any] = {"message": generic, "code": body["code"]}
    for key, kind in _RELAYED_FILE_FIELDS.items():
        value = body.get(key)
        if isinstance(value, kind) and not isinstance(value, bool):
            detail[key] = value[:60] if isinstance(value, str) else value
    dialog = body.get("dialog")
    if isinstance(dialog, dict):
        detail["dialog"] = {
            "type": str(dialog.get("type") or "")[:20],
            "message": str(dialog.get("message") or "")[:500],
        }
    return detail


class SessionLock:
    """Reader/writer lock; ``async with lock:`` is exclusive (asyncio.Lock
    compatible), ``async with lock.shared():`` may be held by many at once.

    Writer preference: once an exclusive acquirer is queued, new shared
    acquirers wait behind it, so an owner close/revoke is never starved by
    a stream of agent calls. Waiters are served in arrival order, and a
    cancelled waiter leaves the counters exact. (A copy of the controller's
    app/browser/session_lock.py -- the broker is a separate container.)
    """

    def __init__(self) -> None:
        self._readers = 0
        self._writer = False
        self._waiters: collections.deque[tuple[bool, asyncio.Future[bool]]] = collections.deque()

    def locked(self) -> bool:
        return self._writer or self._readers > 0

    async def acquire(self) -> bool:
        if not self._writer and self._readers == 0 and not self._pending():
            self._writer = True
            return True
        await self._wait(exclusive=True)
        return True

    def release(self) -> None:
        if not self._writer:
            raise RuntimeError("SessionLock is not held exclusively")
        self._writer = False
        self._wake()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *exc_info: object) -> None:
        self.release()

    async def acquire_shared(self) -> None:
        if not self._writer and not self._pending():
            self._readers += 1
            return
        await self._wait(exclusive=False)

    def release_shared(self) -> None:
        if self._readers <= 0:
            raise RuntimeError("SessionLock is not held shared")
        self._readers -= 1
        self._wake()

    @asynccontextmanager
    async def shared(self):
        await self.acquire_shared()
        try:
            yield
        finally:
            self.release_shared()

    def _pending(self) -> bool:
        return any(not fut.done() for _exclusive, fut in self._waiters)

    async def _wait(self, *, exclusive: bool) -> None:
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        entry = (exclusive, fut)
        self._waiters.append(entry)
        try:
            await fut
        except BaseException:
            try:
                self._waiters.remove(entry)
            except ValueError:
                pass
            if fut.done() and not fut.cancelled():
                # Granted, then cancelled before resuming: give it back.
                if exclusive:
                    self._writer = False
                else:
                    self._readers -= 1
            self._wake()
            raise

    def _wake(self) -> None:
        while self._waiters:
            exclusive, fut = self._waiters[0]
            if fut.done():
                self._waiters.popleft()
                continue
            if exclusive:
                if self._writer or self._readers:
                    return
                self._waiters.popleft()
                self._writer = True
                fut.set_result(True)
                return
            if self._writer:
                return
            self._waiters.popleft()
            self._readers += 1
            fut.set_result(True)


def _pop_tab_id(arguments: dict[str, Any]) -> dict[str, str]:
    """Take an optional ``tab_id`` out of the tool arguments -> upstream headers."""
    if "tab_id" not in arguments:
        return {}
    tab_id = arguments.pop("tab_id")
    if not isinstance(tab_id, str) or not TAB_ID_PATTERN.fullmatch(tab_id):
        raise HTTPException(400, "Invalid tab_id")
    return {"X-Tab-Id": tab_id}


def totp_code(secret: str, step: int) -> str:
    key = base64.b32decode(secret, casefold=True)
    digest = hmac.new(key, step.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = int.from_bytes(digest[offset:offset + 4], "big") & 0x7fffffff
    return f"{number % 1_000_000:06d}"


class TotpStore:
    """Single-owner TOTP state; SQLite transactions serialize replay checks."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_absolute() or not self.path.parent.is_dir() or self.path.is_symlink():
            raise ValueError("TOTP database requires an existing private directory and absolute path")
        if os.name == "posix" and self.path.parent.stat().st_mode & 0o077:
            raise ValueError("TOTP database directory must be private (0700)")
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        if not self.path.is_file():
            raise ValueError("TOTP database must be a regular file")
        if os.name == "posix" and self.path.stat().st_mode & 0o077:
            raise ValueError("TOTP database must be private (0600)")
        try:
            with closing(self.connect()) as db:
                db.execute("""CREATE TABLE IF NOT EXISTS owner_totp (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    secret TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    last_step INTEGER NOT NULL DEFAULT -1,
                    failures INTEGER NOT NULL DEFAULT 0,
                    blocked_until REAL NOT NULL DEFAULT 0
                )""")
        except sqlite3.Error as exc:
            raise ValueError("TOTP database unavailable") from exc

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def status(self) -> str:
        try:
            with closing(self.connect()) as db:
                row = db.execute("SELECT active FROM owner_totp WHERE id=1").fetchone()
            return "active" if row and row[0] else "pending" if row else "unconfigured"
        except sqlite3.Error:
            raise HTTPException(503, "Authenticator state unavailable") from None

    def begin_setup(self) -> str:
        try:
            with closing(self.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT secret, active FROM owner_totp WHERE id=1").fetchone()
                if row and row[1]:
                    raise HTTPException(409, "Authenticator already active")
                if row:
                    secret = row[0]
                else:
                    secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
                    db.execute("INSERT INTO owner_totp (id, secret) VALUES (1, ?)", (secret,))
                db.commit()
            return secret
        except sqlite3.Error:
            raise HTTPException(503, "Authenticator state unavailable") from None

    def verify(self, code: str, *, activate: bool = False) -> None:
        try:
            with closing(self.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT secret, active, last_step, failures, blocked_until FROM owner_totp WHERE id=1"
                ).fetchone()
                if not row or bool(row[1]) == activate:
                    raise HTTPException(403, "Authenticator not ready")
                secret, _, last_step, failures, blocked_until = row
                now = time.time()
                if now < blocked_until:
                    raise HTTPException(429, "Too many authenticator attempts; try later")
                current_step = int(now // TOTP_PERIOD)
                matched_step = next((step for step in range(current_step - 1, current_step + 2)
                                     if step > last_step and hmac.compare_digest(totp_code(secret, step), code)), None)
                if matched_step is None:
                    failures += 1
                    blocked_until = now + TOTP_BLOCK_SECONDS if failures >= TOTP_FAILURE_LIMIT else 0
                    db.execute("UPDATE owner_totp SET failures=?, blocked_until=? WHERE id=1",
                               (failures, blocked_until))
                    db.commit()
                    raise HTTPException(429 if blocked_until else 403, "Invalid or previously used authenticator code")
                db.execute("UPDATE owner_totp SET active=?, last_step=?, failures=0, blocked_until=0 WHERE id=1",
                           (1 if activate else 1, matched_step))
                db.commit()
        except sqlite3.Error:
            raise HTTPException(503, "Authenticator state unavailable") from None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AccessRequest(StrictModel):
    purpose: str = Field(min_length=1, max_length=500)


class OwnerSessionRequest(StrictModel):
    start_url: str = Field(min_length=1, max_length=2000, pattern=r"^https?://")
    auth_profile: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")
    totp_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    portal_assertion: str | None = Field(default=None, min_length=40, max_length=4096)


class TotpProof(StrictModel):
    totp_code: str = Field(pattern=r"^[0-9]{6}$")


class OwnerSaveProfileRequest(StrictModel):
    profile_name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")


class OwnerRenameProfileRequest(StrictModel):
    new_name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")


class OwnerTypeRequest(StrictModel):
    text: str = Field(min_length=1, max_length=2000)


class OwnerActivateTabRequest(StrictModel):
    index: int = Field(ge=0, le=500)


class Action(StrictModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpCall(StrictModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpRequest(StrictModel):
    jsonrpc: Literal["2.0"]
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


@dataclass
class Grant:
    id: str
    agent_id: str
    purpose: str
    requested_at: float
    session_id: str
    # Controller ids are not guaranteed to be globally unique across browser
    # lifetimes.  Bind to this broker's opening generation as well as the id.
    session_generation: int
    status: Literal["approved", "denied", "revoked", "completed"] = "approved"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "purpose": self.purpose,
            "requested_at": self.requested_at,
            "status": self.status,
            "session_id": self.session_id,
        }


def create_app(
    *,
    owner_token: str,
    agent_token: str | None = None,
    agent_tokens: str | None = None,
    upstream_token: str,
    upstream_url: str = "http://127.0.0.1:8000",
    novnc_url: str = "http://browser-node:6080",
    transport: httpx.AsyncBaseTransport | None = None,
    novnc_transport: httpx.AsyncBaseTransport | None = None,
    totp_db_path: str | Path | None = None,
    portal_url: str | None = None,
    portal_assertion_public_key: str | None = None,
    expected_user_id: str | None = None,
    expected_tenant_id: str | None = None,
    assertion_clock_skew_seconds: int = 5,
    transfer_max_bytes: int = DEFAULT_TRANSFER_MAX_BYTES,
) -> FastAPI:
    if totp_db_path is None:
        raise ValueError("BROKER_TOTP_DB is required")
    totp = TotpStore(totp_db_path)
    assertion_key: Ed25519PublicKey | None = None
    if portal_assertion_public_key is not None:
        if not expected_user_id or not expected_tenant_id:
            raise ValueError("Assertion-enabled broker requires immutable user and tenant ids")
        try:
            raw_key = base64.urlsafe_b64decode(
                portal_assertion_public_key + "=" * (-len(portal_assertion_public_key) % 4)
            )
            assertion_key = Ed25519PublicKey.from_public_bytes(raw_key)
        except (ValueError, TypeError):
            raise ValueError("BROKER_PORTAL_ASSERTION_PUBLIC_KEY must be a base64url Ed25519 key") from None
        if assertion_clock_skew_seconds < 0 or assertion_clock_skew_seconds > 30:
            raise ValueError("Assertion clock skew must be between 0 and 30 seconds")
        with closing(totp.connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS portal_assertion_replay (
                jti_hash TEXT PRIMARY KEY,
                expires_at REAL NOT NULL
            )""")
    named_agents: dict[str, str] = {}
    if agent_token:
        named_agents["agent"] = agent_token
    if agent_tokens:
        for entry in agent_tokens.split(","):
            agent_id, separator, token = entry.strip().partition(":")
            if not separator or not agent_id or not token or not all(
                character.isalnum() or character in "_-" for character in agent_id
            ):
                raise ValueError("Invalid named agent credential")
            if agent_id in named_agents:
                raise ValueError("Duplicate agent identity")
            named_agents[agent_id] = token
    if not named_agents:
        raise ValueError("At least one agent credential is required")
    all_tokens = [owner_token, upstream_token, *named_agents.values()]
    if min(map(len, all_tokens)) < 32:
        raise ValueError("All bearer credentials must have at least 32 characters")
    if len(set(all_tokens)) != len(all_tokens):
        raise ValueError("Owner, agents, and upstream credentials must be distinct")
    if upstream_url not in {"http://127.0.0.1:8000", "http://controller:8000"}:
        raise ValueError("Upstream must be the private controller endpoint")
    if novnc_url not in {"http://127.0.0.1:6080", "http://browser-node:6080"}:
        raise ValueError("noVNC upstream must be the private browser-node endpoint")
    if portal_url is not None:
        try:
            parsed_portal = urlsplit(portal_url)
            port = parsed_portal.port
        except ValueError as exc:
            raise ValueError("BROKER_PORTAL_URL must be a public HTTPS origin") from exc
        if (len(portal_url) > 2000 or parsed_portal.scheme != "https" or not parsed_portal.hostname
                or parsed_portal.username is not None or parsed_portal.password is not None
                or parsed_portal.query or parsed_portal.fragment or parsed_portal.path not in {"", "/"}):
            raise ValueError("BROKER_PORTAL_URL must be a public HTTPS origin")
        host = parsed_portal.hostname.lower()
        portal_url = f"https://{'[' + host + ']' if ':' in host else host}" + (f":{port}" if port is not None else "")

    if not 1 <= transfer_max_bytes <= 2 * 1024 * 1024 * 1024:
        raise ValueError("Transfer size cap must be between 1 byte and 2 GiB")
    grants: dict[str, Grant] = {}
    # (session id, session generation, transfer id) -> agent that created it.
    # A transfer is only ever readable/attachable by the agent whose grant made
    # it, and only while that owner session is still the live one.
    transfer_owners: dict[tuple[str, int, str], str] = {}
    # An owner revocation is an agent-wide decision for this one browser
    # lifetime, not merely a state change on the request id they clicked.
    blocked_agents: set[tuple[str, int]] = set()
    # This is deliberately process-local.  After a broker restart, an existing
    # controller session is not trusted as owner-unlocked until the owner opens
    # a new one with TOTP.
    owner_session_id: str | None = None
    owner_session_generation = 0
    session_opening = False
    session_creation_lock = asyncio.Lock()
    # Exclusive (``async with session_state_lock``) for every change to the
    # owner-session / grant state; SHARED for agent operations -- see operate().
    session_state_lock = SessionLock()
    client = httpx.AsyncClient(
        base_url=upstream_url,
        transport=transport,
        headers={"Authorization": f"Bearer {upstream_token}", "X-Operator-Id": "owner"},
        # Above the controller's own hard ceilings on one browser call
        # (BROWSER_CALL_TIMEOUT_SECONDS 30 / BROWSER_ACTION_TIMEOUT_SECONDS 90),
        # so a slow-but-legitimate navigation is not reported as a 502 while
        # it is still running and holding the session.
        timeout=100,
        follow_redirects=False,
    )
    # No fixed Authorization header: the noVNC static file server has no
    # concept of the owner bearer at all, unlike the controller above.
    novnc_client = httpx.AsyncClient(base_url=novnc_url, transport=novnc_transport, timeout=20, follow_redirects=False)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop = asyncio.Event()

        try:
            yield
        finally:
            stop.set()
            await client.aclose()
            await novnc_client.aclose()

    app = FastAPI(
        title="Auto Browser approval broker", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    @app.get("/owner", include_in_schema=False)
    async def owner_page():
        return FileResponse(
            Path(__file__).with_name("owner.html"),
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/owner.js", include_in_schema=False)
    async def owner_script():
        return FileResponse(
            Path(__file__).with_name("owner.js"), media_type="text/javascript",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    def role(authorization: str | None) -> tuple[str, str]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer credential required")
        supplied = authorization[7:]
        if secrets.compare_digest(supplied, owner_token):
            return "owner", "owner"
        for agent_id, token in named_agents.items():
            if secrets.compare_digest(supplied, token):
                return "agent", agent_id
        raise HTTPException(401, "Invalid bearer credential")

    def require_role(authorization: str | None, expected: str) -> str:
        actual_role, identity = role(authorization)
        if actual_role != expected:
            raise HTTPException(403, "Wrong credential role")
        return identity

    def get_grant(grant_id: str) -> Grant:
        grant = grants.get(grant_id)
        if grant is None:
            raise HTTPException(404, "Unknown request")
        return grant

    def agent_grant(grant_id: str, authorization: str | None) -> Grant:
        agent_id = require_role(authorization, "agent")
        grant = get_grant(grant_id)
        if grant.agent_id != agent_id:
            raise HTTPException(404, "Unknown request")
        return grant

    def revoke_owner_session(session_id: str, generation: int) -> None:
        for candidate in grants.values():
            if candidate.session_id == session_id and candidate.session_generation == generation:
                candidate.status = "revoked"

    def verify_portal_assertion(assertion: str) -> None:
        """Verify a short-lived, user-scoped, single-use portal assertion."""
        if assertion_key is None:
            raise HTTPException(403, "Portal assertion is not configured")
        encoded_payload, separator, encoded_signature = assertion.partition(".")
        if not separator or "." in encoded_signature:
            raise HTTPException(403, "Invalid portal assertion")
        try:
            payload_bytes = base64.urlsafe_b64decode(
                encoded_payload + "=" * (-len(encoded_payload) % 4)
            )
            signature = base64.urlsafe_b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4)
            )
            assertion_key.verify(signature, encoded_payload.encode("ascii"))
            claims = json.loads(payload_bytes)
        except (InvalidSignature, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise HTTPException(403, "Invalid portal assertion") from None
        now = time.time()
        if (
            not isinstance(claims, dict)
            or claims.get("sub") != expected_user_id
            or claims.get("tenant") != expected_tenant_id
            or claims.get("purpose") != "browser_open"
            or not isinstance(claims.get("iat"), (int, float))
            or not isinstance(claims.get("exp"), (int, float))
            or not isinstance(claims.get("jti"), str)
            or not 16 <= len(claims["jti"]) <= 200
            or claims["iat"] > now + assertion_clock_skew_seconds
            or claims["exp"] <= now
            or claims["exp"] > claims["iat"] + 120
        ):
            raise HTTPException(403, "Expired or wrongly scoped portal assertion")
        replay_key = hashlib.sha256(claims["jti"].encode("utf-8")).hexdigest()
        try:
            with closing(totp.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM portal_assertion_replay WHERE expires_at<?", (now,))
                if db.execute(
                    "SELECT 1 FROM portal_assertion_replay WHERE jti_hash=?", (replay_key,)
                ).fetchone():
                    raise HTTPException(403, "Portal assertion was already used")
                db.execute(
                    "INSERT INTO portal_assertion_replay(jti_hash,expires_at) VALUES(?,?)",
                    (replay_key, claims["exp"] + assertion_clock_skew_seconds),
                )
                db.commit()
        except sqlite3.Error:
            raise HTTPException(503, "Assertion replay state unavailable") from None

    async def ensure_live(grant: Grant) -> None:
        if grant.status != "approved":
            raise HTTPException(403, "Agent grant is not active")
        if (grant.agent_id, grant.session_generation) in blocked_agents:
            grant.status = "revoked"
            raise HTTPException(403, "Agent access was revoked for this session")
        if (owner_session_id is None or grant.session_id != owner_session_id
                or grant.session_generation != owner_session_generation):
            grant.status = "revoked"
            raise HTTPException(403, "Owner browser session is not available")
        sessions = await upstream("GET", "/sessions")
        if not isinstance(sessions, list) or not any(
            isinstance(item, dict) and item.get("id") == owner_session_id and item.get("status") == "active"
            for item in sessions
        ):
            grant.status = "revoked"
            raise HTTPException(403, "Owner browser session is closed")

    async def upstream(
        method: str, path: str, payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = await client.request(method, path, json=payload, headers=headers)
        except httpx.HTTPError:
            raise HTTPException(502, "Browser controller unavailable") from None
        if response.status_code >= 400:
            raise HTTPException(response.status_code, _relayed_error_detail(response))
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise HTTPException(502, "Invalid browser controller response") from None

    controller_request = upstream

    def safe_session_id(session_id: str) -> str:
        if not session_id or len(session_id) > 120 or not all(
            character.isalnum() or character in "_-" for character in session_id
        ):
            raise HTTPException(400, "Invalid session id")
        return session_id

    def safe_profile_name(profile_name: str) -> str:
        if not profile_name or len(profile_name) > 120 or not all(
            character.isalnum() or character in "_.-" for character in profile_name
        ):
            raise HTTPException(400, "Invalid profile name")
        return profile_name

    async def ensure_no_active_session() -> None:
        sessions = await upstream("GET", "/sessions")
        if not isinstance(sessions, list) or not all(isinstance(item, dict) for item in sessions):
            raise HTTPException(502, "Invalid browser controller response")
        if any(item.get("status") == "active" for item in sessions):
            raise HTTPException(409, "Close the current session first")

    async def trusted_owner_session() -> str:
        nonlocal owner_session_id
        if owner_session_id is None:
            raise HTTPException(403, "Owner must open a verified browser session first")
        sessions = await upstream("GET", "/sessions")
        if not isinstance(sessions, list) or not any(
            isinstance(item, dict) and item.get("id") == owner_session_id and item.get("status") == "active"
            for item in sessions
        ):
            revoke_owner_session(owner_session_id, owner_session_generation)
            owner_session_id = None
            raise HTTPException(403, "Verified owner browser session is closed")
        return owner_session_id

    @app.get("/owner/totp")
    async def owner_totp_status(authorization: str | None = Header(default=None)):
        require_role(authorization, "owner")
        return {"status": totp.status()}

    @app.post("/owner/totp/setup")
    async def owner_totp_setup(response: Response, authorization: str | None = Header(default=None)):
        require_role(authorization, "owner")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        secret = totp.begin_setup()
        return {"secret": secret,
                "provisioning_uri": f"otpauth://totp/Auto%20Browser:Owner?secret={secret}&issuer=Auto%20Browser&algorithm=SHA1&digits=6&period=30"}

    @app.post("/owner/totp/activate")
    async def owner_totp_activate(payload: TotpProof, authorization: str | None = Header(default=None)):
        require_role(authorization, "owner")
        totp.verify(payload.totp_code, activate=True)
        return {"status": "active"}

    @app.get("/owner/sessions")
    async def owner_sessions(authorization: str | None = Header(default=None)):
        require_role(authorization, "owner")
        sessions = await upstream("GET", "/sessions")
        if not isinstance(sessions, list):
            raise HTTPException(502, "Invalid browser controller response")
        return sessions

    @app.get("/owner/visual-access")
    async def owner_visual_access(authorization: str | None = Header(default=None)):
        """Private gateway guard: noVNC is usable only for the TOTP-opened session."""
        require_role(authorization, "owner")
        # Shared side: a read-only check the portal runs for every view poll;
        # it must not queue behind (or hold up) the employees' calls.
        async with session_state_lock.shared():
            return {"session_id": await trusted_owner_session()}

    _NOVNC_DROP_REQUEST_HEADERS = {
        "host", "connection", "upgrade", "authorization", "content-length", "cookie",
    }
    _NOVNC_KEEP_RESPONSE_HEADERS = {"content-type", "content-length", "etag", "last-modified", "cache-control"}

    @app.api_route("/owner/vnc/{path:path}", methods=["GET", "HEAD"])
    async def owner_vnc_static(path: str, request: Request, authorization: str | None = Header(default=None)):
        """Proxy the private noVNC static files -- only browser-node can reach

        this on the tenant-private network, so only this broker (which sits on
        both the tenant network and the portal-reachable control network) can
        serve them onward, and only once a TOTP-opened owner session exists.
        """
        require_role(authorization, "owner")
        async with session_state_lock.shared():
            await trusted_owner_session()
        if ".." in path or path.startswith("/") or not re.fullmatch(r"[A-Za-z0-9._/-]*", path):
            raise HTTPException(404, "Not found")
        headers = {key: value for key, value in request.headers.items() if key.lower() not in _NOVNC_DROP_REQUEST_HEADERS}
        try:
            response = await novnc_client.request(
                request.method, f"/{path}", params=request.query_params, headers=headers, timeout=10,
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Browser view is unavailable") from None
        response_headers = {
            key: value for key, value in response.headers.items() if key.lower() in _NOVNC_KEEP_RESPONSE_HEADERS
        }
        return Response(
            content=response.content if request.method != "HEAD" else b"",
            status_code=response.status_code, headers=response_headers,
        )

    @app.websocket("/owner/vnc/websockify")
    async def owner_vnc_websocket(websocket: WebSocket):
        auth_header = websocket.headers.get("authorization") or ""
        supplied = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        if not supplied or not secrets.compare_digest(supplied, owner_token):
            await websocket.close(code=1008)
            return
        async with session_state_lock.shared():
            try:
                session_id = await trusted_owner_session()
            except HTTPException:
                await websocket.close(code=1008)
                return

        async def still_live() -> bool:
            # Re-verify against the controller's live session list every time,
            # exactly like an agent's ensure_live() check -- comparing only the
            # cached owner_session_id would miss the owner's session ending
            # for any reason other than an explicit close through this broker.
            #
            # Deliberately NOT under session_state_lock: operate() holds (the
            # shared side of) that lock for a whole controller call, so every
            # frame of the owner's live view used to wait behind it -- one slow
            # or hung browser call froze the view and dropped the noVNC socket
            # (2026-09-25 04:01:51). Reading owner_session_id needs no lock.
            if owner_session_id != session_id:
                return False
            try:
                # `upstream` is shadowed below by the noVNC socket of that name.
                sessions = await controller_request("GET", "/sessions")
            except HTTPException:
                return False
            return owner_session_id == session_id and isinstance(sessions, list) and any(
                isinstance(item, dict) and item.get("id") == session_id and item.get("status") == "active"
                for item in sessions
            )

        ws_url = "ws" + novnc_url.removeprefix("http") + "/websockify"
        try:
            async with ws_connect(ws_url, max_size=16 * 1024 * 1024, open_timeout=5) as upstream:
                if not await still_live():
                    await websocket.close(code=1008)
                    return
                await websocket.accept()

                async def browser_to_vnc() -> None:
                    try:
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                break
                            if not await still_live():
                                break
                            if message.get("bytes") is not None:
                                await upstream.send(message["bytes"])
                            elif message.get("text") is not None:
                                await upstream.send(message["text"])
                    except (WebSocketDisconnect, RuntimeError):
                        pass

                async def vnc_to_browser() -> None:
                    async for message in upstream:
                        if not await still_live():
                            break
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                async def periodic_guard() -> None:
                    while True:
                        await asyncio.sleep(1)
                        if not await still_live():
                            break

                tasks = [
                    asyncio.create_task(browser_to_vnc()),
                    asyncio.create_task(vnc_to_browser()),
                    asyncio.create_task(periodic_guard()),
                ]
                _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if websocket.application_state != WebSocketState.DISCONNECTED:
                    await websocket.close(code=1008)
        except Exception:
            if websocket.application_state != WebSocketState.DISCONNECTED:
                await websocket.close(code=1011)

    @app.post("/owner/sessions")
    async def owner_create_session(payload: OwnerSessionRequest, authorization: str | None = Header(default=None)):
        nonlocal owner_session_id, owner_session_generation, session_opening
        require_role(authorization, "owner")
        async with session_creation_lock:
            async with session_state_lock:
                session_opening = True
                try:
                    await ensure_no_active_session()
                    if assertion_key is not None:
                        if payload.totp_code is not None or payload.portal_assertion is None:
                            raise HTTPException(403, "A portal assertion is required")
                        verify_portal_assertion(payload.portal_assertion)
                    else:
                        if payload.portal_assertion is not None or payload.totp_code is None:
                            raise HTTPException(403, "A fresh authenticator code is required")
                        totp.verify(payload.totp_code)
                    session_payload = {"name": "owner-login", "start_url": payload.start_url}
                    if payload.auth_profile is not None:
                        session_payload["auth_profile"] = payload.auth_profile
                    result = await upstream("POST", "/sessions", session_payload)
                    if not isinstance(result, dict) or not isinstance(result.get("id"), str):
                        raise HTTPException(502, "Controller did not return a session id")
                    owner_session_id = result["id"]
                    owner_session_generation += 1
                finally:
                    session_opening = False
        return {**result, "owner_takeover_url": "http://127.0.0.1:16080/vnc.html?autoconnect=true&resize=scale"}

    @app.get("/owner/auth-profiles")
    async def owner_auth_profiles(authorization: str | None = Header(default=None)):
        require_role(authorization, "owner")
        profiles = await upstream("GET", "/auth-profiles")
        if not isinstance(profiles, list):
            raise HTTPException(502, "Invalid browser controller response")
        return profiles

    @app.post("/owner/sessions/{session_id}/auth-profiles")
    async def owner_save_profile(
        session_id: str, payload: OwnerSaveProfileRequest, authorization: str | None = Header(default=None),
    ):
        require_role(authorization, "owner")
        session_id = safe_session_id(session_id)
        return await upstream("POST", f"/sessions/{session_id}/auth-profiles", {"profile_name": payload.profile_name})

    @app.delete("/owner/auth-profiles/{profile_name}")
    async def owner_delete_profile(profile_name: str, authorization: str | None = Header(default=None)):
        require_role(authorization, "owner")
        profile_name = safe_profile_name(profile_name)
        return await upstream("DELETE", f"/auth-profiles/{profile_name}")

    @app.post("/owner/auth-profiles/{profile_name}/rename")
    async def owner_rename_profile(
        profile_name: str, payload: OwnerRenameProfileRequest, authorization: str | None = Header(default=None),
    ):
        require_role(authorization, "owner")
        profile_name = safe_profile_name(profile_name)
        return await upstream("POST", f"/auth-profiles/{profile_name}/rename", {"new_name": payload.new_name})

    @app.post("/owner/sessions/{session_id}/type")
    async def owner_type(
        session_id: str, payload: OwnerTypeRequest, authorization: str | None = Header(default=None),
    ):
        """Insert text into whatever is focused in the owner's live session.

        Backs the "type here" box next to the noVNC viewer: the owner clicks a
        field through the VNC mouse, then sends text here instead of through
        the VNC keyboard channel. Delivered to the controller over CDP, this
        never touches X11 keysyms, which is what makes it work for Arabic (and
        any other non-Latin script) typed on a phone, unlike the viewer's raw
        VNC keyboard input.
        """
        require_role(authorization, "owner")
        session_id = safe_session_id(session_id)
        return await upstream("POST", f"/sessions/{session_id}/actions/type-focused", {"text": payload.text})

    async def owner_trusted_session(session_id: str) -> str:
        session_id = safe_session_id(session_id)
        # Shared side: the owner's tab strip polls this every few seconds and
        # must never queue behind (or hold up) the employees' calls.
        async with session_state_lock.shared():
            trusted = await trusted_owner_session()
        if session_id != trusted:
            raise HTTPException(403, "Not the verified owner browser session")
        return session_id

    @app.get("/owner/sessions/{session_id}/tabs")
    async def owner_list_tabs(session_id: str, authorization: str | None = Header(default=None)):
        """Every tab of the owner's session: who opened it and which one is shown."""
        require_role(authorization, "owner")
        session_id = await owner_trusted_session(session_id)
        tabs = await upstream("GET", f"/sessions/{session_id}/tabs")
        if not isinstance(tabs, list):
            raise HTTPException(502, "Invalid browser controller response")
        return tabs

    @app.post("/owner/sessions/{session_id}/tabs/activate")
    async def owner_activate_tab(
        session_id: str, payload: OwnerActivateTabRequest, authorization: str | None = Header(default=None),
    ):
        """Show that tab in the owner's live view -- owner-initiated only."""
        require_role(authorization, "owner")
        session_id = await owner_trusted_session(session_id)
        return await upstream("POST", f"/sessions/{session_id}/tabs/activate", {"index": payload.index})

    @app.delete("/owner/sessions/{session_id}")
    async def owner_close_session(session_id: str, authorization: str | None = Header(default=None)):
        nonlocal owner_session_id
        require_role(authorization, "owner")
        session_id = safe_session_id(session_id)
        async with session_state_lock:
            result = await upstream("DELETE", f"/sessions/{session_id}")
            revoke_owner_session(session_id, owner_session_generation)
            if owner_session_id == session_id:
                owner_session_id = None
        return result

    async def operate(grant: Grant, operation: str, arguments: dict[str, Any]) -> Any:
        # Agents no longer queue behind each other: every operation holds the
        # SHARED side of session_state_lock for its whole controller call, so
        # several employees' calls (each in its own tab) run at once. Every
        # state change -- owner close/open, revoke, deny, complete,
        # request_access, session_status -- takes the EXCLUSIVE side, so it
        # still waits for every in-flight action and no later action can start
        # first (writer preference): the guarantee "owner close/revoke waits
        # for this action and nothing starts after it" is unchanged.
        # grant.lock guards only this grant's status check, not the I/O.
        async with session_state_lock.shared():
            async with grant.lock:
                await ensure_live(grant)
            method, path, body, headers = operation_request(grant.session_id, operation, dict(arguments))
            return await upstream(method, path, body, headers=headers or None)

    def operation_request(
        session_id: str, operation: str, arguments: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any] | None, dict[str, str]]:
        if operation == "observe":
            headers = _pop_tab_id(arguments)
            if arguments:
                raise HTTPException(400, "Observation options are not exposed")
            return "GET", f"/sessions/{session_id}/observe", None, headers
        if operation == "find_api_keys":
            headers = _pop_tab_id(arguments)
            provider = arguments.get("provider")
            if set(arguments) != {"provider"} or provider not in {"google"}:
                raise HTTPException(400, "find_api_keys requires provider 'google'")
            return "GET", f"/sessions/{session_id}/api-keys?provider={provider}", None, headers
        if operation == "list_tabs":
            if arguments:
                raise HTTPException(400, "list_tabs takes no arguments")
            return "GET", f"/sessions/{session_id}/tabs", None, {}
        if operation == "activate_tab":
            index = arguments.get("index")
            if set(arguments) != {"index"} or not isinstance(index, int) or isinstance(index, bool):
                raise HTTPException(400, "activate_tab requires an integer 'index'")
            return "POST", f"/sessions/{session_id}/tabs/activate", arguments, {}
        if operation == "open_tab":
            if set(arguments) - {"url", "activate", "owner"}:
                raise HTTPException(400, "open_tab takes only 'url', 'activate' and 'owner'")
            url = arguments.get("url")
            activate = arguments.get("activate", True)
            owner = arguments.get("owner")
            if url is not None and not isinstance(url, str):
                raise HTTPException(400, "open_tab 'url' must be a string")
            if not isinstance(activate, bool):
                raise HTTPException(400, "open_tab 'activate' must be a boolean")
            body: dict[str, Any] = {"url": url, "activate": activate}
            if owner is not None:
                if not isinstance(owner, str) or not TAB_OWNER_PATTERN.fullmatch(owner):
                    raise HTTPException(400, "open_tab 'owner' must be a short lowercase label")
                body["owner"] = owner
            return "POST", f"/sessions/{session_id}/tabs/open", body, {}
        if operation in FILE_OPERATIONS:
            raise HTTPException(500, "File operations are not session-locked")
        if operation in ALLOWED_ACTIONS:
            headers = _pop_tab_id(arguments)
            if "approval_id" in arguments:
                raise HTTPException(400, "Built-in sensitive approvals are owner-only")
            path = _ACTION_PATH_OVERRIDES.get(operation, operation)
            return "POST", f"/sessions/{session_id}/actions/{path}", arguments, headers
        raise HTTPException(404, "Tool unavailable")

    _DOWNLOAD_KEYS = {"mode", "element_id", "selector", "url", "media_kind", "timeout_seconds", "pace"}
    _ATTACH_KEYS = {"transfer_id", "element_id", "selector"}

    def owned_transfer(grant: Grant, transfer_id: Any) -> str:
        if not isinstance(transfer_id, str) or not _TRANSFER_ID_RE.fullmatch(transfer_id):
            raise HTTPException(404, "Unknown transfer")
        if transfer_owners.get((grant.session_id, grant.session_generation, transfer_id)) != grant.agent_id:
            raise HTTPException(404, "Unknown transfer")
        return transfer_id

    def remember_transfer(grant: Grant, result: Any) -> None:
        transfer_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(transfer_id, str) or not _TRANSFER_ID_RE.fullmatch(transfer_id):
            raise HTTPException(502, "Invalid browser controller response")
        transfer_owners[(grant.session_id, grant.session_generation, transfer_id)] = grant.agent_id
        while len(transfer_owners) > 500:
            transfer_owners.pop(next(iter(transfer_owners)))

    async def live_file_grant(grant: Grant) -> None:
        # Checked under the state lock like every other operation, but the
        # transfer itself runs outside it: a download may wait minutes for a
        # site to render a video, and nothing else -- the owner closing his
        # browser included -- may queue behind that.
        # The SHARED side, like operate(): a check must not queue behind (or
        # hold up) the other employees' in-flight calls.
        async with session_state_lock.shared():
            async with grant.lock:
                await ensure_live(grant)

    async def file_operation(grant: Grant, operation: str, arguments: dict[str, Any]) -> Any:
        await live_file_grant(grant)
        arguments = dict(arguments)
        # Optional tab_id: the download/attach acts on that employee's tab.
        tab_headers = _pop_tab_id(arguments)
        if operation == "download_file":
            if set(arguments) - _DOWNLOAD_KEYS or not isinstance(arguments.get("mode"), str):
                raise HTTPException(400, "download_file takes mode and element_id/selector/url/media_kind/timeout_seconds/pace")
            wait = arguments.get("timeout_seconds", 120)
            if not isinstance(wait, (int, float)) or isinstance(wait, bool) or not 0 < wait <= 600:
                raise HTTPException(400, "timeout_seconds must be a number up to 600")
            async with grant.lock:
                try:
                    response = await client.post(
                        f"/sessions/{grant.session_id}/files/download", json=arguments,
                        headers=tab_headers or None,
                        timeout=httpx.Timeout(float(wait) + 40, connect=10),
                    )
                except httpx.HTTPError:
                    raise HTTPException(502, "Browser controller unavailable") from None
            if response.status_code >= 400:
                raise HTTPException(response.status_code, _relayed_error_detail(response))
            try:
                result = response.json()
            except ValueError:
                raise HTTPException(502, "Invalid browser controller response") from None
            remember_transfer(grant, result)
            return result
        if operation == "upload_file":
            if set(arguments) - _ATTACH_KEYS:
                raise HTTPException(400, "upload_file takes transfer_id and element_id/selector")
            transfer_id = owned_transfer(grant, arguments.get("transfer_id"))
            body = {key: arguments[key] for key in ("element_id", "selector") if key in arguments}
            async with grant.lock:
                return await upstream(
                    "POST", f"/sessions/{grant.session_id}/files/{transfer_id}/attach", body,
                    headers=tab_headers or None,
                )
        raise HTTPException(404, "Tool unavailable")

    async def revoke_locked(
        grant: Grant,
        *,
        status: Literal["denied", "revoked", "completed"],
        only_if_approved: bool = False,
    ) -> dict[str, Any]:
        async with grant.lock:
            if only_if_approved and grant.status != "approved":
                raise HTTPException(409, "Request is not actively approved")
            grant.status = status
            return grant.public()

    async def revoke(
        grant: Grant,
        *,
        status: Literal["denied", "revoked", "completed"],
        only_if_approved: bool = False,
    ) -> dict[str, Any]:
        async with session_state_lock:
            return await revoke_locked(grant, status=status, only_if_approved=only_if_approved)

    @app.post("/requests", status_code=202)
    async def request_access(payload: AccessRequest, authorization: str | None = Header(default=None)):
        agent_id = require_role(authorization, "agent")
        # A grant can only attach to the process-local, TOTP-opened owner
        # session.  Never discover or adopt a controller session here.
        async with session_state_lock:
            if owner_session_id is None:
                raise HTTPException(403, "Owner must open a browser session first")
            sessions = await upstream("GET", "/sessions")
            if not isinstance(sessions, list) or not any(
                isinstance(item, dict) and item.get("id") == owner_session_id and item.get("status") == "active"
                for item in sessions
            ):
                raise HTTPException(403, "Owner browser session is closed")
            if (agent_id, owner_session_generation) in blocked_agents:
                raise HTTPException(403, "Agent access is revoked for this owner browser session")
        # A connected agent receives one grant per owner-opened session.  A
        # repeated request is a retry/task change, not a new owner approval.
            for existing in grants.values():
                if (existing.agent_id == agent_id and existing.session_id == owner_session_id
                        and existing.session_generation == owner_session_generation
                        and existing.status == "approved"):
                    return existing.public()
        # Prevent one bearer holder from exhausting process memory with an
        # unbounded request queue. Old terminal entries are not security state.
            if len(grants) >= 100:
                for key, old in tuple(grants.items()):
                    if old.status in {"denied", "revoked", "completed"}:
                        grants.pop(key)
                    if len(grants) < 100:
                        break
            if len(grants) >= 100:
                raise HTTPException(429, "Approval queue full")
            grant = Grant(id=secrets.token_urlsafe(18), agent_id=agent_id, purpose=payload.purpose,
                          requested_at=time.time(), session_id=owner_session_id,
                          session_generation=owner_session_generation)
            grants[grant.id] = grant
            return grant.public()

    @app.get("/requests")
    async def list_requests(authorization: str | None = Header(default=None)):
        require_role(authorization, "owner")
        return [grant.public() for grant in grants.values()]

    @app.get("/requests/{grant_id}")
    async def get_request(grant_id: str, authorization: str | None = Header(default=None)):
        grant = agent_grant(grant_id, authorization)
        return grant.public()

    @app.post("/requests/{grant_id}/deny")
    async def deny(grant_id: str, authorization: str | None = Header(default=None)):
        require_role(authorization, "owner")
        async with session_state_lock:
            grant = get_grant(grant_id)
            blocked_agents.add((grant.agent_id, grant.session_generation))
            for candidate in grants.values():
                if candidate.agent_id == grant.agent_id and candidate.session_generation == grant.session_generation and candidate.status == "approved":
                    candidate.status = "revoked"
            return await revoke_locked(grant, status="denied")

    @app.post("/requests/{grant_id}/revoke")
    async def revoke_request(grant_id: str, authorization: str | None = Header(default=None)):
        require_role(authorization, "owner")
        async with session_state_lock:
            grant = get_grant(grant_id)
            blocked_agents.add((grant.agent_id, grant.session_generation))
            for candidate in grants.values():
                if candidate.agent_id == grant.agent_id and candidate.session_generation == grant.session_generation and candidate.status == "approved":
                    candidate.status = "revoked"
            return await revoke_locked(grant, status="revoked")

    @app.post("/requests/{grant_id}/complete")
    async def complete_request(grant_id: str, authorization: str | None = Header(default=None)):
        """Let the approved requesting agent explicitly end its own work."""
        return await revoke(agent_grant(grant_id, authorization), status="completed", only_if_approved=True)

    @app.get("/requests/{grant_id}/observe")
    async def observe(grant_id: str, authorization: str | None = Header(default=None)):
        return await operate(agent_grant(grant_id, authorization), "observe", {})

    @app.post("/requests/{grant_id}/actions/{action_name}")
    async def action(grant_id: str, action_name: str, payload: Action, authorization: str | None = Header(default=None)):
        grant = agent_grant(grant_id, authorization)
        if action_name in FILE_OPERATIONS:
            return await file_operation(grant, action_name, payload.arguments)
        return await operate(grant, action_name, payload.arguments)

    @app.get("/requests/{grant_id}/files/{transfer_id}")
    async def pull_file(grant_id: str, transfer_id: str, authorization: str | None = Header(default=None)):
        """Stream a transfer this agent created (download_file) back to it."""
        grant = agent_grant(grant_id, authorization)
        await live_file_grant(grant)
        owned_transfer(grant, transfer_id)
        request = client.build_request(
            "GET", f"/sessions/{grant.session_id}/files/{transfer_id}", timeout=httpx.Timeout(120, connect=10),
        )
        try:
            response = await client.send(request, stream=True)
        except httpx.HTTPError:
            raise HTTPException(502, "Browser controller unavailable") from None
        if response.status_code >= 400:
            await response.aread()
            await response.aclose()
            raise HTTPException(response.status_code, _relayed_error_detail(response))
        media_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        length = response.headers.get("content-length") or ""
        if media_type not in TRANSFER_MIME_TYPES or not length.isdigit() or int(length) > transfer_max_bytes:
            await response.aclose()
            raise HTTPException(502, "Browser controller returned an unexpected file")
        expected = int(length)

        async def relay():
            sent = 0
            try:
                async for chunk in response.aiter_bytes():
                    sent += len(chunk)
                    if sent > expected:
                        break
                    yield chunk
            finally:
                await response.aclose()

        headers = {
            "Content-Length": length,
            "Content-Disposition": "attachment",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        }
        digest = response.headers.get("x-transfer-sha256") or ""
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            headers["X-Transfer-Sha256"] = digest
        return StreamingResponse(relay(), media_type=media_type, headers=headers)

    class _PushTooLarge(Exception):
        pass

    @app.put("/requests/{grant_id}/files")
    async def push_file(grant_id: str, request: Request, authorization: str | None = Header(default=None)):
        """Receive one file from the agent (streamed, never buffered) and park
        it in the owner session for upload_file to set on the page."""
        grant = agent_grant(grant_id, authorization)
        media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if media_type not in TRANSFER_MIME_TYPES:
            raise HTTPException(415, "Only images, video, audio and PDF files can be uploaded")
        length = request.headers.get("content-length") or ""
        if not length.isdigit():
            raise HTTPException(411, "Content-Length required")
        declared = int(length)
        if declared == 0:
            raise HTTPException(400, "Empty file")
        if declared > transfer_max_bytes:
            raise HTTPException(413, {"message": "File too large", "code": "file_too_large",
                                      "max_bytes": transfer_max_bytes})
        raw_name = request.headers.get("x-file-name") or ""
        if len(raw_name) > 600 or not re.fullmatch(r"[A-Za-z0-9%._~!$&'()*+,;=@-]*", raw_name):
            raise HTTPException(400, "X-File-Name must be percent-encoded")
        await live_file_grant(grant)

        async def forward():
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > declared:
                    raise _PushTooLarge()
                yield chunk

        async with grant.lock:
            try:
                response = await client.put(
                    f"/sessions/{grant.session_id}/files", content=forward(),
                    headers={"Content-Type": media_type, "Content-Length": length, "X-File-Name": raw_name},
                    timeout=httpx.Timeout(180, connect=10),
                )
            except _PushTooLarge:
                raise HTTPException(413, "Body larger than its Content-Length") from None
            except httpx.HTTPError:
                raise HTTPException(502, "Browser controller unavailable") from None
        if response.status_code >= 400:
            raise HTTPException(response.status_code, _relayed_error_detail(response))
        try:
            result = response.json()
        except ValueError:
            raise HTTPException(502, "Invalid browser controller response") from None
        remember_transfer(grant, result)
        return result

    @app.delete("/requests/{grant_id}/files/{transfer_id}")
    async def drop_file(grant_id: str, transfer_id: str, authorization: str | None = Header(default=None)):
        grant = agent_grant(grant_id, authorization)
        await live_file_grant(grant)
        owned_transfer(grant, transfer_id)
        result = await upstream("DELETE", f"/sessions/{grant.session_id}/files/{transfer_id}")
        transfer_owners.pop((grant.session_id, grant.session_generation, transfer_id), None)
        return result

    @app.get("/mcp/tools")
    async def list_tools(authorization: str | None = Header(default=None)):
        require_role(authorization, "agent")
        return [{"name": f"browser.{name}"} for name in ("session_status", "request_access", "get_request", "complete", "observe", "find_api_keys", *sorted(TAB_OPERATIONS), *sorted(FILE_OPERATIONS), *sorted(ALLOWED_ACTIONS))]

    async def session_status() -> dict[str, str]:
        """Safe agent setup signal; deliberately unrelated to TOTP state."""
        def response(status: str) -> dict[str, str]:
            result = {"status": status}
            if portal_url is not None:
                result["portal_url"] = portal_url
            return result

        async with session_state_lock:
            if session_opening:
                return response("pending")
            if owner_session_id is None:
                return response("absent")
            try:
                await trusted_owner_session()
            except HTTPException as exc:
                if exc.status_code == 403:
                    return response("absent")
                raise
            return response("ready")

    @app.post("/mcp/tools/call")
    async def call_tool(payload: McpCall, authorization: str | None = Header(default=None)):
        require_role(authorization, "agent")
        arguments = dict(payload.arguments)
        if payload.name == "browser.session_status":
            if arguments:
                raise HTTPException(400, "Status options are not exposed")
            return await session_status()
        if payload.name == "browser.request_access":
            try:
                request = AccessRequest.model_validate(arguments)
            except ValidationError:
                raise HTTPException(422, "Invalid access request") from None
            return await request_access(request, authorization)
        grant_id = arguments.pop("request_id", None)
        if not isinstance(grant_id, str):
            raise HTTPException(400, "request_id required")
        if not payload.name.startswith("browser."):
            raise HTTPException(404, "Tool unavailable")
        if payload.name == "browser.complete":
            if arguments:
                raise HTTPException(400, "Completion options are not exposed")
            return await revoke(agent_grant(grant_id, authorization), status="completed", only_if_approved=True)
        operation = payload.name.removeprefix("browser.")
        if operation in FILE_OPERATIONS:
            return await file_operation(agent_grant(grant_id, authorization), operation, arguments)
        return await operate(agent_grant(grant_id, authorization), operation, arguments)

    @app.post("/mcp")
    async def mcp(payload: McpRequest, authorization: str | None = Header(default=None)):
        require_role(authorization, "agent")
        if payload.method == "notifications/initialized":
            from fastapi import Response

            return Response(status_code=202)
        if payload.method == "initialize":
            return {
                "jsonrpc": "2.0", "id": payload.id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "auto-browser-approval-broker", "version": "0.1.0"},
                },
            }
        if payload.method == "tools/list":
            names = ("session_status", "request_access", "get_request", "complete", "observe", "find_api_keys", *sorted(TAB_OPERATIONS), *sorted(FILE_OPERATIONS), *sorted(ALLOWED_ACTIONS))
            return {
                "jsonrpc": "2.0", "id": payload.id,
                "result": {"tools": [
                    {
                        "name": f"browser.{name}",
                        "description": "Requires a current grant bound to the owner-opened browser session; no other browser API is exposed.",
                        "inputSchema": {
                            "type": "object",
                            "properties": (
                                {} if name == "session_status" else {"purpose": {"type": "string"}}
                                if name == "request_access" else
                                {"request_id": {"type": "string"}, "arguments": {"type": "object"}}
                            ),
                            "required": ([] if name == "session_status" else ["purpose"] if name == "request_access" else ["request_id"]),
                            "additionalProperties": False,
                        },
                    }
                    for name in names
                ]},
            }
        if payload.method == "tools/call":
            name = payload.params.get("name")
            arguments = payload.params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise HTTPException(400, "Invalid tool call")
            if name == "browser.session_status":
                if arguments:
                    raise HTTPException(400, "Status options are not exposed")
                result = await session_status()
            elif name == "browser.request_access":
                try:
                    request = AccessRequest.model_validate(arguments)
                except ValidationError:
                    raise HTTPException(422, "Invalid access request") from None
                result = await request_access(request, authorization)
            elif name == "browser.get_request":
                if set(arguments) != {"request_id"} or not isinstance(arguments["request_id"], str):
                    raise HTTPException(400, "request_id required")
                result = await get_request(arguments["request_id"], authorization)
            elif name == "browser.complete":
                if set(arguments) != {"request_id"} or not isinstance(arguments["request_id"], str):
                    raise HTTPException(400, "request_id required")
                result = await revoke(
                    agent_grant(arguments["request_id"], authorization), status="completed", only_if_approved=True,
                )
            else:
                if set(arguments) - {"request_id", "arguments"}:
                    raise HTTPException(400, "Unsupported tool arguments")
                request_id = arguments.get("request_id")
                action_arguments = arguments.get("arguments", {})
                if not isinstance(request_id, str) or not isinstance(action_arguments, dict):
                    raise HTTPException(400, "request_id and arguments required")
                if not name.startswith("browser."):
                    raise HTTPException(404, "Tool unavailable")
                operation = name.removeprefix("browser.")
                if operation in FILE_OPERATIONS:
                    result = await file_operation(agent_grant(request_id, authorization), operation, action_arguments)
                else:
                    result = await operate(agent_grant(request_id, authorization), operation, action_arguments)
            import json

            return {
                "jsonrpc": "2.0", "id": payload.id,
                "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
            }
        raise HTTPException(404, "MCP method unavailable")

    return app


def app_from_environment() -> FastAPI:
    return create_app(
        owner_token=os.environ["BROKER_OWNER_TOKEN"],
        agent_token=os.environ.get("BROKER_AGENT_TOKEN"),
        agent_tokens=os.environ.get("BROKER_AGENT_TOKENS"),
        upstream_token=os.environ["API_BEARER_TOKEN"],
        upstream_url=os.environ.get("BROKER_UPSTREAM_URL", "http://127.0.0.1:8000"),
        novnc_url=os.environ.get("BROKER_NOVNC_URL", "http://browser-node:6080"),
        totp_db_path=os.environ["BROKER_TOTP_DB"],
        portal_url=os.environ.get("BROKER_PORTAL_URL"),
        portal_assertion_public_key=os.environ.get("BROKER_PORTAL_ASSERTION_PUBLIC_KEY"),
        expected_user_id=os.environ.get("BROKER_USER_ID"),
        expected_tenant_id=os.environ.get("BROKER_TENANT_ID"),
        transfer_max_bytes=int(os.environ.get("BROKER_TRANSFER_MAX_BYTES") or DEFAULT_TRANSFER_MAX_BYTES),
    )


app = app_from_environment() if os.environ.get("BROKER_OWNER_TOKEN") else None

"""Fail-closed agent access to a loopback-only Auto Browser controller.

Only this process holds the upstream bearer credential. The owner opens the
one browser session with a fresh phone authenticator code; authenticated agents
may use that exact already-open session until the owner closes it. Run a single
broker worker behind a private owner-controlled ingress.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ALLOWED_ACTIONS = frozenset({"click", "type", "press", "scroll", "navigate", "wait"})
TOTP_PERIOD = 30
TOTP_FAILURE_LIMIT = 5
TOTP_BLOCK_SECONDS = 300


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
    totp_code: str = Field(pattern=r"^[0-9]{6}$")


class TotpProof(StrictModel):
    totp_code: str = Field(pattern=r"^[0-9]{6}$")


class OwnerSaveProfileRequest(StrictModel):
    profile_name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")


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
    transport: httpx.AsyncBaseTransport | None = None,
    totp_db_path: str | Path | None = None,
    portal_url: str | None = None,
) -> FastAPI:
    if totp_db_path is None:
        raise ValueError("BROKER_TOTP_DB is required")
    totp = TotpStore(totp_db_path)
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

    grants: dict[str, Grant] = {}
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
    session_state_lock = asyncio.Lock()
    client = httpx.AsyncClient(
        base_url=upstream_url,
        transport=transport,
        headers={"Authorization": f"Bearer {upstream_token}", "X-Operator-Id": "owner"},
        timeout=20,
        follow_redirects=False,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop = asyncio.Event()

        try:
            yield
        finally:
            stop.set()
            await client.aclose()

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

    async def upstream(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            response = await client.request(method, path, json=payload)
        except httpx.HTTPError:
            raise HTTPException(502, "Browser controller unavailable") from None
        if response.status_code >= 400:
            raise HTTPException(response.status_code, "Browser controller rejected request")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise HTTPException(502, "Invalid browser controller response") from None

    def safe_session_id(session_id: str) -> str:
        if not session_id or len(session_id) > 120 or not all(
            character.isalnum() or character in "_-" for character in session_id
        ):
            raise HTTPException(400, "Invalid session id")
        return session_id

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
        async with session_state_lock:
            return {"session_id": await trusted_owner_session()}

    @app.post("/owner/sessions")
    async def owner_create_session(payload: OwnerSessionRequest, authorization: str | None = Header(default=None)):
        nonlocal owner_session_id, owner_session_generation, session_opening
        require_role(authorization, "owner")
        async with session_creation_lock:
            async with session_state_lock:
                session_opening = True
                try:
                    await ensure_no_active_session()
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
        # Keep the state lock through controller I/O.  Owner close/revoke then
        # waits for this action and no later action can start first.
        async with session_state_lock:
            async with grant.lock:
                await ensure_live(grant)
                if operation == "observe":
                    if arguments:
                        raise HTTPException(400, "Observation options are not exposed")
                    return await upstream("GET", f"/sessions/{grant.session_id}/observe")
                if operation in ALLOWED_ACTIONS:
                    if "approval_id" in arguments:
                        raise HTTPException(400, "Built-in sensitive approvals are owner-only")
                    return await upstream("POST", f"/sessions/{grant.session_id}/actions/{operation}", arguments)
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
        return await operate(agent_grant(grant_id, authorization), action_name, payload.arguments)

    @app.get("/mcp/tools")
    async def list_tools(authorization: str | None = Header(default=None)):
        require_role(authorization, "agent")
        return [{"name": f"browser.{name}"} for name in ("session_status", "request_access", "get_request", "complete", "observe", *sorted(ALLOWED_ACTIONS))]

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
        return await operate(agent_grant(grant_id, authorization), payload.name.removeprefix("browser."), arguments)

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
            names = ("session_status", "request_access", "get_request", "complete", "observe", *sorted(ALLOWED_ACTIONS))
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
                result = await operate(agent_grant(request_id, authorization), name.removeprefix("browser."), action_arguments)
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
        totp_db_path=os.environ["BROKER_TOTP_DB"],
        portal_url=os.environ.get("BROKER_PORTAL_URL"),
    )


app = app_from_environment() if os.environ.get("BROKER_OWNER_TOKEN") else None

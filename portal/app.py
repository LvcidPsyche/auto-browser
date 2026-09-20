"""Secure human portal for identity, browser lifecycle, and OAuth consent.

The portal deliberately exposes a small product API.  It is not a proxy for
the broker, controller, VNC server, or OAuth gateway.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from approval_broker.app import TOTP_PERIOD, totp_code

SESSION_COOKIE = "ab_portal_session"
CSRF_COOKIE = "ab_portal_csrf"
TOTP_PATTERN = re.compile(r"^[0-9]{6}$")

# Keep upstream contracts in one place.  They can be adapted without changing
# the portal's public routes or weakening its proxy boundary.
IDENTITY_VERIFY_PATH = "/internal/auth/verify"
IDENTITY_INVITATION_REDEEM_PATH = "/invitations/redeem"
IDENTITY_ENROLLMENT_CONFIRM_PATH = "/enrollments/confirm"
IDENTITY_RECOVERY_BEGIN_PATH = "/internal/auth/recover"
IDENTITY_RECOVERY_CONFIRM_PATH = IDENTITY_ENROLLMENT_CONFIRM_PATH
IDENTITY_RECOVERY_CODES_PATH = "/internal/auth/recovery-codes"
BROKER_OPEN_PATH = "/owner/sessions"
BROKER_CLOSE_PREFIX = "/owner/sessions/"
GATEWAY_CONNECTIONS_PATH = "/internal/connected-clients"
GATEWAY_ACTIVE_USER_PATH = "/internal/active-user"
GATEWAY_CONSENT_PREVIEW_PATH = "/internal/consent/preview"
GATEWAY_CONSENT_PATH = "/internal/consent"
SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("public_origin must be an HTTPS origin")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("public_origin must be an HTTPS origin")
    return f"https://{parsed.netloc}"


def _credential(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) < 32:
        raise ValueError(f"{name} must have at least 32 characters")
    return value


def _safe_next(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2500:
        return None
    if value == "/browser" or value.startswith("/oauth/authorize?authorization_request="):
        return value
    return None


def _totp_secret(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z2-7]+", value):
        raise ValueError("broker_totp_secret must be a valid private base32 secret")
    normalized = value.upper()
    try:
        decoded = base64.b32decode(normalized + "=" * ((-len(normalized)) % 8))
    except (ValueError, TypeError):
        raise ValueError("broker_totp_secret must be a valid private base32 secret") from None
    if len(decoded) < 20:
        raise ValueError("broker_totp_secret must contain at least 160 bits")
    return normalized


class PortalStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).absolute()
        if self.root.exists() and (not self.root.is_dir() or self.root.is_symlink()):
            raise ValueError("Portal state root must be a regular directory")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / "portal.sqlite3"
        if self.path.exists() and (not self.path.is_file() or self.path.is_symlink()):
            raise ValueError("Portal database must be a regular file")
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        with closing(self.connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS portal_sessions (
                    token_hash TEXT PRIMARY KEY,
                    csrf_hash TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    account TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    absolute_expires_at REAL NOT NULL,
                    idle_expires_at REAL NOT NULL,
                    revoked_at REAL
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS portal_sessions_user_idx
                    ON portal_sessions(user_id, tenant_id);
                CREATE TABLE IF NOT EXISTS browser_ownership (
                    slot INTEGER PRIMARY KEY CHECK(slot = 1),
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    broker_session_id TEXT,
                    claimed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_totp_state (
                    slot INTEGER PRIMARY KEY CHECK(slot = 1),
                    last_step INTEGER NOT NULL
                );
                """
            )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def create_session(
        self, *, user_id: str, tenant_id: str, account: str, now: float,
        absolute_ttl: int, idle_ttl: int,
    ) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        absolute = now + absolute_ttl
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            # A successful login rotates all portal sessions for this immutable
            # identity.  Stolen older cookies cannot survive a fresh login.
            db.execute(
                "UPDATE portal_sessions SET revoked_at=? "
                "WHERE user_id=? AND tenant_id=? AND revoked_at IS NULL",
                (now, user_id, tenant_id),
            )
            db.execute(
                """INSERT INTO portal_sessions
                   (token_hash, csrf_hash, user_id, tenant_id, account, created_at,
                    last_seen_at, absolute_expires_at, idle_expires_at, revoked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (_digest(token), _digest(csrf), user_id, tenant_id, account, now,
                 now, absolute, min(absolute, now + idle_ttl)),
            )
            db.commit()
        return token, csrf

    def session(self, token: str, *, now: float, idle_ttl: int) -> sqlite3.Row | None:
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM portal_sessions WHERE token_hash=?", (_digest(token),)
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                db.commit()
                return None
            if row["absolute_expires_at"] <= now or row["idle_expires_at"] <= now:
                db.execute(
                    "UPDATE portal_sessions SET revoked_at=? WHERE token_hash=?",
                    (now, _digest(token)),
                )
                db.commit()
                return None
            db.execute(
                """UPDATE portal_sessions SET last_seen_at=?, idle_expires_at=?
                   WHERE token_hash=?""",
                (now, min(row["absolute_expires_at"], now + idle_ttl), _digest(token)),
            )
            db.commit()
            return row

    def revoke(self, token: str, *, now: float) -> None:
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE portal_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (now, _digest(token)),
            )


async def _payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        try:
            value = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(value, dict):
            raise HTTPException(422, "Object required")
        return value
    if content_type == "application/x-www-form-urlencoded":
        # Avoid a multipart dependency for the intentionally small HTML forms.
        from urllib.parse import parse_qs
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        return {key: entries[-1] for key, entries in values.items()}
    raise HTTPException(415, "Use JSON or URL-encoded form data")


def _required_text(data: Mapping[str, Any], key: str, maximum: int = 512) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise HTTPException(422, f"Invalid {key}")
    return value.strip()


def _totp(data: Mapping[str, Any]) -> str:
    value = data.get("totp_code")
    if not isinstance(value, str) or not TOTP_PATTERN.fullmatch(value):
        raise HTTPException(422, "A 6-digit authenticator code is required")
    return value


def _upstream_error(response: httpx.Response, fallback: str) -> HTTPException:
    # Upstream bodies can contain credentials or operational details, so the
    # portal never reflects them.  Preserve only a useful client status class.
    status = response.status_code if 400 <= response.status_code < 500 else 502
    return HTTPException(status, fallback)


def _identity(identity: Mapping[str, Any], account: str) -> tuple[str, str, str]:
    user_id, tenant_id = identity.get("user_id"), identity.get("tenant_id")
    if not isinstance(user_id, str) or not user_id or len(user_id) > 200:
        raise HTTPException(502, "Identity response missing user binding")
    if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > 200:
        raise HTTPException(502, "Identity response missing tenant binding")
    canonical = identity.get("account", account)
    if not isinstance(canonical, str) or not canonical or len(canonical) > 320:
        canonical = account
    return user_id, tenant_id, canonical


def _safe_enrollment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(502, "Invalid identity response")
    allowed = {
        "status", "enrollment_token", "enrollment_id", "recovery_token",
        "user_id", "tenant_id", "account", "account_id", "display_name",
        "secret", "totp_secret", "otpauth_uri", "provisioning_uri", "recovery_codes", "expires_at",
    }
    return {key: item for key, item in value.items() if key in allowed}


def _safe_connection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "connection_ref", "client_id", "client_name", "application_type",
        "provider", "capabilities", "created_at", "last_used_at",
    }
    return {key: item for key, item in value.items() if key in allowed}


def create_app(
    *,
    state_root: str | Path,
    identity_internal_token: str,
    broker_owner_token: str,
    broker_totp_secret: str,
    gateway_internal_token: str,
    public_origin: str,
    identity_base_url: str = "http://identity",
    broker_base_url: str = "http://approval-broker:18001",
    gateway_base_url: str = "http://oauth-gateway",
    identity_client: httpx.AsyncClient | None = None,
    broker_client: httpx.AsyncClient | None = None,
    gateway_client: httpx.AsyncClient | None = None,
    identity_transport: httpx.AsyncBaseTransport | None = None,
    broker_transport: httpx.AsyncBaseTransport | None = None,
    gateway_transport: httpx.AsyncBaseTransport | None = None,
    absolute_session_ttl: int = 12 * 60 * 60,
    idle_session_ttl: int = 30 * 60,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """Build the portal with injectable upstream clients or transports."""
    public_origin = _origin(public_origin)
    identity_internal_token = _credential(identity_internal_token, "identity_internal_token")
    broker_owner_token = _credential(broker_owner_token, "broker_owner_token")
    broker_totp_secret = _totp_secret(broker_totp_secret)
    gateway_internal_token = _credential(gateway_internal_token, "gateway_internal_token")
    if absolute_session_ttl <= 0 or idle_session_ttl <= 0:
        raise ValueError("Session expiry settings must be positive")
    if identity_client is not None and identity_transport is not None:
        raise ValueError("Pass an identity client or transport, not both")
    if broker_client is not None and broker_transport is not None:
        raise ValueError("Pass a broker client or transport, not both")
    if gateway_client is not None and gateway_transport is not None:
        raise ValueError("Pass a gateway client or transport, not both")

    owned: list[httpx.AsyncClient] = []

    def client(existing: httpx.AsyncClient | None, base: str, transport: httpx.AsyncBaseTransport | None):
        if existing is not None:
            return existing
        made = httpx.AsyncClient(base_url=base, transport=transport, timeout=10.0)
        owned.append(made)
        return made

    identity_http = client(identity_client, identity_base_url, identity_transport)
    broker_http = client(broker_client, broker_base_url, broker_transport)
    gateway_http = client(gateway_client, gateway_base_url, gateway_transport)
    store = PortalStore(state_root)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        for item in owned:
            await item.aclose()

    app = FastAPI(title="Auto Browser portal", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.portal_store = store

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers[key] = value
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    def session_for(request: Request) -> sqlite3.Row:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(401, "Sign in required")
        row = store.session(token, now=clock(), idle_ttl=idle_session_ttl)
        if row is None:
            raise HTTPException(401, "Session expired or revoked")
        return row

    async def mutation(request: Request, *, require_session: bool = True) -> sqlite3.Row | None:
        origin = request.headers.get("origin")
        if origin != public_origin:
            raise HTTPException(403, "Invalid request origin")
        if not require_session:
            return None
        row = session_for(request)
        data = await _payload(request)
        supplied = request.headers.get("x-csrf-token") or data.get("csrf_token")
        cookie = request.cookies.get(CSRF_COOKIE)
        if not isinstance(supplied, str) or not isinstance(cookie, str):
            raise HTTPException(403, "CSRF validation failed")
        if not secrets.compare_digest(supplied.encode(), cookie.encode()):
            raise HTTPException(403, "CSRF validation failed")
        if not secrets.compare_digest(_digest(supplied).encode(), row["csrf_hash"].encode()):
            raise HTTPException(403, "CSRF validation failed")
        return row

    def internal_headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @app.get("/healthz")
    async def health():
        with closing(store.connect()) as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/")
    @app.get("/signin")
    async def sign_in_page(request: Request):
        next_path = _safe_next(request.query_params.get("next"))
        next_field = (
            f"<input type=hidden name=next value='{html.escape(next_path, quote=True)}'>" if next_path else ""
        )
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Secure Browser</title>"
            "<h1>Sign in</h1><form method=post action=/signin>" + next_field +
            "<label>Account <input name=account autocomplete=username required></label>"
            "<label>Authenticator code <input name=totp_code inputmode=numeric "
            "autocomplete=one-time-code pattern='[0-9]{6}' required></label>"
            "<button>Sign in</button></form>"
        )

    @app.post("/signin")
    async def sign_in(request: Request):
        await mutation(request, require_session=False)
        data = await _payload(request)
        account, code = _required_text(data, "account", 320), _totp(data)
        try:
            response = await identity_http.post(
                IDENTITY_VERIFY_PATH,
                headers=internal_headers(identity_internal_token),
                json={"account": account, "totp_code": code, "purpose": "portal_login"},
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Identity service unavailable") from None
        if response.status_code != 200:
            raise _upstream_error(response, "Authentication failed")
        user_id, tenant_id, canonical = _identity(response.json(), account)
        token, csrf = store.create_session(
            user_id=user_id, tenant_id=tenant_id, account=canonical, now=clock(),
            absolute_ttl=absolute_session_ttl, idle_ttl=idle_session_ttl,
        )
        next_path = _safe_next(data.get("next"))
        result = (
            RedirectResponse(next_path, status_code=303)
            if next_path
            else JSONResponse({"status": "signed_in", "user_id": user_id, "tenant_id": tenant_id})
        )
        result.set_cookie(SESSION_COOKIE, token, httponly=True, secure=True, samesite="lax", path="/")
        result.set_cookie(CSRF_COOKIE, csrf, httponly=False, secure=True, samesite="lax", path="/")
        return result

    @app.post("/logout")
    async def logout(request: Request):
        await mutation(request)
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            store.revoke(token, now=clock())
        result = JSONResponse({"status": "signed_out"})
        result.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
        result.delete_cookie(CSRF_COOKIE, secure=True, samesite="lax", path="/")
        return result

    @app.get("/api/session")
    async def session_state(request: Request):
        row = session_for(request)
        with closing(store.connect()) as db:
            owner = db.execute("SELECT * FROM browser_ownership WHERE slot=1").fetchone()
        browser = "closed"
        if owner is not None:
            browser = "open" if owner["broker_session_id"] else "opening"
            if owner["user_id"] != row["user_id"] or owner["tenant_id"] != row["tenant_id"]:
                browser = "unavailable"
        return {
            "authenticated": True, "user_id": row["user_id"], "tenant_id": row["tenant_id"],
            "account": row["account"], "browser": browser,
        }

    @app.get("/browser")
    async def browser_page(request: Request):
        try:
            row = session_for(request)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            return RedirectResponse("/signin?next=%2Fbrowser", status_code=303)
        with closing(store.connect()) as db:
            owner = db.execute("SELECT * FROM browser_ownership WHERE slot=1").fetchone()
        state = "closed"
        if owner is not None:
            if owner["user_id"] != row["user_id"] or owner["tenant_id"] != row["tenant_id"]:
                state = "unavailable"
            else:
                state = "open" if owner["broker_session_id"] else "opening"
        csrf = html.escape(request.cookies.get(CSRF_COOKIE, ""), quote=True)
        account = html.escape(row["account"])
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Secure Browser</title>"
            f"<h1>Secure Browser</h1><p>Signed in as {account}</p><p>Browser: {state}</p>"
            "<h2>Open browser</h2><form method=post action=/api/browser/open>"
            f"<input type=hidden name=csrf_token value='{csrf}'>"
            "<label>Start URL <input name=start_url type=url value='https://example.com' required></label>"
            "<label>Fresh authenticator code <input name=totp_code inputmode=numeric "
            "autocomplete=one-time-code pattern='[0-9]{6}' required></label><button>Open</button></form>"
            "<h2>Close browser</h2><form method=post action=/api/browser/close>"
            f"<input type=hidden name=csrf_token value='{csrf}'><button>Close server session</button></form>"
        )

    @app.post("/api/browser/open")
    async def open_browser(request: Request):
        row = await mutation(request)
        data = await _payload(request)
        code = _totp(data)
        newly_claimed = False
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT * FROM browser_ownership WHERE slot=1").fetchone()
            if owner is not None and (owner["user_id"] != row["user_id"] or owner["tenant_id"] != row["tenant_id"]):
                db.commit()
                raise HTTPException(409, "The shared browser node is in use")
            if owner is not None and owner["broker_session_id"]:
                db.commit()
                raise HTTPException(409, "Browser session is already open")
            if owner is None:
                db.execute(
                    "INSERT INTO browser_ownership(slot,user_id,tenant_id,broker_session_id,claimed_at) VALUES(1,?,?,NULL,?)",
                    (row["user_id"], row["tenant_id"], clock()),
                )
                newly_claimed = True
            db.commit()
        try:
            verified = await identity_http.post(
                IDENTITY_VERIFY_PATH, headers=internal_headers(identity_internal_token),
                json={"account": row["account"], "totp_code": code, "purpose": "browser_open"},
            )
            if verified.status_code != 200:
                raise _upstream_error(verified, "Authenticator verification failed")
            verified_user, verified_tenant, _ = _identity(verified.json(), row["account"])
            if verified_user != row["user_id"] or verified_tenant != row["tenant_id"]:
                raise HTTPException(403, "Identity binding changed")
            # The human code has now been consumed exactly once by identity.
            # The unchanged broker has a separate server-only authenticator;
            # never forward the user's code or expose this derived proof.
            current_step = int(clock() // TOTP_PERIOD)
            with closing(store.connect()) as db:
                totp_state = db.execute("SELECT last_step FROM broker_totp_state WHERE slot=1").fetchone()
            last_broker_step = totp_state["last_step"] if totp_state else -1
            if last_broker_step > current_step:
                raise HTTPException(429, "Wait for a fresh broker authenticator time step")
            broker_step = current_step if last_broker_step < current_step else current_step + 1
            broker_code = totp_code(broker_totp_secret, broker_step)
            if secrets.compare_digest(broker_code, code):
                # Keep the human-entered value single-use even in the rare
                # event that two independent TOTP seeds produce the same six digits.
                if broker_step >= current_step + 1:
                    raise HTTPException(429, "Wait for a fresh broker authenticator time step")
                broker_code = totp_code(broker_totp_secret, broker_step + 1)
                broker_step += 1
            with closing(store.connect()) as db:
                db.execute(
                    "INSERT OR REPLACE INTO broker_totp_state(slot,last_step) VALUES(1,?)",
                    (broker_step,),
                )
            broker_payload: dict[str, Any] = {
                "totp_code": broker_code, "start_url": "https://example.com",
            }
            for key in ("start_url", "auth_profile"):
                if key in data:
                    broker_payload[key] = _required_text(data, key, 2048 if key == "start_url" else 200)
            opened = await broker_http.post(
                BROKER_OPEN_PATH, headers=internal_headers(broker_owner_token), json=broker_payload,
            )
            if opened.status_code not in (200, 201):
                raise _upstream_error(opened, "Browser could not be opened")
            body = opened.json()
            session_id = body.get("id") if isinstance(body, dict) else None
            if (not isinstance(session_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id)):
                raise HTTPException(502, "Broker response missing session id")
            with closing(store.connect()) as db:
                updated = db.execute(
                    """UPDATE browser_ownership SET broker_session_id=?
                       WHERE slot=1 AND user_id=? AND tenant_id=? AND broker_session_id IS NULL""",
                    (session_id, row["user_id"], row["tenant_id"]),
                ).rowcount
            if updated != 1:
                raise HTTPException(409, "Browser ownership changed")
            try:
                active = await gateway_http.post(
                    GATEWAY_ACTIVE_USER_PATH, headers=internal_headers(gateway_internal_token),
                    json={"user_id": row["user_id"], "tenant_id": row["tenant_id"], "active": True},
                )
            except httpx.HTTPError:
                active = None
            if active is None or active.status_code != 200:
                # Do not leave a browser usable by agents if the OAuth gateway
                # could not bind its credentials to this human owner.
                cleanup = await broker_http.delete(
                    BROKER_CLOSE_PREFIX + session_id, headers=internal_headers(broker_owner_token),
                )
                if cleanup.status_code not in (200, 204, 404):
                    raise HTTPException(502, "Gateway binding failed and browser cleanup was not confirmed")
                with closing(store.connect()) as db:
                    db.execute(
                        "DELETE FROM browser_ownership WHERE slot=1 AND user_id=? AND tenant_id=? AND broker_session_id=?",
                        (row["user_id"], row["tenant_id"], session_id),
                    )
                if active is None:
                    raise HTTPException(502, "Browser ownership could not be activated")
                raise _upstream_error(active, "Browser ownership could not be activated")
            return {"status": "open", "session_id": session_id}
        except httpx.HTTPError:
            raise HTTPException(502, "Browser service unavailable") from None
        finally:
            # A failed first attempt must not permanently reserve capacity.  A
            # successful claim has a session id and is unaffected by this.
            if newly_claimed:
                with closing(store.connect()) as db:
                    db.execute(
                        """DELETE FROM browser_ownership WHERE slot=1 AND user_id=? AND tenant_id=?
                           AND broker_session_id IS NULL""",
                        (row["user_id"], row["tenant_id"]),
                    )

    @app.post("/api/browser/close")
    async def close_browser(request: Request):
        row = await mutation(request)
        with closing(store.connect()) as db:
            owner = db.execute("SELECT * FROM browser_ownership WHERE slot=1").fetchone()
        if owner is None:
            return {"status": "closed"}
        if owner["user_id"] != row["user_id"] or owner["tenant_id"] != row["tenant_id"]:
            raise HTTPException(409, "The shared browser node is in use")
        if not owner["broker_session_id"]:
            raise HTTPException(409, "Browser session is still opening")
        try:
            closed = await broker_http.delete(
                BROKER_CLOSE_PREFIX + owner["broker_session_id"],
                headers=internal_headers(broker_owner_token),
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Browser service unavailable") from None
        if closed.status_code not in (200, 204, 404):
            raise _upstream_error(closed, "Browser could not be closed")
        try:
            inactive = await gateway_http.post(
                GATEWAY_ACTIVE_USER_PATH, headers=internal_headers(gateway_internal_token),
                json={"user_id": row["user_id"], "tenant_id": row["tenant_id"], "active": False},
            )
        except httpx.HTTPError:
            inactive = None
        if inactive is None or inactive.status_code not in (200, 404):
            raise HTTPException(502, "Browser closed but gateway state could not be cleared")
        with closing(store.connect()) as db:
            db.execute(
                "DELETE FROM browser_ownership WHERE slot=1 AND user_id=? AND tenant_id=? AND broker_session_id=?",
                (row["user_id"], row["tenant_id"], owner["broker_session_id"]),
            )
        return {"status": "closed"}

    @app.get("/api/connections")
    async def connections(request: Request):
        row = session_for(request)
        try:
            response = await gateway_http.get(
                GATEWAY_CONNECTIONS_PATH, headers=internal_headers(gateway_internal_token),
                params={"user_id": row["user_id"], "tenant_id": row["tenant_id"]},
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Connection service unavailable") from None
        if response.status_code != 200:
            raise _upstream_error(response, "Connections could not be listed")
        body = response.json()
        values = body.get("connections", body) if isinstance(body, (dict, list)) else []
        if not isinstance(values, list):
            raise HTTPException(502, "Invalid connection response")
        return {"connections": [_safe_connection(item) for item in values]}

    @app.post("/api/connections/{connection_id}/disconnect")
    async def disconnect(connection_id: str, request: Request):
        row = await mutation(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", connection_id):
            raise HTTPException(404, "Connection not found")
        try:
            response = await gateway_http.post(
                f"{GATEWAY_CONNECTIONS_PATH}/{connection_id}/disconnect",
                headers=internal_headers(gateway_internal_token),
                json={"user_id": row["user_id"], "tenant_id": row["tenant_id"]},
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Connection service unavailable") from None
        if response.status_code not in (200, 204):
            raise _upstream_error(response, "Connection could not be disconnected")
        return {"status": "disconnected", "connection_id": connection_id}

    @app.get("/invite/{invitation_token}")
    async def invitation_page(invitation_token: str):
        escaped = html.escape(invitation_token, quote=True)
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Enrollment</title><h1>Accept invitation</h1>"
            f"<form method=post action=/api/invitations/redeem><input type=hidden name=invitation_token value='{escaped}'>"
            "<label>Display name <input name=display_name required></label>"
            "<label>Recovery email <input name=recovery_email type=email></label>"
            "<button>Continue</button></form>"
        )

    @app.post("/api/invitations/redeem")
    async def redeem_invitation(request: Request):
        await mutation(request, require_session=False)
        data = await _payload(request)
        token = _required_text(data, "invitation_token")
        display_name = _required_text(data, "display_name", 120)
        identity_payload: dict[str, Any] = {
            "invitation_token": token, "display_name": display_name,
        }
        recovery_email = data.get("recovery_email")
        if recovery_email not in (None, ""):
            if not isinstance(recovery_email, str) or len(recovery_email) > 320:
                raise HTTPException(422, "Invalid recovery_email")
            identity_payload["recovery_email"] = recovery_email.strip()
        response = await identity_http.post(
            IDENTITY_INVITATION_REDEEM_PATH,
            json=identity_payload,
        )
        if response.status_code != 200:
            # Compatibility with the current identity service's email field is
            # intentionally left to its contract adapter, not retried here.
            raise _upstream_error(response, "Invitation could not be redeemed")
        return _safe_enrollment(response.json())

    @app.post("/api/enrollments/confirm")
    async def confirm_enrollment(request: Request):
        await mutation(request, require_session=False)
        data = await _payload(request)
        payload = {
            "enrollment_id": _required_text(data, "enrollment_id"),
            "totp_code": _totp(data),
        }
        response = await identity_http.post(IDENTITY_ENROLLMENT_CONFIRM_PATH, json=payload)
        if response.status_code != 200:
            raise _upstream_error(response, "Enrollment could not be confirmed")
        # Enrollment secrets and recovery codes are returned once, never stored.
        return _safe_enrollment(response.json())

    @app.post("/api/recovery/begin")
    async def recovery_begin(request: Request):
        await mutation(request, require_session=False)
        data = await _payload(request)
        response = await identity_http.post(
            IDENTITY_RECOVERY_BEGIN_PATH, headers=internal_headers(identity_internal_token),
            json={"account": _required_text(data, "account", 320),
                  "recovery_code": _required_text(data, "recovery_code", 200)},
        )
        if response.status_code != 200:
            raise _upstream_error(response, "Recovery failed")
        return _safe_enrollment(response.json())

    @app.post("/api/recovery/confirm")
    async def recovery_confirm(request: Request):
        await mutation(request, require_session=False)
        data = await _payload(request)
        response = await identity_http.post(
            IDENTITY_RECOVERY_CONFIRM_PATH,
            json={"enrollment_id": _required_text(data, "enrollment_id"), "totp_code": _totp(data)},
        )
        if response.status_code != 200:
            raise _upstream_error(response, "Recovery confirmation failed")
        return _safe_enrollment(response.json())

    @app.post("/api/recovery-codes/regenerate")
    async def regenerate_recovery_codes(request: Request):
        row = await mutation(request)
        data = await _payload(request)
        response = await identity_http.post(
            IDENTITY_RECOVERY_CODES_PATH, headers=internal_headers(identity_internal_token),
            json={"account": row["account"], "totp_code": _totp(data),
                  "purpose": "recovery_codes"},
        )
        if response.status_code != 200:
            raise _upstream_error(response, "Recovery codes could not be regenerated")
        return _safe_enrollment(response.json())

    def authorization_request(values: Mapping[str, Any]) -> str:
        value = values.get("authorization_request")
        if not isinstance(value, str) or not (20 <= len(value) <= 200):
            raise HTTPException(422, "Invalid authorization_request")
        return value

    @app.get("/oauth/authorize")
    async def oauth_authorize_page(request: Request):
        try:
            row = session_for(request)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            return RedirectResponse(
                "/signin?next=" + quote(request.url.path + "?" + request.url.query, safe=""),
                status_code=303,
            )
        request_secret = authorization_request(request.query_params)
        response = await gateway_http.post(
            GATEWAY_CONSENT_PREVIEW_PATH, headers=internal_headers(gateway_internal_token),
            json={"authorization_request": request_secret,
                  "user_id": row["user_id"], "tenant_id": row["tenant_id"]},
        )
        if response.status_code != 200:
            raise _upstream_error(response, "Authorization request is invalid")
        preview = response.json()
        name = html.escape(str(preview.get("client_name", "Unknown client")))
        capabilities = preview.get("capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
            raise HTTPException(502, "Invalid consent response")
        csrf = html.escape(request.cookies.get(CSRF_COOKIE, ""), quote=True)
        fields = ("<input type=hidden name=authorization_request value='"
                  + html.escape(request_secret, quote=True) + "'>")
        items = "".join(f"<li>{html.escape(item)}</li>" for item in capabilities)
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Authorize assistant</title>"
            f"<h1>Connect {name}</h1><ul>{items}</ul><form method=post action=/oauth/authorize>"
            f"{fields}<input type=hidden name=csrf_token value='{csrf}'>"
            "<button name=decision value=approve>Allow</button>"
            "<button name=decision value=deny>Deny</button></form>"
        )

    @app.post("/oauth/authorize")
    async def oauth_authorize(request: Request):
        row = await mutation(request)
        data = await _payload(request)
        request_secret = authorization_request(data)
        decision = data.get("decision")
        if decision not in ("approve", "deny"):
            raise HTTPException(422, "Invalid consent decision")
        response = await gateway_http.post(
            GATEWAY_CONSENT_PATH, headers=internal_headers(gateway_internal_token),
            json={"authorization_request": request_secret, "approve": decision == "approve",
                  "user_id": row["user_id"], "tenant_id": row["tenant_id"]},
        )
        if response.status_code != 200:
            raise _upstream_error(response, "Consent could not be recorded")
        value = response.json()
        redirect_url = value.get("redirect_url") if isinstance(value, dict) else None
        if not isinstance(redirect_url, str) or not redirect_url or "\r" in redirect_url or "\n" in redirect_url:
            raise HTTPException(502, "Gateway did not return a validated redirect")
        parsed = urlsplit(redirect_url)
        if not parsed.scheme or not parsed.netloc:
            raise HTTPException(502, "Gateway did not return a validated redirect")
        # Crucially, Location comes only from the gateway response.  The
        # request's redirect_uri is never used as the redirect target here.
        return RedirectResponse(redirect_url, status_code=303)

    return app


def app_from_environment() -> FastAPI:
    return create_app(
        state_root=os.environ["PORTAL_STATE_ROOT"],
        identity_internal_token=os.environ["IDENTITY_INTERNAL_TOKEN"],
        broker_owner_token=os.environ["BROKER_OWNER_TOKEN"],
        broker_totp_secret=os.environ["PORTAL_BROKER_TOTP_SECRET"],
        gateway_internal_token=os.environ["MCP_GATEWAY_INTERNAL_TOKEN"],
        public_origin=os.environ["PORTAL_PUBLIC_ORIGIN"],
        identity_base_url=os.environ.get("PORTAL_IDENTITY_URL", "http://identity"),
        broker_base_url=os.environ.get("PORTAL_BROKER_URL", "http://approval-broker:18001"),
        gateway_base_url=os.environ.get("PORTAL_GATEWAY_URL", "http://mcp-gateway"),
    )


app = app_from_environment() if os.environ.get("PORTAL_STATE_ROOT") else None

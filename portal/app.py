"""Secure human portal for identity, browser lifecycle, and OAuth consent.

The portal deliberately exposes a small product API.  It is not a proxy for
the broker, controller, VNC server, or OAuth gateway.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import qrcode
import qrcode.image.svg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.asyncio.client import connect as ws_connect

from tenant_stacks import MAX_ALLOWED_HOSTS, normalize_hostname

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
BROKER_VISUAL_ACCESS_PATH = "/owner/visual-access"
BROKER_AUTH_PROFILE_SAVE_PREFIX = "/owner/sessions/"
BROKER_AUTH_PROFILE_PREFIX = "/owner/auth-profiles/"
PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
# The owner is shown this exact sentence whenever a request is denied because
# it did not come from the single identity this browser stack belongs to.
# It is deliberately plain Arabic prose (see CLAUDE.md's BiDi rule) with no
# embedded Latin tokens, so it always renders correctly in the portal UI.
SOLE_OWNER_DENIAL_AR = "هذا المتصفح وبياناته المحفوظة مخصصة لصاحب الحساب فقط، ولا يمكن لحساب آخر الوصول لها."
VIEWER_DENIAL_AR = "لا يمكن عرض المتصفح الآن — يجب فتح جلسة متصفح أولاً من نفس حسابك."
GATEWAY_CONNECTIONS_PATH = "/internal/connected-clients"
GATEWAY_ACTIVE_USER_PATH = "/internal/active-user"
GATEWAY_CONSENT_PREVIEW_PATH = "/internal/consent/preview"
GATEWAY_CONSENT_PATH = "/internal/consent"
GATEWAY_SITE_REQUESTS_PATH = "/internal/site-requests"
TENANT_POLICY_APPLY_PATH = "/internal/allowed-hosts/apply"
VIEWER_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws: wss:; "
    "media-src 'self' blob:; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)

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


def _assertion_private_key(value: str) -> Ed25519PrivateKey:
    import base64

    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return Ed25519PrivateKey.from_private_bytes(raw)
    except (ValueError, TypeError):
        raise ValueError("portal_assertion_private_key must be a base64url Ed25519 key") from None


def _portal_assertion(
    key: Ed25519PrivateKey, *, user_id: str, tenant_id: str, now: float,
    purpose: str = "browser_open",
) -> str:
    import base64

    claims = {
        "sub": user_id,
        "tenant": tenant_id,
        "purpose": purpose,
        "iat": int(now),
        "exp": int(now) + 60,
        "jti": secrets.token_urlsafe(24),
    }
    payload = base64.urlsafe_b64encode(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    signature = base64.urlsafe_b64encode(key.sign(payload.encode("ascii"))).rstrip(b"=").decode("ascii")
    return f"{payload}.{signature}"


def _site_request_scope_token(
    internal_token: str, *, user_id: str, tenant_id: str, now: float,
) -> str:
    claims = {
        "sub": user_id,
        "tenant": tenant_id,
        "purpose": "site_requests",
        "iat": int(now),
        "exp": int(now) + 60,
    }
    payload = base64.urlsafe_b64encode(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    signature = base64.urlsafe_b64encode(
        hmac.new(internal_token.encode(), payload.encode("ascii"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    return f"{payload}.{signature}"


def _otpauth_qr_svg(provisioning_uri: str) -> str:
    """Render the enrollment QR entirely server-side as inline SVG.

    This never calls a third-party QR service, never writes to disk, and the
    markup is handed straight back in the HTTP response -- nothing here is
    logged. `qrcode.image.svg.SvgPathImage` is pure XML generation; it does
    not touch the network or the filesystem.
    """
    image = qrcode.make(
        provisioning_uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2,
    )
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("ascii")
    # Strip the XML prolog: it is not valid embedded inside an HTML document.
    return svg.split("?>", 1)[-1].strip()


def _totp_manual_secret(provisioning_uri: str, enrollment: Mapping[str, Any]) -> str | None:
    """Extract the manual-entry key, preferring the otpauth URI's own secret."""
    try:
        values = parse_qs(urlsplit(provisioning_uri).query).get("secret")
    except ValueError:
        values = None
    if values and values[0]:
        return values[0]
    for key in ("secret", "totp_secret"):
        value = enrollment.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _canonical_hostname(value: Any) -> str:
    """Use the shared policy canonicalizer for every portal hostname."""
    try:
        return normalize_hostname(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "Invalid hostname") from None


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
                    authenticated_at REAL NOT NULL,
                    revoked_at REAL
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS portal_sessions_user_idx
                    ON portal_sessions(user_id, tenant_id);
                CREATE TABLE IF NOT EXISTS browser_ownership_v2 (
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    broker_session_id TEXT,
                    claimed_at REAL NOT NULL,
                    PRIMARY KEY(user_id, tenant_id)
                );
                CREATE TABLE IF NOT EXISTS portal_site_policy_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at REAL NOT NULL,
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    actor_type TEXT NOT NULL CHECK(actor_type IN ('human', 'assistant', 'system')),
                    action TEXT NOT NULL,
                    hostname TEXT,
                    outcome TEXT NOT NULL,
                    request_id TEXT
                );
                CREATE TRIGGER IF NOT EXISTS portal_site_policy_audit_no_update
                BEFORE UPDATE ON portal_site_policy_audit
                BEGIN SELECT RAISE(ABORT, 'portal site policy audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS portal_site_policy_audit_no_delete
                BEFORE DELETE ON portal_site_policy_audit
                BEGIN SELECT RAISE(ABORT, 'portal site policy audit is append-only'); END;
                CREATE TABLE IF NOT EXISTS portal_sole_owner_lock (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    bound_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portal_auth_profiles (
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    profile_name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(user_id, tenant_id, profile_name)
                );
                CREATE INDEX IF NOT EXISTS portal_auth_profiles_name_idx
                    ON portal_auth_profiles(profile_name);
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(portal_sessions)")
            }
            if "authenticated_at" not in columns:
                # Existing sessions predate freshness tracking and therefore
                # fail closed until the human authenticates again.
                db.execute(
                    "ALTER TABLE portal_sessions "
                    "ADD COLUMN authenticated_at REAL NOT NULL DEFAULT 0"
                )
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_ownership'"
            ).fetchone():
                db.execute(
                    """INSERT OR IGNORE INTO browser_ownership_v2
                       (user_id,tenant_id,broker_session_id,claimed_at)
                       SELECT user_id,tenant_id,broker_session_id,claimed_at FROM browser_ownership"""
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
                "UPDATE portal_sessions SET revoked_at=?, authenticated_at=0 "
                "WHERE user_id=? AND tenant_id=? AND revoked_at IS NULL",
                (now, user_id, tenant_id),
            )
            db.execute(
                """INSERT INTO portal_sessions
                   (token_hash, csrf_hash, user_id, tenant_id, account, created_at,
                    last_seen_at, absolute_expires_at, idle_expires_at,
                    authenticated_at, revoked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (_digest(token), _digest(csrf), user_id, tenant_id, account, now,
                 now, absolute, min(absolute, now + idle_ttl), now),
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
            # An explicitly opened browser is the owner's durable session boundary.  While
            # that same identity still owns a recorded broker session, a dropped VNC socket,
            # a sleeping phone, or an idle portal tab must not expire the cookie and lock the
            # owner out of reconnecting.  The close route removes ownership and revokes this
            # cookie; sessions without an open browser keep the normal idle/absolute limits.
            browser_open = db.execute(
                "SELECT 1 FROM browser_ownership_v2 WHERE user_id=? AND tenant_id=? "
                "AND broker_session_id IS NOT NULL",
                (row["user_id"], row["tenant_id"]),
            ).fetchone() is not None
            if (
                not browser_open
                and (row["absolute_expires_at"] <= now or row["idle_expires_at"] <= now)
            ):
                db.execute(
                    """UPDATE portal_sessions
                       SET revoked_at=?, authenticated_at=0 WHERE token_hash=?""",
                    (now, _digest(token)),
                )
                db.commit()
                return None
            absolute_expires_at = (
                max(float(row["absolute_expires_at"]), now + idle_ttl)
                if browser_open
                else float(row["absolute_expires_at"])
            )
            db.execute(
                """UPDATE portal_sessions
                   SET last_seen_at=?, idle_expires_at=?, absolute_expires_at=?
                   WHERE token_hash=?""",
                (
                    now,
                    now + idle_ttl if browser_open else min(absolute_expires_at, now + idle_ttl),
                    absolute_expires_at,
                    _digest(token),
                ),
            )
            db.commit()
            return row

    def refresh_authentication(self, token: str, *, now: float) -> bool:
        """Record a completed server-verified reauthentication."""
        with closing(self.connect()) as db:
            updated = db.execute(
                """UPDATE portal_sessions SET authenticated_at=?
                   WHERE token_hash=? AND revoked_at IS NULL
                   AND absolute_expires_at>? AND idle_expires_at>?""",
                (now, _digest(token), now, now),
            ).rowcount
        return updated == 1

    def revoke(self, token: str, *, now: float) -> None:
        with closing(self.connect()) as db:
            db.execute(
                """UPDATE portal_sessions SET revoked_at=?, authenticated_at=0
                   WHERE token_hash=? AND revoked_at IS NULL""",
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


logger = logging.getLogger(__name__)


def _upstream_error(response: httpx.Response, fallback: str) -> HTTPException:
    # Upstream bodies can contain credentials or operational details, so the
    # portal never reflects them to the CLIENT -- but that used to mean an
    # Open failure left no trace anywhere except the broker/controller's own
    # logs, which nobody watches for a single tenant's click. Log the
    # upstream status and its short `detail` (a broker HTTPException message
    # such as "Close the current session first", never raw body content) on
    # the portal's own side so the real reason is findable from one place.
    logger.warning(
        "upstream error -> %s: broker/controller responded %s %s",
        fallback, response.status_code, _safe_upstream_detail(response),
    )
    # Preserve only a useful client status class.
    status = response.status_code if 400 <= response.status_code < 500 else 502
    return HTTPException(status, fallback)


def _safe_upstream_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "<non-JSON response body>"
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return repr(body["detail"][:300])
    return "<no 'detail' field>"


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
    broker_owner_token: str | None,
    portal_assertion_private_key: str,
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
    broker_resolver: Callable[[str, str], tuple[httpx.AsyncClient, str]] | None = None,
    gateway_transport: httpx.AsyncBaseTransport | None = None,
    tenant_policy_internal_token: str | None = None,
    tenant_policy_base_url: str = "http://tenant-policy:18005",
    tenant_policy_client: httpx.AsyncClient | None = None,
    tenant_policy_transport: httpx.AsyncBaseTransport | None = None,
    policy_resolver: Callable[[str, str], Awaitable[Mapping[str, Any]] | Mapping[str, Any]] | None = None,
    policy_applier: Callable[[str, str, list[str], int], Awaitable[Mapping[str, Any]]] | None = None,
    absolute_session_ttl: int = 12 * 60 * 60,
    idle_session_ttl: int = 30 * 60,
    authentication_freshness_ttl: int = 120,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """Build the portal with injectable upstream clients or transports."""
    public_origin = _origin(public_origin)
    identity_internal_token = _credential(identity_internal_token, "identity_internal_token")
    if broker_resolver is None:
        broker_owner_token = _credential(broker_owner_token or "", "broker_owner_token")
    assertion_key = _assertion_private_key(portal_assertion_private_key)
    gateway_internal_token = _credential(gateway_internal_token, "gateway_internal_token")
    if absolute_session_ttl <= 0 or idle_session_ttl <= 0:
        raise ValueError("Session expiry settings must be positive")
    if authentication_freshness_ttl <= 0:
        raise ValueError("Authentication freshness must be positive")
    if identity_client is not None and identity_transport is not None:
        raise ValueError("Pass an identity client or transport, not both")
    if broker_client is not None and broker_transport is not None:
        raise ValueError("Pass a broker client or transport, not both")
    if gateway_client is not None and gateway_transport is not None:
        raise ValueError("Pass a gateway client or transport, not both")
    if tenant_policy_client is not None and tenant_policy_transport is not None:
        raise ValueError("Pass a tenant policy client or transport, not both")

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
    policy_http = (
        client(tenant_policy_client, tenant_policy_base_url, tenant_policy_transport)
        if tenant_policy_internal_token is not None else None
    )
    store = PortalStore(state_root)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        for item in owned:
            await item.aclose()
        close_resolver = getattr(broker_resolver, "aclose", None)
        if close_resolver is not None:
            await close_resolver()

    app = FastAPI(title="Auto Browser portal", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.portal_store = store

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers[key] = value
        # The noVNC viewer is a real application served through this origin: it loads its own
        # scripts, styles, images and opens a WebSocket back here. The site-wide
        # "default-src 'none'" policy blocked all of that and rendered an unusable page, so the
        # viewer paths get a policy that is still same-origin-only but lets the app run.
        if request.url.path == "/vnc" or request.url.path.startswith("/vnc/"):
            response.headers["Content-Security-Policy"] = VIEWER_CONTENT_SECURITY_POLICY
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

    def authentication_is_fresh(row: Mapping[str, Any], *, now: float) -> bool:
        return now < float(row["authenticated_at"]) + authentication_freshness_ttl

    def _request_origin_is_trusted(request: Request) -> bool:
        # Chrome/Firefox send Origin on cross-origin and (usually) same-origin form POSTs, but
        # several mobile browsers omit it entirely on a same-origin top-level form submission --
        # which locked the owner out of the enrollment form. When Origin is absent we fall back
        # to the two other same-origin signals the browser does send: Sec-Fetch-Site and the
        # Referer. An attacker's cross-site POST carries Sec-Fetch-Site: cross-site (or a foreign
        # Referer), so this stays a real CSRF check rather than an open door.
        origin = request.headers.get("origin")
        # An in-app browser (WhatsApp, Facebook, some webviews) can send `Origin: null` for a
        # same-origin form POST; treat that exactly like a missing Origin and fall through to
        # the other same-origin signals instead of rejecting the owner's own enrollment.
        if origin not in (None, "", "null"):
            return origin == public_origin
        if request.headers.get("sec-fetch-site") == "same-origin":
            return True
        referer = request.headers.get("referer") or ""
        return referer == public_origin or referer.startswith(public_origin + "/")

    async def mutation(request: Request, *, require_session: bool = True) -> sqlite3.Row | None:
        if not _request_origin_is_trusted(request):
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

    def site_request_headers(row: Mapping[str, Any]) -> dict[str, str]:
        scoped = _site_request_scope_token(
            gateway_internal_token,
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            now=clock(),
        )
        return {"Authorization": f"Bearer {scoped}"}

    def broker_for(user_id: str, tenant_id: str) -> tuple[httpx.AsyncClient, str]:
        if broker_resolver is not None:
            try:
                return broker_resolver(user_id, tenant_id)
            except LookupError:
                raise HTTPException(404, "Browser stack is not provisioned for this identity") from None
        assert broker_owner_token is not None
        return broker_http, broker_owner_token

    def audit(
        *, row: Mapping[str, Any], action: str, hostname: str | None,
        outcome: str, request_id: str | None = None,
    ) -> None:
        """Audit only identifiers and outcomes: never supplied authenticator codes."""
        with closing(store.connect()) as db:
            db.execute(
                """INSERT INTO portal_site_policy_audit
                   (recorded_at,user_id,tenant_id,actor_type,action,hostname,outcome,request_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (clock(), row["user_id"], row["tenant_id"], "human", action,
                 hostname, outcome, request_id),
            )

    def require_sole_new_surface_owner(row: Mapping[str, Any]) -> None:
        """Pin the noVNC viewer and the auth-profile UI to a single identity.

        This deployment is single-owner today: `broker_resolver` is only set
        once real per-tenant stack isolation exists (each tenant then gets
        its own broker/browser, so no extra lock is needed here). Without a
        resolver every identity shares the exact same physical broker and
        browser data, so the *first* identity ever to touch one of these two
        new, more sensitive surfaces is permanently bound as the only one
        allowed to use them again. This does not affect signing in, opening
        the shared browser, or the sites page -- only the live view and the
        saved-login UI added by this change.
        """
        if broker_resolver is not None:
            return
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            bound = db.execute(
                "SELECT user_id, tenant_id FROM portal_sole_owner_lock WHERE id=1"
            ).fetchone()
            if bound is None:
                db.execute(
                    "INSERT INTO portal_sole_owner_lock(id,user_id,tenant_id,bound_at) VALUES(1,?,?,?)",
                    (row["user_id"], row["tenant_id"], clock()),
                )
                db.commit()
                return
            db.commit()
        if bound["user_id"] != row["user_id"] or bound["tenant_id"] != row["tenant_id"]:
            raise HTTPException(403, SOLE_OWNER_DENIAL_AR)

    def owned_profile_names(row: Mapping[str, Any]) -> list[str]:
        with closing(store.connect()) as db:
            rows = db.execute(
                "SELECT profile_name, created_at FROM portal_auth_profiles "
                "WHERE user_id=? AND tenant_id=? ORDER BY created_at DESC",
                (row["user_id"], row["tenant_id"]),
            ).fetchall()
        return [item["profile_name"] for item in rows]

    def owns_profile(row: Mapping[str, Any], profile_name: str) -> bool:
        with closing(store.connect()) as db:
            found = db.execute(
                "SELECT 1 FROM portal_auth_profiles WHERE user_id=? AND tenant_id=? AND profile_name=?",
                (row["user_id"], row["tenant_id"], profile_name),
            ).fetchone()
        return found is not None

    def claim_profile_name(row: Mapping[str, Any], profile_name: str) -> None:
        """Record local ownership of a broker profile name.

        The underlying broker/controller profile store has no per-tenant
        namespace of its own in this single-owner deployment, so this table
        is what stops one identity from silently reusing -- and overwriting
        -- a name another identity already saved.
        """
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if broker_resolver is None:
                other = db.execute(
                    "SELECT 1 FROM portal_auth_profiles WHERE profile_name=? "
                    "AND NOT (user_id=? AND tenant_id=?)",
                    (profile_name, row["user_id"], row["tenant_id"]),
                ).fetchone()
                if other is not None:
                    db.commit()
                    raise HTTPException(409, "Profile name already belongs to a different identity")
            db.execute(
                "INSERT INTO portal_auth_profiles(user_id,tenant_id,profile_name,created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(user_id,tenant_id,profile_name) "
                "DO UPDATE SET created_at=excluded.created_at",
                (row["user_id"], row["tenant_id"], profile_name, clock()),
            )
            db.commit()

    def rename_profile_claim(row: Mapping[str, Any], old_name: str, new_name: str) -> None:
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM portal_auth_profiles WHERE user_id=? AND tenant_id=? AND profile_name=?",
                (row["user_id"], row["tenant_id"], old_name),
            )
            db.execute(
                "INSERT INTO portal_auth_profiles(user_id,tenant_id,profile_name,created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(user_id,tenant_id,profile_name) "
                "DO UPDATE SET created_at=excluded.created_at",
                (row["user_id"], row["tenant_id"], new_name, clock()),
            )
            db.commit()

    def drop_profile_claim(row: Mapping[str, Any], profile_name: str) -> None:
        with closing(store.connect()) as db:
            db.execute(
                "DELETE FROM portal_auth_profiles WHERE user_id=? AND tenant_id=? AND profile_name=?",
                (row["user_id"], row["tenant_id"], profile_name),
            )

    async def resolve_policy(user_id: str, tenant_id: str) -> tuple[list[str], int]:
        if policy_resolver is None:
            raise HTTPException(503, "Site policy service is not configured")
        try:
            value = policy_resolver(user_id, tenant_id)
            if hasattr(value, "__await__"):
                value = await value
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "Site policy could not be read") from None
        if not isinstance(value, Mapping):
            raise HTTPException(502, "Invalid site policy response")
        hosts, revision = value.get("allowed_hosts"), value.get("revision")
        if not isinstance(hosts, list) or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise HTTPException(502, "Invalid site policy response")
        try:
            canonical = [_canonical_hostname(host) for host in hosts]
        except HTTPException:
            raise HTTPException(502, "Invalid site policy response") from None
        if len(canonical) > MAX_ALLOWED_HOSTS or len(set(canonical)) != len(canonical):
            raise HTTPException(502, "Invalid site policy response")
        return sorted(canonical), revision

    async def apply_policy(
        user_id: str, tenant_id: str, desired_hosts: list[str], expected_revision: int,
    ) -> tuple[list[str], int]:
        if policy_applier is not None:
            try:
                result = await policy_applier(user_id, tenant_id, desired_hosts, expected_revision)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "Site policy could not be applied") from None
            # The injected seam is trusted application code.  Let a small fake
            # applier use a side-effect-only contract without weakening the
            # production service's explicit response confirmation.
            if result is None:
                result = {"allowed_hosts": desired_hosts, "revision": expected_revision + 1}
        else:
            if policy_http is None or tenant_policy_internal_token is None:
                raise HTTPException(503, "Site policy service is not configured")
            try:
                response = await policy_http.post(
                    TENANT_POLICY_APPLY_PATH,
                    headers=internal_headers(tenant_policy_internal_token),
                    json={
                        "portal_assertion": _portal_assertion(
                            assertion_key, user_id=user_id, tenant_id=tenant_id,
                            now=clock(), purpose="site_policy_change",
                        ),
                        "allowed_hosts": desired_hosts,
                        "expected_revision": expected_revision,
                    },
                )
            except httpx.HTTPError:
                raise HTTPException(502, "Site policy service unavailable") from None
            if response.status_code not in (200, 201):
                raise _upstream_error(response, "Site policy could not be applied")
            result = response.json()
        if not isinstance(result, Mapping):
            raise HTTPException(502, "Invalid site policy response")
        hosts, revision = result.get("allowed_hosts"), result.get("revision")
        if not isinstance(hosts, list) or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise HTTPException(502, "Invalid site policy response")
        try:
            returned_hosts = sorted(_canonical_hostname(host) for host in hosts)
        except HTTPException:
            raise HTTPException(502, "Invalid site policy response") from None
        if returned_hosts != desired_hosts or len(returned_hosts) > MAX_ALLOWED_HOSTS:
            raise HTTPException(502, "Site policy application was not confirmed")
        return returned_hosts, revision

    async def require_fresh_site_change(
        request: Request, row: Mapping[str, Any], data: Mapping[str, Any], *, action: str,
    ) -> None:
        """Require a fresh authenticator code for a sensitive settings change.

        Despite the name (kept for the identity service's existing
        "site_policy_change" purpose bucket), this also gates saving,
        renaming, and deleting an auth profile -- those are exactly as
        sensitive as changing allowed sites.
        """
        if authentication_is_fresh(row, now=clock()):
            return
        response = await identity_http.post(
            IDENTITY_VERIFY_PATH, headers=internal_headers(identity_internal_token),
            json={"account": row["account"], "totp_code": _totp(data), "purpose": "site_policy_change"},
        )
        if response.status_code != 200:
            audit(row=row, action=action, hostname=None, outcome="reauthentication_failed")
            raise _upstream_error(response, "Authenticator verification failed")
        verified_user, verified_tenant, _ = _identity(response.json(), row["account"])
        if verified_user != row["user_id"] or verified_tenant != row["tenant_id"]:
            audit(row=row, action=action, hostname=None, outcome="binding_rejected")
            raise HTTPException(403, "Identity binding changed")
        if not store.refresh_authentication(request.cookies.get(SESSION_COOKIE, ""), now=clock()):
            raise HTTPException(401, "Session expired or revoked")

    async def close_owned_browser(row: Mapping[str, Any]) -> None:
        """Close the identity-bound browser before a controller restart."""
        with closing(store.connect()) as db:
            owner = db.execute(
                "SELECT * FROM browser_ownership_v2 WHERE user_id=? AND tenant_id=?",
                (row["user_id"], row["tenant_id"]),
            ).fetchone()
        if owner is None:
            return
        if not owner["broker_session_id"]:
            raise HTTPException(409, "Browser session is still opening")
        selected_broker, selected_owner_token = broker_for(row["user_id"], row["tenant_id"])
        try:
            closed = await selected_broker.delete(
                BROKER_CLOSE_PREFIX + owner["broker_session_id"],
                headers=internal_headers(selected_owner_token),
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
                "DELETE FROM browser_ownership_v2 WHERE user_id=? AND tenant_id=? AND broker_session_id=?",
                (row["user_id"], row["tenant_id"], owner["broker_session_id"]),
            )

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
        # A browser form post lands a human on this response, so send them to the browser page
        # instead of raw JSON; only a non-browser client (no HTML in Accept) sees the JSON body.
        wants_html = "text/html" in (request.headers.get("accept") or "")
        result = (
            RedirectResponse(next_path or "/browser", status_code=303)
            if (next_path or wants_html)
            else JSONResponse({"status": "signed_in", "user_id": user_id, "tenant_id": tenant_id})
        )
        # max_age keeps the sign-in across a closed phone browser for as long as the
        # server-side session itself lives (absolute_session_ttl); without it the
        # cookie died with the tab and the owner had to sign in with a code again.
        result.set_cookie(
            SESSION_COOKIE, token, httponly=True, secure=True, samesite="lax", path="/",
            max_age=absolute_session_ttl,
        )
        result.set_cookie(
            CSRF_COOKIE, csrf, httponly=False, secure=True, samesite="lax", path="/",
            max_age=absolute_session_ttl,
        )
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
            owner = db.execute(
                "SELECT * FROM browser_ownership_v2 WHERE user_id=? AND tenant_id=?",
                (row["user_id"], row["tenant_id"]),
            ).fetchone()
        browser = "closed" if owner is None else "open" if owner["broker_session_id"] else "opening"
        return {
            "authenticated": True, "user_id": row["user_id"], "tenant_id": row["tenant_id"],
            "account": row["account"], "browser": browser,
            "browser_open_requires_code": not authentication_is_fresh(row, now=clock()),
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
            owner = db.execute(
                "SELECT * FROM browser_ownership_v2 WHERE user_id=? AND tenant_id=?",
                (row["user_id"], row["tenant_id"]),
            ).fetchone()
        state = "closed" if owner is None else "open" if owner["broker_session_id"] else "opening"
        csrf = html.escape(request.cookies.get(CSRF_COOKIE, ""), quote=True)
        account = html.escape(row["account"])
        # Always render the code field. Freshness can expire between rendering this page and
        # pressing Open, which used to produce a bare "A 6-digit authenticator code is required"
        # JSON error with no field to type it into.
        fresh = authentication_is_fresh(row, now=clock())
        label = (
            "كود المصادقة لو اتطلب منك (authenticator code)" if fresh else "Fresh authenticator code"
        )
        authenticator_field = (
            f"<label>{label} <input name=totp_code inputmode=numeric "
            "autocomplete=one-time-code pattern='[0-9]{6}'"
            + ("" if fresh else " required")
            + "></label>"
        )
        viewer_link = (
            "<p><a href='/vnc/vnc.html?autoconnect=true&reconnect=true&reconnect_delay=1500&resize=scale&path=websockify&quality=4&compression=7'>"
            "شوف المتصفح (Watch and control the browser)</a></p>"
            if state == "open" else
            "<p>شوف المتصفح: افتح المتصفح أولاً.</p>"
        )
        # The viewer's keyboard goes through VNC/X11, which does not reliably carry
        # Arabic (or other non-Latin) typing from a phone keyboard. This box sends
        # typed text straight to whatever field is focused in the browser instead --
        # click the field in the viewer above first, then type here and send.
        type_bridge = (
            "<h2>اكتب هنا (Type here)</h2>"
            "<p>دوس على الحقل في المتصفح فوق الأول، وبعدين اكتب هنا وابعت.</p>"
            "<form method=post action=/api/browser/type>"
            f"<input type=hidden name=csrf_token value='{csrf}'>"
            "<input name=text dir=auto lang=ar autocomplete=off "
            "placeholder='اكتب هنا بأي لغة' style='width:100%;font-size:1.1em' required>"
            "<button>ابعت (Send)</button></form>"
            if state == "open" else ""
        )
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Secure Browser</title>"
            f"<h1>Secure Browser</h1><p>Signed in as {account}</p><p>Browser: {state}</p>"
            "<p><a href='/sites'>Manage allowed sites and assistant requests</a></p>"
            "<p><a href='/profiles'>احفظ الدخول (saved logins)</a></p>"
            f"{viewer_link}"
            f"{type_bridge}"
            "<h2>Open browser</h2><form method=post action=/api/browser/open>"
            f"<input type=hidden name=csrf_token value='{csrf}'>"
            "<label>Start URL <input name=start_url type=url value='https://www.google.com' required></label>"
            "<label>Saved login (optional) <input name=auth_profile placeholder='leave blank for a fresh browser'></label>"
            f"{authenticator_field}<button>Open</button></form>"
            "<h2>Close browser</h2><form method=post action=/api/browser/close>"
            f"<input type=hidden name=csrf_token value='{csrf}'><button>Close server session</button></form>"
        )

    def browser_open_response(request: Request, session_id: str):
        """Return the same successful result for a new or already-open browser.

        Opening is idempotent for one authenticated owner: a repeated form submit or a
        retry after a lost response must rejoin the live session, not surface a conflict.
        """
        if "text/html" in (request.headers.get("accept") or ""):
            return RedirectResponse("/browser", status_code=303)
        return {"status": "open", "session_id": session_id}

    async def await_browser_open(row: Mapping[str, Any], *, timeout_seconds: float = 5.0) -> str | None:
        """Wait briefly for an identical in-flight Open request to finish.

        The ownership row is created before the broker call and receives its session id
        after the broker succeeds.  A second tap that sees the row in that short NULL state
        waits for the first request instead of issuing a competing broker Open.
        """
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            with closing(store.connect()) as db:
                owner = db.execute(
                    "SELECT broker_session_id FROM browser_ownership_v2 "
                    "WHERE user_id=? AND tenant_id=?",
                    (row["user_id"], row["tenant_id"]),
                ).fetchone()
            if owner is None:
                return None
            session_id = owner["broker_session_id"]
            if session_id and await viewer_session_id(row) == session_id:
                return session_id
            await asyncio.sleep(0.05)
        return None

    def safe_site_request(value: Any, row: Mapping[str, Any]) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        request_id = value.get("request_id", value.get("id"))
        raw_host = value.get("hostname", value.get("host"))
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
            return None
        if value.get("status") != "pending":
            return None
        try:
            hostname = _canonical_hostname(raw_host)
        except HTTPException:
            return None
        # A gateway response must never be used to cross an immutable binding.
        if value.get("user_id", row["user_id"]) != row["user_id"]:
            return None
        if value.get("tenant_id", row["tenant_id"]) != row["tenant_id"]:
            return None
        expires_at = value.get("expires_at")
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
            if expires_at <= clock():
                return None
        elif expires_at is not None and (not isinstance(expires_at, str) or len(expires_at) > 80):
            return None
        result: dict[str, Any] = {"request_id": request_id, "hostname": hostname}
        if expires_at is not None:
            result["expires_at"] = expires_at
        for key in ("reason", "client_name"):
            item = value.get(key)
            if isinstance(item, str) and item.strip() and len(item) <= 500:
                result[key] = item.strip()
        return result

    async def pending_site_requests(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            response = await gateway_http.get(
                GATEWAY_SITE_REQUESTS_PATH, headers=site_request_headers(row),
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Assistant request service unavailable") from None
        if response.status_code != 200:
            raise _upstream_error(response, "Assistant requests could not be listed")
        body = response.json()
        values = body.get("requests", body) if isinstance(body, (Mapping, list)) else []
        if not isinstance(values, list):
            raise HTTPException(502, "Invalid assistant request response")
        return [item for value in values if (item := safe_site_request(value, row)) is not None]

    async def site_request(request_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
            raise HTTPException(404, "Assistant request not found")
        try:
            response = await gateway_http.get(
                f"{GATEWAY_SITE_REQUESTS_PATH}/{request_id}",
                headers=site_request_headers(row),
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Assistant request service unavailable") from None
        if response.status_code == 404:
            raise HTTPException(404, "Assistant request not found")
        if response.status_code != 200:
            raise _upstream_error(response, "Assistant request could not be read")
        item = safe_site_request(response.json(), row)
        if item is None:
            raise HTTPException(404, "Assistant request not found")
        return item

    async def decide_site_request(
        *, row: Mapping[str, Any], request_id: str, decision: str,
    ) -> None:
        try:
            response = await gateway_http.post(
                f"{GATEWAY_SITE_REQUESTS_PATH}/{request_id}/decision",
                headers=site_request_headers(row),
                json={"decision": decision},
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Assistant request service unavailable") from None
        if response.status_code not in (200, 204):
            raise _upstream_error(response, "Assistant request decision could not be recorded")

    @app.get("/sites")
    async def sites_page(request: Request):
        try:
            row = session_for(request)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            return RedirectResponse("/signin?next=%2Fsites", status_code=303)
        csrf = html.escape(request.cookies.get(CSRF_COOKIE, ""), quote=True)
        fresh = authentication_is_fresh(row, now=clock())
        code = "" if fresh else (
            "<label>Fresh authenticator code <input name=totp_code inputmode=numeric "
            "autocomplete=one-time-code pattern='[0-9]{6}' required></label>"
        )
        hosts, _ = await resolve_policy(row["user_id"], row["tenant_id"])
        requests = await pending_site_requests(row)
        host_items = "".join(
            "<li><code>" + html.escape(host) + "</code>"
            "<form method=post action=/api/sites/remove><input type=hidden name=csrf_token value='" + csrf + "'>"
            "<input type=hidden name=hostname value='" + html.escape(host, quote=True) + "'>"
            + code + "<button>Remove</button></form></li>"
            for host in hosts
        ) or "<li>No sites are currently allowed.</li>"
        request_items = "".join(
            "<li><code>" + html.escape(item["hostname"]) + "</code>"
            + (" expires " + html.escape(str(item["expires_at"])) if "expires_at" in item else "")
            + (" from " + html.escape(item["client_name"]) if "client_name" in item else "")
            + (" — " + html.escape(item["reason"]) if "reason" in item else "")
            + "<form method=post action=/api/site-requests/" + html.escape(item["request_id"], quote=True)
            + "/decision><input type=hidden name=csrf_token value='" + csrf + "'>"
            + code + "<button name=decision value=approved>Approve</button>"
            "<button name=decision value=denied>Deny</button></form></li>"
            for item in requests
        ) or "<li>No pending assistant requests.</li>"
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Allowed sites</title>"
            "<h1>Allowed sites</h1><p><a href='/browser'>Back to browser</a></p>"
            "<p>Applying a change briefly restarts only your controller. Your current browser session "
            "will close and must be reopened; saved authentication profiles stay intact.</p>"
            "<h2>Allowed sites</h2><ul>" + host_items + "</ul>"
            "<form method=post action=/api/sites><input type=hidden name=csrf_token value='" + csrf + "'>"
            "<label>Hostname <input name=hostname required></label>" + code + "<button>Add site</button></form>"
            "<h2>Pending assistant requests</h2><ul>" + request_items + "</ul>"
        )

    @app.get("/api/sites")
    async def list_sites(request: Request):
        row = session_for(request)
        allowed_hosts, revision = await resolve_policy(row["user_id"], row["tenant_id"])
        return {"allowed_hosts": allowed_hosts, "revision": revision}

    @app.post("/api/sites")
    async def add_site(request: Request):
        row = await mutation(request)
        data = await _payload(request)
        hostname = _canonical_hostname(data.get("hostname"))
        await require_fresh_site_change(request, row, data, action="site_add")
        hosts, revision = await resolve_policy(row["user_id"], row["tenant_id"])
        if hostname in hosts:
            audit(row=row, action="site_add", hostname=hostname, outcome="already_allowed")
            raise HTTPException(409, "Hostname is already allowed")
        if len(hosts) >= MAX_ALLOWED_HOSTS:
            audit(row=row, action="site_add", hostname=hostname, outcome="limit_rejected")
            raise HTTPException(422, "At most 64 hostnames are allowed")
        desired = sorted([*hosts, hostname])
        try:
            await close_owned_browser(row)
            applied_hosts, applied_revision = await apply_policy(
                row["user_id"], row["tenant_id"], desired, revision,
            )
        except HTTPException as exc:
            audit(row=row, action="site_add", hostname=hostname, outcome="failed")
            raise exc
        audit(row=row, action="site_add", hostname=hostname, outcome="applied")
        return {"allowed_hosts": applied_hosts, "revision": applied_revision}

    @app.delete("/api/sites/{hostname}")
    async def remove_site(hostname: str, request: Request):
        row = await mutation(request)
        data = await _payload(request)
        hostname = _canonical_hostname(hostname)
        await require_fresh_site_change(request, row, data, action="site_remove")
        hosts, revision = await resolve_policy(row["user_id"], row["tenant_id"])
        if hostname not in hosts:
            audit(row=row, action="site_remove", hostname=hostname, outcome="not_allowed")
            raise HTTPException(404, "Hostname is not allowed")
        desired = [host for host in hosts if host != hostname]
        try:
            await close_owned_browser(row)
            applied_hosts, applied_revision = await apply_policy(
                row["user_id"], row["tenant_id"], desired, revision,
            )
        except HTTPException as exc:
            audit(row=row, action="site_remove", hostname=hostname, outcome="failed")
            raise exc
        audit(row=row, action="site_remove", hostname=hostname, outcome="applied")
        return {"allowed_hosts": applied_hosts, "revision": applied_revision}

    @app.post("/api/sites/remove")
    async def remove_site_form(request: Request):
        row = await mutation(request)
        data = await _payload(request)
        hostname = _canonical_hostname(data.get("hostname"))
        await require_fresh_site_change(request, row, data, action="site_remove")
        hosts, revision = await resolve_policy(row["user_id"], row["tenant_id"])
        if hostname not in hosts:
            audit(row=row, action="site_remove", hostname=hostname, outcome="not_allowed")
            raise HTTPException(404, "Hostname is not allowed")
        desired = [host for host in hosts if host != hostname]
        try:
            await close_owned_browser(row)
            applied_hosts, applied_revision = await apply_policy(
                row["user_id"], row["tenant_id"], desired, revision,
            )
        except HTTPException as exc:
            audit(row=row, action="site_remove", hostname=hostname, outcome="failed")
            raise exc
        audit(row=row, action="site_remove", hostname=hostname, outcome="applied")
        return {"allowed_hosts": applied_hosts, "revision": applied_revision}

    @app.get("/api/site-requests")
    async def list_site_requests(request: Request):
        return {"requests": await pending_site_requests(session_for(request))}

    @app.post("/api/site-requests/{request_id}/decision")
    async def site_request_decision(request_id: str, request: Request):
        row = await mutation(request)
        data = await _payload(request)
        decision = data.get("decision")
        if decision not in ("approved", "denied"):
            raise HTTPException(422, "Invalid assistant request decision")
        item = await site_request(request_id, row)
        await require_fresh_site_change(request, row, data, action="site_request_" + decision)
        if decision == "approved":
            hosts, revision = await resolve_policy(row["user_id"], row["tenant_id"])
            desired = sorted(set([*hosts, item["hostname"]]))
            if len(desired) > MAX_ALLOWED_HOSTS:
                audit(row=row, action="site_request_approved", hostname=item["hostname"], outcome="limit_rejected", request_id=request_id)
                raise HTTPException(422, "At most 64 hostnames are allowed")
            if desired != hosts:
                try:
                    await close_owned_browser(row)
                    await apply_policy(row["user_id"], row["tenant_id"], desired, revision)
                except HTTPException as exc:
                    audit(row=row, action="site_request_approved", hostname=item["hostname"], outcome="failed", request_id=request_id)
                    raise exc
        try:
            await decide_site_request(row=row, request_id=request_id, decision=decision)
        except HTTPException as exc:
            audit(row=row, action="site_request_" + decision, hostname=item["hostname"], outcome="decision_failed", request_id=request_id)
            raise exc
        audit(row=row, action="site_request_" + decision, hostname=item["hostname"], outcome="recorded", request_id=request_id)
        return {"status": decision, "request_id": request_id}

    @app.post("/api/browser/open")
    async def open_browser(request: Request):
        row = await mutation(request)
        data = await _payload(request)
        now = clock()
        code = None
        requires_code = not authentication_is_fresh(row, now=now)
        newly_claimed = False
        opening_in_progress = False
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT * FROM browser_ownership_v2 WHERE user_id=? AND tenant_id=?",
                (row["user_id"], row["tenant_id"]),
            ).fetchone()
            if owner is not None and owner["broker_session_id"]:
                db.commit()
                # The recorded session can die upstream (the tenant stack was rebuilt or
                # restarted, or the session was closed out of band) while this row still
                # points at it. That used to wedge the owner permanently: Open said "already
                # open" and the viewer said "no session", with no way out except a manual
                # database fix. Verify against the broker and clear a dead record instead of
                # trusting the local row blindly.
                if await viewer_session_id(row) is not None:
                    return browser_open_response(request, owner["broker_session_id"])
                with closing(store.connect()) as cleanup_db:
                    cleanup_db.execute(
                        """UPDATE browser_ownership_v2 SET broker_session_id=NULL
                           WHERE user_id=? AND tenant_id=? AND broker_session_id=?""",
                        (row["user_id"], row["tenant_id"], owner["broker_session_id"]),
                    )
                    cleanup_db.commit()
                # The row already exists (broker_session_id now NULL) -- do not INSERT again,
                # that would violate the (user_id, tenant_id) primary key. `newly_claimed=True`
                # only controls the failure-cleanup below, which deletes on `broker_session_id
                # IS NULL` regardless of whether the row is fresh or just-healed, so this is
                # safe either way.
                newly_claimed = True
            elif owner is None:
                db.execute(
                    "INSERT INTO browser_ownership_v2(user_id,tenant_id,broker_session_id,claimed_at) VALUES(?,?,NULL,?)",
                    (row["user_id"], row["tenant_id"], clock()),
                )
                newly_claimed = True
            else:
                # Another request from this same authenticated owner has already claimed the
                # row and is between broker Open and recording the returned session id.  Do
                # not race it with a second broker Open; wait outside the SQLite transaction.
                opening_in_progress = True
            db.commit()
        if opening_in_progress:
            existing_session_id = await await_browser_open(row)
            if existing_session_id is not None:
                return browser_open_response(request, existing_session_id)
            raise HTTPException(409, "Browser session is still opening; try again")
        try:
            if requires_code:
                code = _totp(data)
                verified = await identity_http.post(
                    IDENTITY_VERIFY_PATH, headers=internal_headers(identity_internal_token),
                    json={"account": row["account"], "totp_code": code, "purpose": "browser_open"},
                )
                if verified.status_code != 200:
                    raise _upstream_error(verified, "Authenticator verification failed")
                verified_user, verified_tenant, _ = _identity(verified.json(), row["account"])
                if verified_user != row["user_id"] or verified_tenant != row["tenant_id"]:
                    raise HTTPException(403, "Identity binding changed")
                session_token = request.cookies.get(SESSION_COOKIE, "")
                if not store.refresh_authentication(session_token, now=clock()):
                    raise HTTPException(401, "Session expired or revoked")
            broker_payload: dict[str, Any] = {
                "portal_assertion": _portal_assertion(
                    assertion_key, user_id=row["user_id"], tenant_id=row["tenant_id"], now=clock()
                ),
                "start_url": "https://www.google.com",
            }
            if "start_url" in data:
                broker_payload["start_url"] = _required_text(data, "start_url", 2048)
            # An HTML form always submits the field, empty or not; an empty value means
            # "fresh browser, no saved login", not an invalid profile name.
            raw_profile = data.get("auth_profile")
            if isinstance(raw_profile, str) and not raw_profile.strip():
                data = {k: v for k, v in data.items() if k != "auth_profile"}
            if "auth_profile" in data:
                profile_name = _required_text(data, "auth_profile", 200)
                if not PROFILE_NAME_PATTERN.fullmatch(profile_name):
                    raise HTTPException(422, "Invalid auth_profile")
                # Opening a browser pre-logged-in from a saved profile is exactly
                # as sensitive as viewing or managing that profile, so it is
                # gated the same way (see require_sole_new_surface_owner).
                require_sole_new_surface_owner(row)
                if not owns_profile(row, profile_name):
                    # A name nobody has claimed yet is either brand new or a
                    # profile saved before this ownership table existed; adopt
                    # it for this identity instead of orphaning a real login.
                    with closing(store.connect()) as db:
                        db.execute("BEGIN IMMEDIATE")
                        if broker_resolver is None and db.execute(
                            "SELECT 1 FROM portal_auth_profiles WHERE profile_name=? "
                            "AND NOT (user_id=? AND tenant_id=?)",
                            (profile_name, row["user_id"], row["tenant_id"]),
                        ).fetchone():
                            db.commit()
                            raise HTTPException(403, SOLE_OWNER_DENIAL_AR)
                        db.execute(
                            "INSERT OR IGNORE INTO portal_auth_profiles"
                            "(user_id,tenant_id,profile_name,created_at) VALUES(?,?,?,?)",
                            (row["user_id"], row["tenant_id"], profile_name, clock()),
                        )
                        db.commit()
                broker_payload["auth_profile"] = profile_name
            selected_broker, selected_owner_token = broker_for(row["user_id"], row["tenant_id"])
            opened = await selected_broker.post(
                BROKER_OPEN_PATH, headers=internal_headers(selected_owner_token), json=broker_payload,
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
                    """UPDATE browser_ownership_v2 SET broker_session_id=?
                       WHERE user_id=? AND tenant_id=? AND broker_session_id IS NULL""",
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
                cleanup = await selected_broker.delete(
                    BROKER_CLOSE_PREFIX + session_id, headers=internal_headers(selected_owner_token),
                )
                if cleanup.status_code not in (200, 204, 404):
                    raise HTTPException(502, "Gateway binding failed and browser cleanup was not confirmed")
                with closing(store.connect()) as db:
                    db.execute(
                        "DELETE FROM browser_ownership_v2 WHERE user_id=? AND tenant_id=? AND broker_session_id=?",
                        (row["user_id"], row["tenant_id"], session_id),
                    )
                if active is None:
                    raise HTTPException(502, "Browser ownership could not be activated")
                raise _upstream_error(active, "Browser ownership could not be activated")
            return browser_open_response(request, session_id)
        except httpx.HTTPError:
            raise HTTPException(502, "Browser service unavailable") from None
        finally:
            # A failed first attempt must not permanently reserve capacity.  A
            # successful claim has a session id and is unaffected by this.
            if newly_claimed:
                with closing(store.connect()) as db:
                    db.execute(
                        """DELETE FROM browser_ownership_v2 WHERE user_id=? AND tenant_id=?
                           AND broker_session_id IS NULL""",
                        (row["user_id"], row["tenant_id"]),
                    )

    @app.post("/api/browser/close")
    async def close_browser(request: Request):
        row = await mutation(request)
        await close_owned_browser(row)
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            store.revoke(token, now=clock())
        result = (
            RedirectResponse("/signin", status_code=303)
            if "text/html" in (request.headers.get("accept") or "")
            else JSONResponse({"status": "closed"})
        )
        result.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
        result.delete_cookie(CSRF_COOKIE, secure=True, samesite="lax", path="/")
        return result

    @app.post("/api/browser/type")
    async def type_into_browser(request: Request):
        """Send text into whatever is focused in the live browser, bypassing VNC keys.

        The noVNC viewer's keyboard channel goes through X11 keysyms, which mobile
        IMEs (Gboard's Arabic layout included) do not reliably feed for non-Latin
        scripts. This bridge lets the owner click a field through the viewer's
        mouse, then type into this box instead: the text is delivered to the
        focused element directly via the controller, independent of language.
        """
        row = await mutation(request)
        require_sole_new_surface_owner(row)
        data = await _payload(request)
        text = _required_text(data, "text", 2000)
        session_id = await viewer_session_id(row)
        if session_id is None:
            raise HTTPException(403, VIEWER_DENIAL_AR)
        selected_broker, selected_owner_token = broker_for(row["user_id"], row["tenant_id"])
        try:
            response = await selected_broker.post(
                f"/owner/sessions/{session_id}/type",
                headers=internal_headers(selected_owner_token),
                json={"text": text},
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Browser service unavailable") from None
        if response.status_code != 200:
            raise _upstream_error(response, "Text could not be sent")
        if "text/html" in (request.headers.get("accept") or ""):
            return RedirectResponse("/browser", status_code=303)
        return {"status": "sent"}

    _viewer_session_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
    _VIEWER_SESSION_CACHE_SECONDS = 1.0

    async def viewer_session_id_cached(row: Mapping[str, Any]) -> str | None:
        """Same check as `viewer_session_id`, memoised for a few seconds.

        The noVNC client pulls ~50 static files in one burst and each one used to run the full
        broker/controller guard, which tripped the upstream rate limiter and left the viewer
        stuck loading. The websocket path deliberately does NOT use this cache: it keeps
        re-running the uncached check before accept and on every inbound frame.
        """
        key = (row["user_id"], row["tenant_id"])
        now = clock()
        cached = _viewer_session_cache.get(key)
        if cached is not None and now - cached[0] < _VIEWER_SESSION_CACHE_SECONDS:
            return cached[1]
        value = await viewer_session_id(row)
        # Only a positive result is cached. Caching "no session" would keep denying for seconds
        # after the owner opens the browser, which is exactly when he reloads the viewer.
        if value is not None:
            _viewer_session_cache[key] = (now, value)
        else:
            _viewer_session_cache.pop(key, None)
        return value

    async def viewer_session_id(row: Mapping[str, Any]) -> str | None:
        """The live broker session id for this identity, or None if there isn't one.

        This is the single gate the noVNC viewer relies on: it re-runs before
        the socket is accepted, before every inbound control frame, and once
        a second on a timer, mirroring the broker's own `/owner/visual-access`
        guard so a closed or replaced session can never keep giving a view.
        """
        with closing(store.connect()) as db:
            owner = db.execute(
                "SELECT broker_session_id FROM browser_ownership_v2 WHERE user_id=? AND tenant_id=?",
                (row["user_id"], row["tenant_id"]),
            ).fetchone()
        if owner is None or not owner["broker_session_id"]:
            return None
        selected_broker, selected_owner_token = broker_for(row["user_id"], row["tenant_id"])
        try:
            guard = await selected_broker.get(
                BROKER_VISUAL_ACCESS_PATH, headers=internal_headers(selected_owner_token), timeout=5,
            )
        except httpx.HTTPError:
            return None
        if guard.status_code != 200:
            return None
        try:
            session_id = guard.json().get("session_id") if guard.content else None
        except ValueError:
            return None
        if not isinstance(session_id, str) or session_id != owner["broker_session_id"]:
            return None
        return session_id

    _VNC_DROP_REQUEST_HEADERS = {
        "host", "connection", "upgrade", "cookie", "authorization",
        "content-length", "x-csrf-token", "origin",
    }
    _VNC_KEEP_RESPONSE_HEADERS = {"content-type", "content-length", "etag", "last-modified", "cache-control"}

    @app.api_route("/vnc/{path:path}", methods=["GET", "HEAD"])
    async def vnc_static(path: str, request: Request):
        # Interactive access is intentional and required: the owner types
        # passwords and scans WhatsApp/Facebook QR codes through this exact
        # view, so it is never downgraded to a read-only/view-only mode.
        row = session_for(request)
        require_sole_new_surface_owner(row)
        if ".." in path or path.startswith("/") or not re.fullmatch(r"[A-Za-z0-9._/-]*", path):
            raise HTTPException(404, "Not found")
        if await viewer_session_id_cached(row) is None:
            raise HTTPException(403, VIEWER_DENIAL_AR)
        # noVNC itself lives on browser-node, which only the broker can reach
        # (the tenant-private network never includes the portal), so this
        # proxies through the broker's own gated /owner/vnc/* relay rather
        # than reaching browser-node directly.
        selected_broker, selected_owner_token = broker_for(row["user_id"], row["tenant_id"])
        headers = {
            key: value for key, value in request.headers.items()
            if key.lower() not in _VNC_DROP_REQUEST_HEADERS
        }
        headers["Authorization"] = f"Bearer {selected_owner_token}"
        try:
            upstream = await selected_broker.request(
                request.method, f"/owner/vnc/{path}", params=request.query_params, headers=headers, timeout=10,
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Browser view is unavailable") from None
        if upstream.status_code == 403:
            raise HTTPException(403, VIEWER_DENIAL_AR)
        if upstream.status_code >= 400:
            raise _upstream_error(upstream, "Browser view is unavailable")
        response_headers = {
            key: value for key, value in upstream.headers.items()
            if key.lower() in _VNC_KEEP_RESPONSE_HEADERS
        }
        return Response(
            content=upstream.content if request.method != "HEAD" else b"",
            status_code=upstream.status_code, headers=response_headers,
        )

    @app.websocket("/vnc/websockify")
    async def vnc_websocket(websocket: WebSocket):
        if websocket.headers.get("origin") not in (None, public_origin):
            await websocket.close(code=1008)
            return
        token = websocket.cookies.get(SESSION_COOKIE)
        row = store.session(token, now=clock(), idle_ttl=idle_session_ttl) if token else None
        if row is None:
            await websocket.close(code=1008)
            return
        try:
            require_sole_new_surface_owner(row)
        except HTTPException:
            await websocket.close(code=1008)
            return
        session_id = await viewer_session_id(row)
        if session_id is None:
            await websocket.close(code=1008)
            return
        selected_broker, selected_owner_token = broker_for(row["user_id"], row["tenant_id"])
        ws_url = "ws" + str(selected_broker.base_url).removeprefix("http") + "/owner/vnc/websockify"
        # The VNC socket is real owner activity, but it does not make ordinary HTTP requests.
        # Refresh the portal idle lease while this authenticated, ownership-bound viewer is
        # alive so a harmless network drop can reconnect instead of being rejected as an
        # idle session.  The absolute session lifetime is still enforced by store.session.
        heartbeat_seconds = min(30.0, max(1.0, idle_session_ttl / 3))
        next_session_heartbeat = clock() + heartbeat_seconds
        try:
            async with ws_connect(
                ws_url, additional_headers={"Authorization": f"Bearer {selected_owner_token}"},
                max_size=16 * 1024 * 1024, open_timeout=5,
            ) as upstream:
                if await viewer_session_id(row) != session_id:
                    await websocket.close(code=1008)
                    return
                await websocket.accept()

                async def browser_to_vnc() -> None:
                    try:
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                break
                            # Every inbound frame can carry keyboard/mouse input, so control
                            # must drop as soon as the session closes. The check runs on every
                            # frame but through a 1-second memo: an uncached broker+controller
                            # round trip per keystroke and per video frame made typing lag and
                            # drop characters. `periodic_guard` re-runs the uncached check every
                            # second, so control still ends within a second of revocation.
                            if await viewer_session_id_cached(row) != session_id:
                                break
                            if message.get("bytes") is not None:
                                await upstream.send(message["bytes"])
                            elif message.get("text") is not None:
                                await upstream.send(message["text"])
                    except (WebSocketDisconnect, RuntimeError):
                        pass

                async def vnc_to_browser() -> None:
                    async for message in upstream:
                        if await viewer_session_id_cached(row) != session_id:
                            break
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                async def periodic_guard() -> None:
                    nonlocal next_session_heartbeat
                    while True:
                        await asyncio.sleep(1)
                        now = clock()
                        if now >= next_session_heartbeat:
                            if store.session(token, now=now, idle_ttl=idle_session_ttl) is None:
                                break
                            next_session_heartbeat = now + heartbeat_seconds
                        if await viewer_session_id(row) != session_id:
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

    @app.get("/profiles")
    async def profiles_page(request: Request):
        try:
            row = session_for(request)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            return RedirectResponse("/signin?next=%2Fprofiles", status_code=303)
        try:
            require_sole_new_surface_owner(row)
        except HTTPException as exc:
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8><title>Saved logins</title>"
                f"<h1>Saved logins</h1><p>{html.escape(str(exc.detail))}</p>"
                "<p><a href='/browser'>Back to browser</a></p>",
                status_code=exc.status_code,
            )
        csrf = html.escape(request.cookies.get(CSRF_COOKIE, ""), quote=True)
        fresh = authentication_is_fresh(row, now=clock())
        code = "" if fresh else (
            "<label>Fresh authenticator code <input name=totp_code inputmode=numeric "
            "autocomplete=one-time-code pattern='[0-9]{6}' required></label>"
        )
        names = owned_profile_names(row)
        items = "".join(
            "<li><code>" + html.escape(name) + "</code> "
            "<form style='display:inline' method=post action='/api/auth-profiles/"
            + html.escape(name, quote=True) + "/rename'>"
            "<input type=hidden name=csrf_token value='" + csrf + "'>"
            "<input name=new_name placeholder='new name' required>" + code
            + "<button>Rename</button></form> "
            "<form style='display:inline' method=post action='/api/auth-profiles/"
            + html.escape(name, quote=True) + "/delete'>"
            "<input type=hidden name=csrf_token value='" + csrf + "'>" + code
            + "<button>Delete</button></form></li>"
            for name in names
        ) or "<li>No saved logins yet.</li>"
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Saved logins</title>"
            "<h1>احفظ الدخول — Saved logins</h1><p><a href='/browser'>Back to browser</a></p>"
            "<p>This stores the browser's cookies and site session for the sites you were logged "
            "into, encrypted on the server. Your actual passwords are never stored.</p>"
            "<h2>Save the currently open browser's login</h2>"
            "<form method=post action=/api/auth-profiles>"
            f"<input type=hidden name=csrf_token value='{csrf}'>"
            "<label>Name this login <input name=profile_name placeholder='e.g. facebook' required "
            "pattern='[A-Za-z0-9_.-]{1,120}'></label>" + code + "<button>احفظ الدخول</button></form>"
            "<h2>Saved logins</h2><ul>" + items + "</ul>"
            "<p>To open the browser already signed in, go back to the browser page and type the "
            "saved login's name in the \"Saved login\" field before pressing Open.</p>"
        )

    @app.get("/api/auth-profiles")
    async def list_auth_profiles_api(request: Request):
        row = session_for(request)
        require_sole_new_surface_owner(row)
        return {"profiles": owned_profile_names(row)}

    @app.post("/api/auth-profiles")
    async def save_auth_profile_api(request: Request):
        row = await mutation(request)
        data = await _payload(request)
        name = _required_text(data, "profile_name", 120)
        if not PROFILE_NAME_PATTERN.fullmatch(name):
            raise HTTPException(422, "Names may contain letters, numbers, dots, underscores, and hyphens")
        require_sole_new_surface_owner(row)
        await require_fresh_site_change(request, row, data, action="auth_profile_save")
        with closing(store.connect()) as db:
            owner = db.execute(
                "SELECT broker_session_id FROM browser_ownership_v2 WHERE user_id=? AND tenant_id=?",
                (row["user_id"], row["tenant_id"]),
            ).fetchone()
        if owner is None or not owner["broker_session_id"]:
            raise HTTPException(409, "Open the browser before saving its login")
        claim_profile_name(row, name)
        selected_broker, selected_owner_token = broker_for(row["user_id"], row["tenant_id"])
        try:
            saved = await selected_broker.post(
                f"{BROKER_AUTH_PROFILE_SAVE_PREFIX}{owner['broker_session_id']}/auth-profiles",
                headers=internal_headers(selected_owner_token), json={"profile_name": name},
            )
        except httpx.HTTPError:
            drop_profile_claim(row, name)
            raise HTTPException(502, "Browser service unavailable") from None
        if saved.status_code not in (200, 201):
            drop_profile_claim(row, name)
            audit(row=row, action="auth_profile_save", hostname=None, outcome="failed")
            raise _upstream_error(saved, "Login could not be saved")
        audit(row=row, action="auth_profile_save", hostname=None, outcome="saved")
        return {"status": "saved", "profile_name": name}

    async def _delete_auth_profile(name: str, request: Request) -> dict[str, Any]:
        row = await mutation(request)
        require_sole_new_surface_owner(row)
        if not PROFILE_NAME_PATTERN.fullmatch(name) or not owns_profile(row, name):
            raise HTTPException(404, "Saved login not found")
        data = await _payload(request)
        await require_fresh_site_change(request, row, data, action="auth_profile_delete")
        selected_broker, selected_owner_token = broker_for(row["user_id"], row["tenant_id"])
        try:
            deleted = await selected_broker.delete(
                f"{BROKER_AUTH_PROFILE_PREFIX}{name}", headers=internal_headers(selected_owner_token),
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Browser service unavailable") from None
        if deleted.status_code not in (200, 204, 404):
            audit(row=row, action="auth_profile_delete", hostname=None, outcome="failed")
            raise _upstream_error(deleted, "Saved login could not be deleted")
        drop_profile_claim(row, name)
        audit(row=row, action="auth_profile_delete", hostname=None, outcome="deleted")
        return {"status": "deleted", "profile_name": name}

    @app.delete("/api/auth-profiles/{name}")
    async def delete_auth_profile_api(name: str, request: Request):
        return await _delete_auth_profile(name, request)

    @app.post("/api/auth-profiles/{name}/delete")
    async def delete_auth_profile_form(name: str, request: Request):
        return await _delete_auth_profile(name, request)

    @app.post("/api/auth-profiles/{name}/rename")
    async def rename_auth_profile_api(name: str, request: Request):
        row = await mutation(request)
        require_sole_new_surface_owner(row)
        if not PROFILE_NAME_PATTERN.fullmatch(name) or not owns_profile(row, name):
            raise HTTPException(404, "Saved login not found")
        data = await _payload(request)
        new_name = _required_text(data, "new_name", 120)
        if not PROFILE_NAME_PATTERN.fullmatch(new_name):
            raise HTTPException(422, "Names may contain letters, numbers, dots, underscores, and hyphens")
        if new_name == name:
            raise HTTPException(422, "New name must differ from the current name")
        await require_fresh_site_change(request, row, data, action="auth_profile_rename")
        selected_broker, selected_owner_token = broker_for(row["user_id"], row["tenant_id"])
        try:
            renamed = await selected_broker.post(
                f"{BROKER_AUTH_PROFILE_PREFIX}{name}/rename",
                headers=internal_headers(selected_owner_token), json={"new_name": new_name},
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Browser service unavailable") from None
        if renamed.status_code not in (200, 201):
            audit(row=row, action="auth_profile_rename", hostname=None, outcome="failed")
            raise _upstream_error(renamed, "Saved login could not be renamed")
        rename_profile_claim(row, name, new_name)
        audit(row=row, action="auth_profile_rename", hostname=None, outcome="renamed")
        return {"status": "renamed", "profile_name": new_name}

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
            f"<form method=post action=/enroll><input type=hidden name=invitation_token value='{escaped}'>"
            "<label>Display name <input name=display_name required></label>"
            "<label>Recovery email <input name=recovery_email type=email></label>"
            "<button>Continue</button></form>"
        )

    async def redeem_payload(data: Mapping[str, Any]) -> dict[str, Any]:
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
            raise _upstream_error(response, "Invitation could not be redeemed")
        return _safe_enrollment(response.json())

    @app.post("/enroll")
    async def enroll_page(request: Request):
        await mutation(request, require_session=False)
        enrollment = await redeem_payload(await _payload(request))
        enrollment_id = enrollment.get("enrollment_id")
        provisioning_uri = enrollment.get("provisioning_uri")
        if (not isinstance(enrollment_id, str) or not enrollment_id
                or not isinstance(provisioning_uri, str)
                or not provisioning_uri.startswith("otpauth://totp/")):
            raise HTTPException(502, "Invalid identity enrollment response")
        escaped_id = html.escape(enrollment_id, quote=True)
        escaped_uri = html.escape(provisioning_uri, quote=True)
        qr_svg = _otpauth_qr_svg(provisioning_uri)
        manual_secret = _totp_manual_secret(provisioning_uri, enrollment)
        manual_secret_html = (
            "<details><summary>اضغط لإظهار المفتاح</summary>"
            f"<p>Manual entry key: <code>{html.escape(manual_secret)}</code></p></details>"
            if manual_secret else ""
        )
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Authenticator enrollment</title>"
            "<h1>Add your authenticator</h1>"
            "<p>Scan this with your authenticator app (Google Authenticator or similar).</p>"
            f"<p>{qr_svg}</p>"
            f"<p><a href='{escaped_uri}'>Open in your authenticator app</a> (same device only)</p>"
            f"{manual_secret_html}"
            "<form method=post action=/enroll/confirm>"
            f"<input type=hidden name=enrollment_id value='{escaped_id}'>"
            "<label>Authenticator code <input name=totp_code inputmode=numeric "
            "autocomplete=one-time-code pattern='[0-9]{6}' required></label>"
            "<button>Confirm enrollment</button></form>"
        )

    @app.post("/enroll/confirm")
    async def enroll_confirm_page(request: Request):
        await mutation(request, require_session=False)
        data = await _payload(request)
        response = await identity_http.post(
            IDENTITY_ENROLLMENT_CONFIRM_PATH,
            json={"enrollment_id": _required_text(data, "enrollment_id"), "totp_code": _totp(data)},
        )
        if response.status_code != 200:
            raise _upstream_error(response, "Enrollment could not be confirmed")
        enrollment = _safe_enrollment(response.json())
        recovery_codes = enrollment.get("recovery_codes")
        if not isinstance(recovery_codes, list) or any(not isinstance(code, str) for code in recovery_codes):
            raise HTTPException(502, "Invalid identity enrollment response")
        items = "".join(f"<li><code>{html.escape(code)}</code></li>" for code in recovery_codes)
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Enrollment complete</title>"
            "<h1>Enrollment complete</h1>"
            "<p>Save these one-time recovery codes now. They will not be shown again.</p>"
            f"<ul>{items}</ul><p><a href='/signin'>Continue to sign in</a></p>"
        )

    @app.post("/api/invitations/redeem")
    async def redeem_invitation(request: Request):
        await mutation(request, require_session=False)
        return await redeem_payload(await _payload(request))

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
    broker_resolver = None
    policy_resolver = None
    if os.environ.get("TENANT_STACK_ROOT"):
        from tenant_stacks import TenantBrokerRegistry, TenantPolicyRegistry

        broker_resolver = TenantBrokerRegistry(os.environ["TENANT_STACK_ROOT"], "portal")
        policy_resolver = TenantPolicyRegistry(os.environ["TENANT_STACK_ROOT"])
    return create_app(
        state_root=os.environ["PORTAL_STATE_ROOT"],
        identity_internal_token=os.environ["IDENTITY_INTERNAL_TOKEN"],
        broker_owner_token=os.environ.get("BROKER_OWNER_TOKEN"),
        portal_assertion_private_key=os.environ["PORTAL_ASSERTION_PRIVATE_KEY"],
        gateway_internal_token=os.environ["MCP_GATEWAY_INTERNAL_TOKEN"],
        public_origin=os.environ["PORTAL_PUBLIC_ORIGIN"],
        identity_base_url=os.environ.get("PORTAL_IDENTITY_URL", "http://identity"),
        broker_base_url=os.environ.get("PORTAL_BROKER_URL", "http://approval-broker:18001"),
        broker_resolver=broker_resolver,
        gateway_base_url=os.environ.get("PORTAL_GATEWAY_URL", "http://mcp-gateway"),
        policy_resolver=policy_resolver,
        tenant_policy_internal_token=os.environ.get("TENANT_POLICY_INTERNAL_TOKEN"),
        tenant_policy_base_url=os.environ.get(
            "PORTAL_TENANT_POLICY_URL", "http://tenant-policy:18005"
        ),
        authentication_freshness_ttl=int(
            os.environ.get("PORTAL_AUTHENTICATION_FRESHNESS_SECONDS", "120")
        ),
        # Single-owner deployments can keep a sign-in (and its authenticator
        # freshness) for days instead of re-asking for a code every few minutes.
        absolute_session_ttl=int(os.environ.get("PORTAL_ABSOLUTE_SESSION_SECONDS", str(12 * 60 * 60))),
        idle_session_ttl=int(os.environ.get("PORTAL_IDLE_SESSION_SECONDS", str(30 * 60))),
    )


app = app_from_environment() if os.environ.get("PORTAL_STATE_ROOT") else None

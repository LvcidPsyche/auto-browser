"""OAuth 2.0 public-client gateway for the private Auto Browser MCP broker.

The public process owns OAuth state and the only broker credential.  Browser
identity is always derived from the access token; MCP arguments can never
select a user, tenant, profile, session, connection, or upstream grant.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ACCESS_TTL = 900
REFRESH_TTL = 30 * 24 * 60 * 60
CODE_TTL = 120
AUTH_REQUEST_TTL = 600
ALLOWED_ACTIONS = ("click", "navigate", "observe", "press", "scroll", "type", "wait")
FORBIDDEN_ID_KEYS = frozenset({
    "user", "user_id", "userid", "tenant", "tenant_id", "tenantid",
    "profile", "profile_id", "profileid", "session", "session_id", "sessionid",
    "grant", "grant_id", "grantid", "connection", "connection_id", "connectionid",
})
ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,160}$")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _random() -> str:
    return secrets.token_urlsafe(48)


def _origin_url(value: str, label: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} must be a public HTTPS origin") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError(f"{label} must be a public HTTPS origin")
    host = f"[{parsed.hostname.lower()}]" if ":" in parsed.hostname else parsed.hostname.lower()
    return f"https://{host}" + (f":{port}" if port is not None else "")


def _mcp_resource(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("resource_url must be the canonical HTTPS MCP URL") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.query or parsed.path != "/mcp"):
        raise ValueError("resource_url must be the canonical HTTPS MCP URL ending in /mcp")
    host = f"[{parsed.hostname.lower()}]" if ":" in parsed.hostname else parsed.hostname.lower()
    return urlunsplit(("https", host + (f":{parsed.port}" if parsed.port is not None else ""), "/mcp", "", ""))


def _clean_name(value: Any) -> str:
    if not isinstance(value, str):
        raise HTTPException(400, "client_name must be a string")
    cleaned = " ".join("".join(ch for ch in value if ch.isprintable() and ch not in "<>\r\n\t").split())
    if not cleaned:
        raise HTTPException(400, "client_name is required")
    return cleaned[:100]


def _redirect_ok(uri: str, application_type: str) -> bool:
    try:
        parsed = urlsplit(uri)
        _ = parsed.port
    except (TypeError, ValueError):
        return False
    if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment
            or parsed.scheme not in ("https", "http")):
        return False
    if parsed.scheme == "https":
        return True
    return application_type == "native" and parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}


def _safe_identity(value: str, label: str) -> str:
    if not ID_RE.fullmatch(value):
        raise HTTPException(422, f"Invalid {label}")
    return value


def _contains_forbidden_id(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in FORBIDDEN_ID_KEYS or _contains_forbidden_id(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_id(child) for child in value)
    return False


def _redact_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_ids(child) for key, child in value.items()
            if str(key).lower().replace("-", "_") not in FORBIDDEN_ID_KEYS
        }
    if isinstance(value, list):
        return [_redact_ids(child) for child in value]
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConsentRequest(StrictModel):
    authorization_request: str = Field(min_length=20, max_length=200)
    user_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=160)
    approve: bool


class ConsentPreviewRequest(StrictModel):
    authorization_request: str = Field(min_length=20, max_length=200)
    user_id: str | None = Field(default=None, min_length=1, max_length=160)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=160)


class ActiveUserRequest(StrictModel):
    user_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=160)
    active: bool = True


class DisconnectRequest(StrictModel):
    user_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=160)


class McpRequest(StrictModel):
    jsonrpc: Literal["2.0"]
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_absolute() or not self.path.parent.is_dir() or self.path.is_symlink():
            raise ValueError("OAuth database requires an absolute path in an existing private directory")
        if os.name == "posix" and self.path.parent.stat().st_mode & 0o077:
            raise ValueError("OAuth database directory must be private (0700)")
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        if os.name == "posix" and self.path.stat().st_mode & 0o077:
            raise ValueError("OAuth database must be private (0600)")
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY, client_name TEXT NOT NULL,
                    application_type TEXT NOT NULL, redirect_uris TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_requests (
                    request_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL, state TEXT, code_challenge TEXT NOT NULL,
                    scope TEXT NOT NULL, expires_at REAL NOT NULL, used_at REAL,
                    FOREIGN KEY(client_id) REFERENCES clients(client_id)
                );
                CREATE TABLE IF NOT EXISTS grants (
                    grant_id TEXT PRIMARY KEY, connection_ref TEXT UNIQUE NOT NULL,
                    client_id TEXT NOT NULL, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    created_at REAL NOT NULL, revoked_at REAL,
                    FOREIGN KEY(client_id) REFERENCES clients(client_id)
                );
                CREATE TABLE IF NOT EXISTS authorization_codes (
                    code_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL, client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL, code_challenge TEXT NOT NULL,
                    expires_at REAL NOT NULL, used_at REAL,
                    FOREIGN KEY(grant_id) REFERENCES grants(grant_id)
                );
                CREATE TABLE IF NOT EXISTS token_families (
                    family_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, revoked_at REAL,
                    FOREIGN KEY(grant_id) REFERENCES grants(grant_id)
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, family_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL, client_id TEXT NOT NULL, expires_at REAL NOT NULL,
                    used_at REAL, revoked_at REAL,
                    FOREIGN KEY(family_id) REFERENCES token_families(family_id),
                    FOREIGN KEY(grant_id) REFERENCES grants(grant_id)
                );
                CREATE TABLE IF NOT EXISTS broker_mappings (
                    public_ref TEXT PRIMARY KEY, upstream_ref TEXT NOT NULL,
                    grant_id TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL,
                    FOREIGN KEY(grant_id) REFERENCES grants(grant_id)
                );
                CREATE TABLE IF NOT EXISTS runtime_binding (
                    user_id TEXT NOT NULL, tenant_id TEXT NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(user_id, tenant_id)
                );
            """)
            runtime_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(runtime_binding)")
            }
            if "singleton" in runtime_columns:
                db.executescript("""
                    ALTER TABLE runtime_binding RENAME TO runtime_binding_singleton;
                    CREATE TABLE runtime_binding (
                        user_id TEXT NOT NULL, tenant_id TEXT NOT NULL, updated_at REAL NOT NULL,
                        PRIMARY KEY(user_id, tenant_id)
                    );
                    INSERT OR IGNORE INTO runtime_binding(user_id,tenant_id,updated_at)
                        SELECT user_id,tenant_id,updated_at FROM runtime_binding_singleton;
                    DROP TABLE runtime_binding_singleton;
                """)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        return db


def create_app(
    *,
    issuer_url: str,
    resource_url: str,
    portal_url: str,
    internal_token: str,
    broker_token: str | None,
    database_path: str | Path,
    broker_url: str = "http://approval-broker:18001",
    broker_transport: httpx.AsyncBaseTransport | None = None,
    broker_client: httpx.AsyncClient | None = None,
    broker_resolver: Callable[[str, str], tuple[httpx.AsyncClient, str]] | None = None,
    clock: Any = time.time,
) -> FastAPI:
    issuer = _origin_url(issuer_url, "issuer_url")
    resource = _mcp_resource(resource_url)
    portal = _origin_url(portal_url, "portal_url")
    if len(internal_token) < 32:
        raise ValueError("Internal credential must have at least 32 characters")
    if broker_resolver is None and (
        not broker_token or len(broker_token) < 32 or secrets.compare_digest(internal_token, broker_token)
    ):
        raise ValueError("Internal and broker credentials must be distinct and at least 32 characters")
    if broker_client is not None and broker_transport is not None:
        raise ValueError("Inject broker_client or broker_transport, not both")
    store = Store(database_path)
    owns_client = broker_resolver is None and broker_client is None
    client = broker_client
    if client is None and broker_resolver is None:
        client = httpx.AsyncClient(
            base_url=broker_url, transport=broker_transport, timeout=20, follow_redirects=False,
        )
    resource_metadata = f"{issuer}/.well-known/oauth-protected-resource"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            if owns_client and client is not None:
                await client.aclose()
            close_resolver = getattr(broker_resolver, "aclose", None)
            if close_resolver is not None:
                await close_resolver()

    app = FastAPI(title="Auto Browser OAuth MCP gateway", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    def oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": error, "error_description": description}, status_code=status,
                            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    def require_internal(authorization: str | None) -> None:
        if not authorization or not authorization.startswith("Bearer ") or not secrets.compare_digest(
                authorization[7:], internal_token):
            raise HTTPException(401, "Internal bearer required")

    def bind_active(db: sqlite3.Connection, user_id: str, tenant_id: str) -> None:
        db.execute(
            "INSERT OR REPLACE INTO runtime_binding(user_id,tenant_id,updated_at) VALUES(?,?,?)",
            (user_id, tenant_id, clock()),
        )

    def broker_for(user_id: str, tenant_id: str) -> tuple[httpx.AsyncClient, str]:
        if broker_resolver is not None:
            try:
                return broker_resolver(user_id, tenant_id)
            except LookupError:
                raise HTTPException(403, "No browser stack is bound to this identity") from None
        assert client is not None and broker_token is not None
        return client, broker_token

    def load_access(authorization: str | None) -> sqlite3.Row:
        challenge = f'Bearer resource_metadata="{resource_metadata}"'
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer access token required", headers={"WWW-Authenticate": challenge})
        with closing(store.connect()) as db:
            row = db.execute("""
                SELECT t.*, g.user_id, g.tenant_id, g.connection_ref, g.revoked_at AS grant_revoked,
                       f.revoked_at AS family_revoked
                FROM tokens t JOIN grants g ON g.grant_id=t.grant_id
                JOIN token_families f ON f.family_id=t.family_id
                WHERE t.token_hash=? AND t.kind='access'
            """, (_digest(authorization[7:]),)).fetchone()
        if (not row or row["expires_at"] <= clock() or row["revoked_at"] is not None
                or row["grant_revoked"] is not None or row["family_revoked"] is not None):
            raise HTTPException(401, "Invalid or expired access token", headers={"WWW-Authenticate": challenge})
        return row

    async def form_data(request: Request) -> dict[str, str]:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/x-www-form-urlencoded":
            raise HTTPException(415, "application/x-www-form-urlencoded required")
        raw = (await request.body()).decode("utf-8", "strict")
        parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=False)
        if any(len(values) != 1 for values in parsed.values()):
            raise HTTPException(400, "Duplicate form parameter")
        return {key: values[0] for key, values in parsed.items()}

    @app.get("/.well-known/oauth-authorization-server")
    async def authorization_metadata():
        return {
            "issuer": issuer, "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token", "registration_endpoint": f"{issuer}/register",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["browser"], "authorization_response_iss_parameter_supported": True,
        }

    @app.get("/.well-known/oauth-protected-resource")
    async def protected_metadata():
        return {"resource": resource, "authorization_servers": [issuer], "scopes_supported": ["browser"]}

    @app.post("/register")
    async def register(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return oauth_error("invalid_client_metadata", "JSON body required")
        if not isinstance(body, dict):
            return oauth_error("invalid_client_metadata", "Object body required")
        allowed = {"client_name", "redirect_uris", "application_type", "token_endpoint_auth_method",
                   "grant_types", "response_types"}
        if set(body) - allowed:
            return oauth_error("invalid_client_metadata", "Unsupported client metadata")
        try:
            name = _clean_name(body.get("client_name"))
        except HTTPException as exc:
            return oauth_error("invalid_client_metadata", str(exc.detail))
        application_type = body.get("application_type", "web")
        redirects = body.get("redirect_uris")
        if application_type not in {"web", "native"} or not isinstance(redirects, list) or not redirects:
            return oauth_error("invalid_client_metadata", "application_type and redirect_uris are required")
        if len(redirects) > 10 or len(set(redirects)) != len(redirects) or not all(
                isinstance(uri, str) and len(uri) <= 2000 and _redirect_ok(uri, application_type) for uri in redirects):
            return oauth_error("invalid_redirect_uri", "Redirect URIs must be exact HTTPS URLs or native loopback HTTP URLs")
        if body.get("token_endpoint_auth_method", "none") != "none":
            return oauth_error("invalid_client_metadata", "Only public clients are supported")
        if body.get("grant_types", ["authorization_code", "refresh_token"]) != ["authorization_code", "refresh_token"]:
            return oauth_error("invalid_client_metadata", "Unsupported grant_types")
        if body.get("response_types", ["code"]) != ["code"]:
            return oauth_error("invalid_client_metadata", "Unsupported response_types")
        client_id = _random()
        with closing(store.connect()) as db:
            db.execute("INSERT INTO clients VALUES (?,?,?,?,?)",
                       (client_id, name, application_type, json.dumps(redirects), clock()))
        return JSONResponse({
            "client_id": client_id, "client_name": name, "application_type": application_type,
            "redirect_uris": redirects, "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        }, status_code=201, headers={"Cache-Control": "no-store"})

    @app.get("/authorize")
    async def authorize(
        response_type: str = Query(...), client_id: str = Query(...), redirect_uri: str = Query(...),
        code_challenge: str = Query(...), code_challenge_method: str = Query(...),
        resource_parameter: str = Query(..., alias="resource"),
        state: str | None = Query(default=None), scope: str = Query(default="browser"),
    ):
        with closing(store.connect()) as db:
            registered = db.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
            if not registered or redirect_uri not in json.loads(registered["redirect_uris"]):
                raise HTTPException(400, "Unknown client or redirect URI")
            if (response_type != "code" or code_challenge_method != "S256" or scope != "browser"
                    or resource_parameter != resource
                    or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge)):
                return RedirectResponse(redirect_uri + ("&" if "?" in redirect_uri else "?") + urlencode({
                    "error": "invalid_request", "iss": issuer,
                    **({"state": state} if state is not None else {})
                }), status_code=302)
            request_secret = _random()
            db.execute("INSERT INTO auth_requests VALUES (?,?,?,?,?,?,?,NULL)", (
                _digest(request_secret), client_id, redirect_uri, state, code_challenge, scope,
                clock() + AUTH_REQUEST_TTL,
            ))
        portal_query = urlencode({"authorization_request": request_secret})
        return RedirectResponse(f"{portal}/oauth/authorize?{portal_query}", status_code=302,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.post("/internal/active-user")
    async def active_user(payload: ActiveUserRequest, authorization: str | None = Header(default=None)):
        require_internal(authorization)
        user_id, tenant_id = _safe_identity(payload.user_id, "user_id"), _safe_identity(payload.tenant_id, "tenant_id")
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if payload.active:
                bind_active(db, user_id, tenant_id)
            else:
                row = db.execute(
                    "SELECT 1 FROM runtime_binding WHERE user_id=? AND tenant_id=?",
                    (user_id, tenant_id),
                ).fetchone()
                if not row:
                    raise HTTPException(404, "Active binding not found")
                db.execute(
                    "DELETE FROM runtime_binding WHERE user_id=? AND tenant_id=?",
                    (user_id, tenant_id),
                )
            db.commit()
        return {"active": payload.active}

    @app.post("/internal/consent")
    async def consent(payload: ConsentRequest, authorization: str | None = Header(default=None)):
        require_internal(authorization)
        user_id, tenant_id = _safe_identity(payload.user_id, "user_id"), _safe_identity(payload.tenant_id, "tenant_id")
        now = clock()
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("""SELECT a.*, c.client_name FROM auth_requests a JOIN clients c
                                  ON c.client_id=a.client_id WHERE request_hash=?""",
                                 (_digest(payload.authorization_request),)).fetchone()
            if not pending or pending["used_at"] is not None or pending["expires_at"] <= now:
                raise HTTPException(400, "Invalid or expired authorization request")
            db.execute("UPDATE auth_requests SET used_at=? WHERE request_hash=?", (now, pending["request_hash"]))
            params: dict[str, str] = {}
            params["iss"] = issuer
            if pending["state"] is not None:
                params["state"] = pending["state"]
            if payload.approve:
                grant_id, connection_ref, code = _random(), _random(), _random()
                db.execute("INSERT INTO grants VALUES (?,?,?,?,?,?,NULL)",
                           (grant_id, connection_ref, pending["client_id"], user_id, tenant_id, now))
                db.execute("INSERT INTO authorization_codes VALUES (?,?,?,?,?,?,NULL)", (
                    _digest(code), grant_id, pending["client_id"], pending["redirect_uri"],
                    pending["code_challenge"], now + CODE_TTL,
                ))
                params["code"] = code
            else:
                params["error"] = "access_denied"
            db.commit()
        separator = "&" if "?" in pending["redirect_uri"] else "?"
        return {"redirect_url": pending["redirect_uri"] + separator + urlencode(params)}

    @app.post("/internal/consent/preview")
    async def consent_preview(payload: ConsentPreviewRequest,
                              authorization: str | None = Header(default=None)):
        require_internal(authorization)
        if payload.user_id is not None:
            _safe_identity(payload.user_id, "user_id")
        if payload.tenant_id is not None:
            _safe_identity(payload.tenant_id, "tenant_id")
        with closing(store.connect()) as db:
            pending = db.execute("""SELECT a.client_id,c.client_name FROM auth_requests a
                                  JOIN clients c ON c.client_id=a.client_id
                                  WHERE a.request_hash=? AND a.used_at IS NULL AND a.expires_at>?""",
                                 (_digest(payload.authorization_request), clock())).fetchone()
        if not pending:
            raise HTTPException(400, "Invalid or expired authorization request")
        return {
            "client_id": pending["client_id"],
            "client_name": pending["client_name"],
            "capabilities": ["Browser status", "Ordinary browser navigation and interaction"],
        }

    def mint_pair(db: sqlite3.Connection, grant_id: str, client_id: str, family_id: str | None = None) -> dict[str, Any]:
        now = clock()
        family_id = family_id or _random()
        if not db.execute("SELECT 1 FROM token_families WHERE family_id=?", (family_id,)).fetchone():
            db.execute("INSERT INTO token_families VALUES (?,?,NULL)", (family_id, grant_id))
        access, refresh = _random(), _random()
        db.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,NULL,NULL)",
                   (_digest(access), "access", family_id, grant_id, client_id, now + ACCESS_TTL))
        db.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,NULL,NULL)",
                   (_digest(refresh), "refresh", family_id, grant_id, client_id, now + REFRESH_TTL))
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                "refresh_token": refresh, "scope": "browser"}

    @app.post("/token")
    async def token(request: Request):
        try:
            form = await form_data(request)
        except (HTTPException, UnicodeDecodeError) as exc:
            return oauth_error("invalid_request", str(getattr(exc, "detail", "Malformed form")),
                               getattr(exc, "status_code", 400))
        grant_type, client_id = form.get("grant_type"), form.get("client_id")
        if not client_id:
            return oauth_error("invalid_request", "client_id is required")
        if form.get("resource") != resource:
            return oauth_error("invalid_target", "The canonical MCP resource parameter is required")
        now = clock()
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM clients WHERE client_id=?", (client_id,)).fetchone():
                return oauth_error("invalid_client", "Unknown public client", 401)
            if grant_type == "authorization_code":
                if set(form) - {"grant_type", "client_id", "code", "redirect_uri", "code_verifier", "resource"}:
                    return oauth_error("invalid_request", "Unsupported token parameter")
                row = db.execute("""SELECT c.*,g.revoked_at AS grant_revoked FROM authorization_codes c
                                  JOIN grants g ON g.grant_id=c.grant_id WHERE c.code_hash=?""",
                                 (_digest(form.get("code", "")),)).fetchone()
                verifier = form.get("code_verifier", "")
                expected = secrets.token_urlsafe(32)  # fixed-length dummy for timing on unknown codes
                if row:
                    expected = row["code_challenge"]
                import base64
                actual = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                valid = bool(row and row["client_id"] == client_id and row["redirect_uri"] == form.get("redirect_uri")
                             and row["used_at"] is None and row["expires_at"] > now and row["grant_revoked"] is None
                             and re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
                             and secrets.compare_digest(expected, actual))
                if not valid:
                    return oauth_error("invalid_grant", "Invalid authorization code, redirect URI, or verifier")
                db.execute("UPDATE authorization_codes SET used_at=? WHERE code_hash=?", (now, row["code_hash"]))
                result = mint_pair(db, row["grant_id"], client_id)
                db.commit()
                return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
            if grant_type == "refresh_token":
                if set(form) - {"grant_type", "client_id", "refresh_token", "resource"}:
                    return oauth_error("invalid_request", "Unsupported token parameter")
                row = db.execute("""SELECT t.*,g.revoked_at AS grant_revoked,f.revoked_at AS family_revoked
                                  FROM tokens t JOIN grants g ON g.grant_id=t.grant_id
                                  JOIN token_families f ON f.family_id=t.family_id
                                  WHERE t.token_hash=? AND t.kind='refresh'""",
                                 (_digest(form.get("refresh_token", "")),)).fetchone()
                if row and row["used_at"] is not None:
                    db.execute("UPDATE token_families SET revoked_at=? WHERE family_id=? AND revoked_at IS NULL",
                               (now, row["family_id"]))
                    db.commit()
                    return oauth_error("invalid_grant", "Refresh token replay revoked this token family")
                if (not row or row["client_id"] != client_id or row["expires_at"] <= now
                        or row["revoked_at"] is not None or row["grant_revoked"] is not None
                        or row["family_revoked"] is not None):
                    return oauth_error("invalid_grant", "Invalid refresh token")
                db.execute("UPDATE tokens SET used_at=? WHERE token_hash=?", (now, row["token_hash"]))
                result = mint_pair(db, row["grant_id"], client_id, row["family_id"])
                db.commit()
                return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
            return oauth_error("unsupported_grant_type", "Unsupported grant_type")

    @app.get("/internal/connected-clients")
    async def connected_clients(user_id: str, tenant_id: str, authorization: str | None = Header(default=None)):
        require_internal(authorization)
        user_id, tenant_id = _safe_identity(user_id, "user_id"), _safe_identity(tenant_id, "tenant_id")
        with closing(store.connect()) as db:
            rows = db.execute("""SELECT g.connection_ref,c.client_id,c.client_name,c.application_type,g.created_at
                               FROM grants g JOIN clients c ON c.client_id=g.client_id
                               WHERE g.user_id=? AND g.tenant_id=? AND g.revoked_at IS NULL
                               ORDER BY g.created_at""", (user_id, tenant_id)).fetchall()
        return {"connections": [dict(row) for row in rows]}

    @app.post("/internal/connected-clients/{connection_ref}/disconnect")
    async def disconnect(connection_ref: str, payload: DisconnectRequest,
                         authorization: str | None = Header(default=None)):
        require_internal(authorization)
        user_id, tenant_id = _safe_identity(payload.user_id, "user_id"), _safe_identity(payload.tenant_id, "tenant_id")
        now = clock()
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            grant = db.execute("SELECT grant_id FROM grants WHERE connection_ref=? AND user_id=? AND tenant_id=?",
                               (connection_ref, user_id, tenant_id)).fetchone()
            if not grant:
                raise HTTPException(404, "Connection not found")
            db.execute("UPDATE grants SET revoked_at=COALESCE(revoked_at,?) WHERE grant_id=?", (now, grant["grant_id"]))
            db.execute("UPDATE authorization_codes SET used_at=COALESCE(used_at,?) WHERE grant_id=?", (now, grant["grant_id"]))
            db.execute("UPDATE token_families SET revoked_at=COALESCE(revoked_at,?) WHERE grant_id=?", (now, grant["grant_id"]))
            db.execute("UPDATE tokens SET revoked_at=COALESCE(revoked_at,?) WHERE grant_id=?", (now, grant["grant_id"]))
            db.execute("UPDATE broker_mappings SET revoked_at=COALESCE(revoked_at,?) WHERE grant_id=?", (now, grant["grant_id"]))
            db.commit()
        return {"disconnected": True}

    async def broker_call(
        user_id: str, tenant_id: str, name: str, arguments: dict[str, Any]
    ) -> Any:
        payload = {"jsonrpc": "2.0", "id": secrets.token_hex(8), "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        selected_broker, selected_token = broker_for(user_id, tenant_id)
        try:
            response = await selected_broker.post(
                "/mcp", json=payload, headers={"Authorization": f"Bearer {selected_token}"}
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Private browser broker unavailable") from None
        if response.status_code >= 400:
            raise HTTPException(502 if response.status_code >= 500 else response.status_code,
                                "Private browser broker rejected the operation")
        try:
            result = response.json()["result"]
            text = result["content"][0]["text"]
            return json.loads(text)
        except (ValueError, KeyError, IndexError, TypeError):
            raise HTTPException(502, "Invalid private browser broker response") from None

    def resolve_mapping(grant_id: str, public_ref: str) -> str:
        with closing(store.connect()) as db:
            row = db.execute("""SELECT upstream_ref FROM broker_mappings
                              WHERE public_ref=? AND grant_id=? AND revoked_at IS NULL""",
                             (public_ref, grant_id)).fetchone()
        if not row:
            raise HTTPException(404, "Unknown request reference")
        return row["upstream_ref"]

    async def invoke_tool(token_row: sqlite3.Row, name: str, arguments: dict[str, Any]) -> Any:
        if _contains_forbidden_id(arguments):
            raise HTTPException(400, "Identity and browser ownership selectors are not accepted")
        grant_id = token_row["grant_id"]
        user_id, tenant_id = token_row["user_id"], token_row["tenant_id"]
        if name == "browser.session_status":
            if arguments:
                raise HTTPException(400, "session_status accepts no arguments")
            with closing(store.connect()) as db:
                binding = db.execute(
                    "SELECT 1 FROM runtime_binding WHERE user_id=? AND tenant_id=?",
                    (user_id, tenant_id),
                ).fetchone()
            if not binding:
                state = "session_closed"
            else:
                status = _redact_ids(await broker_call(user_id, tenant_id, name, {}))
                state = status.get("status", "unknown") if isinstance(status, dict) else "unknown"
            return {"state": state, "portal_url": f"{portal}/browser?" + urlencode({
                "connection": token_row["connection_ref"]
            })}
        with closing(store.connect()) as db:
            binding = db.execute(
                "SELECT 1 FROM runtime_binding WHERE user_id=? AND tenant_id=?",
                (user_id, tenant_id),
            ).fetchone()
        if not binding:
            raise HTTPException(403, "This connection has no active browser stack")
        if name == "browser.request_access":
            if set(arguments) != {"purpose"} or not isinstance(arguments.get("purpose"), str):
                raise HTTPException(400, "A purpose string is required")
            result = await broker_call(user_id, tenant_id, name, arguments)
            upstream_ref = result.get("id") if isinstance(result, dict) else None
            if not isinstance(upstream_ref, str):
                raise HTTPException(502, "Broker did not return an access reference")
            with closing(store.connect()) as db:
                existing = db.execute("SELECT public_ref FROM broker_mappings WHERE upstream_ref=? AND grant_id=? AND revoked_at IS NULL",
                                      (upstream_ref, grant_id)).fetchone()
                public_ref = existing["public_ref"] if existing else _random()
                if not existing:
                    db.execute("INSERT INTO broker_mappings VALUES (?,?,?,?,NULL)",
                               (public_ref, upstream_ref, grant_id, clock()))
            safe = _redact_ids(result)
            if isinstance(safe, dict):
                safe.pop("id", None)
                safe["request_id"] = public_ref
            return safe
        if name not in {"browser.get_request", "browser.complete", *(f"browser.{x}" for x in ALLOWED_ACTIONS)}:
            raise HTTPException(404, "Tool unavailable")
        if not isinstance(arguments.get("request_id"), str):
            raise HTTPException(400, "A gateway request_id is required")
        public_ref = arguments["request_id"]
        upstream_ref = resolve_mapping(grant_id, public_ref)
        if name in {"browser.get_request", "browser.complete"}:
            if set(arguments) != {"request_id"}:
                raise HTTPException(400, "Unsupported tool arguments")
            broker_arguments = {"request_id": upstream_ref}
        else:
            if set(arguments) - {"request_id", "arguments"} or not isinstance(arguments.get("arguments", {}), dict):
                raise HTTPException(400, "Unsupported tool arguments")
            if _contains_forbidden_id(arguments.get("arguments", {})):
                raise HTTPException(400, "Identity and browser ownership selectors are not accepted")
            broker_arguments = {"request_id": upstream_ref, "arguments": arguments.get("arguments", {})}
        result = _redact_ids(await broker_call(user_id, tenant_id, name, broker_arguments))
        if isinstance(result, dict):
            result.pop("id", None)
            result["request_id"] = public_ref
        return result

    def tools() -> list[dict[str, Any]]:
        result = [{"name": "browser.session_status", "description": "Show safe browser readiness and the human portal link.",
                   "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
                  {"name": "browser.request_access", "description": "Request access for an ordinary browser task.",
                   "inputSchema": {"type": "object", "properties": {"purpose": {"type": "string"}},
                                   "required": ["purpose"], "additionalProperties": False}}]
        for name in ("get_request", "complete", *ALLOWED_ACTIONS):
            properties: dict[str, Any] = {"request_id": {"type": "string"}}
            if name not in {"get_request", "complete"}:
                properties["arguments"] = {"type": "object"}
            result.append({"name": f"browser.{name}", "description": "Operate only on this connection's mapped browser grant.",
                           "inputSchema": {"type": "object", "properties": properties,
                                           "required": ["request_id"], "additionalProperties": False}})
        return result

    @app.post("/mcp")
    async def mcp(request: Request, authorization: str | None = Header(default=None)):
        token_row = load_access(authorization)
        try:
            payload = McpRequest.model_validate(await request.json())
        except (ValueError, ValidationError):
            raise HTTPException(400, "Invalid MCP request") from None
        if payload.method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "auto-browser-oauth-gateway", "version": "1.0.0"}}
        elif payload.method == "notifications/initialized":
            return JSONResponse({}, status_code=202)
        elif payload.method == "tools/list":
            result = {"tools": tools()}
        elif payload.method == "tools/call":
            name, arguments = payload.params.get("name"), payload.params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise HTTPException(400, "Invalid tool call")
            value = await invoke_tool(token_row, name, arguments)
            result = {"content": [{"type": "text", "text": json.dumps(value, separators=(",", ":"))}]}
        else:
            raise HTTPException(404, "MCP method unavailable")
        return {"jsonrpc": "2.0", "id": payload.id, "result": result}

    return app


def app_from_environment() -> FastAPI:
    broker_resolver = None
    if os.environ.get("TENANT_STACK_ROOT"):
        from tenant_stacks import TenantBrokerRegistry

        broker_resolver = TenantBrokerRegistry(os.environ["TENANT_STACK_ROOT"], "gateway")
    return create_app(
        issuer_url=os.environ["MCP_GATEWAY_ISSUER_URL"],
        resource_url=os.environ["MCP_GATEWAY_RESOURCE_URL"],
        portal_url=os.environ["MCP_GATEWAY_PORTAL_URL"],
        internal_token=os.environ["MCP_GATEWAY_INTERNAL_TOKEN"],
        broker_token=os.environ.get("MCP_GATEWAY_BROKER_TOKEN"),
        database_path=os.environ["MCP_GATEWAY_DB"],
        broker_url=os.environ.get("MCP_GATEWAY_BROKER_URL", "http://approval-broker:18001"),
        broker_resolver=broker_resolver,
    )


app = app_from_environment() if os.environ.get("MCP_GATEWAY_ISSUER_URL") else None

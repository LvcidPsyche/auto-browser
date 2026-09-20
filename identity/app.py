"""Invitation-only identity enrollment for the Auto Browser service."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Callable, Protocol

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_INVITATION_TTL = 24 * 60 * 60
DEFAULT_VERIFICATION_TTL = 10 * 60
DEFAULT_VERIFICATION_ATTEMPTS = 5
DEFAULT_VERIFICATION_REQUESTS = 3
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
OPAQUE_ID_PATTERN = r"^[A-Za-z0-9_-]{20,80}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class InvitationCreate(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    tenant_id: str | None = Field(default=None, pattern=OPAQUE_ID_PATTERN)
    expires_in_seconds: int | None = Field(default=None, ge=60, le=30 * 24 * 60 * 60)


class InvitationRedeem(StrictModel):
    invitation_token: str = Field(min_length=20, max_length=512)
    email: str = Field(min_length=3, max_length=320)


class VerificationRequest(StrictModel):
    redemption_token: str = Field(min_length=20, max_length=512)


class VerificationConfirm(StrictModel):
    challenge_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    verification_token: str = Field(min_length=20, max_length=512)


class EmailSender(Protocol):
    def send_verification(self, email: str, token: str, expires_at: float) -> None: ...


class NoOpEmailSender:
    """Production-safe placeholder: deliberately sends nothing."""

    def send_verification(self, email: str, token: str, expires_at: float) -> None:
        del email, token, expires_at


class RecordingEmailSender:
    """Test sender that keeps messages in process memory only."""

    def __init__(self) -> None:
        self.messages: list[dict[str, str | float]] = []

    def send_verification(self, email: str, token: str, expires_at: float) -> None:
        self.messages.append({"email": email, "token": token, "expires_at": expires_at})


def normalize_email(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if len(normalized) > 320 or not EMAIL_PATTERN.fullmatch(normalized):
        raise HTTPException(422, "Invalid email")
    return normalized


def opaque_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class IdentityStore:
    """SQLite identity state with explicit transactions for one-time transitions."""

    def __init__(self, root: str | Path):
        self.root = Path(root).absolute()
        if self.root.exists() and (not self.root.is_dir() or self.root.is_symlink()):
            raise ValueError("Identity state root must be a regular directory")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        if os.name == "posix" and self.root.stat().st_mode & 0o077:
            raise ValueError("Identity state root must be private (0700)")
        self.path = self.root / "identity.sqlite3"
        if self.path.exists() and (not self.path.is_file() or self.path.is_symlink()):
            raise ValueError("Identity database must be a regular file")
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        if os.name == "posix" and self.path.stat().st_mode & 0o077:
            raise ValueError("Identity database must be private (0600)")
        try:
            with closing(self.connect()) as db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tenants (
                        tenant_id TEXT PRIMARY KEY,
                        created_at REAL NOT NULL
                    ) WITHOUT ROWID;
                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
                        email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                        created_at REAL NOT NULL
                    ) WITHOUT ROWID;
                    CREATE TABLE IF NOT EXISTS invitations (
                        invitation_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
                        email TEXT NOT NULL COLLATE NOCASE,
                        inviter TEXT NOT NULL,
                        token_hash TEXT NOT NULL UNIQUE,
                        redemption_hash TEXT UNIQUE,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        redeemed_at REAL,
                        revoked_at REAL,
                        verification_requests INTEGER NOT NULL DEFAULT 0,
                        user_id TEXT REFERENCES users(user_id)
                    ) WITHOUT ROWID;
                    CREATE TABLE IF NOT EXISTS verification_challenges (
                        challenge_id TEXT PRIMARY KEY,
                        invitation_id TEXT NOT NULL REFERENCES invitations(invitation_id),
                        tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
                        email TEXT NOT NULL COLLATE NOCASE,
                        token_hash TEXT NOT NULL UNIQUE,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL,
                        invalidated_at REAL,
                        verified_at REAL
                    ) WITHOUT ROWID;
                    CREATE TABLE IF NOT EXISTS audit_log (
                        audit_id TEXT PRIMARY KEY,
                        occurred_at REAL NOT NULL,
                        action TEXT NOT NULL,
                        result TEXT NOT NULL,
                        inviter TEXT,
                        invitee_email TEXT COLLATE NOCASE,
                        tenant_id TEXT,
                        invitation_id TEXT,
                        user_id TEXT,
                        source TEXT
                    ) WITHOUT ROWID;
                    CREATE TABLE IF NOT EXISTS rate_limits (
                        rate_key TEXT PRIMARY KEY,
                        window_started REAL NOT NULL,
                        count INTEGER NOT NULL
                    ) WITHOUT ROWID;
                    CREATE TRIGGER IF NOT EXISTS audit_log_no_update
                        BEFORE UPDATE ON audit_log
                        BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
                        BEFORE DELETE ON audit_log
                        BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
                    CREATE INDEX IF NOT EXISTS invitations_email_idx ON invitations(email);
                    CREATE INDEX IF NOT EXISTS verification_invitation_idx
                        ON verification_challenges(invitation_id);
                    CREATE INDEX IF NOT EXISTS audit_invitation_idx ON audit_log(invitation_id);
                    """
                )
        except sqlite3.Error as exc:
            raise ValueError("Identity database unavailable") from exc

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def audit(
        db: sqlite3.Connection,
        *,
        now: float,
        action: str,
        result: str,
        inviter: str | None = None,
        email: str | None = None,
        tenant_id: str | None = None,
        invitation_id: str | None = None,
        user_id: str | None = None,
        source: str | None = None,
    ) -> None:
        db.execute(
            """INSERT INTO audit_log
               (audit_id, occurred_at, action, result, inviter, invitee_email,
                tenant_id, invitation_id, user_id, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (opaque_id("aud"), now, action, result, inviter, email, tenant_id, invitation_id, user_id, source),
        )

    def check_rate(self, key: str, *, now: float, limit: int, window: float) -> bool:
        safe_key = token_digest(key)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT window_started, count FROM rate_limits WHERE rate_key=?", (safe_key,)).fetchone()
            if row is None or now - row["window_started"] >= window:
                db.execute(
                    """INSERT INTO rate_limits(rate_key, window_started, count) VALUES (?, ?, 1)
                       ON CONFLICT(rate_key) DO UPDATE SET window_started=excluded.window_started, count=1""",
                    (safe_key, now),
                )
                db.commit()
                return True
            if row["count"] >= limit:
                db.commit()
                return False
            db.execute("UPDATE rate_limits SET count=count+1 WHERE rate_key=?", (safe_key,))
            db.commit()
            return True


def create_app(
    *,
    state_root: str | Path,
    admin_token: str,
    sender: EmailSender | None = None,
    admin_id: str = "owner-admin",
    invitation_ttl: int = DEFAULT_INVITATION_TTL,
    verification_ttl: int = DEFAULT_VERIFICATION_TTL,
    verification_attempts: int = DEFAULT_VERIFICATION_ATTEMPTS,
    verification_requests: int = DEFAULT_VERIFICATION_REQUESTS,
    source_rate_limit: int = 100,
    source_rate_window: int = 60,
    token_rate_limit: int = 10,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    if len(admin_token) < 32:
        raise ValueError("Admin bearer credential must have at least 32 characters")
    if min(invitation_ttl, verification_ttl, verification_attempts, verification_requests) < 1:
        raise ValueError("Identity expiry and attempt settings must be positive")
    store = IdentityStore(state_root)
    sender = sender or NoOpEmailSender()
    app = FastAPI(title="Auto Browser identity", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.identity_store = store

    def source_of(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def rate_or_reject(
        kind: str,
        subject: str,
        source: str,
        *,
        token_scoped: bool = False,
        audit_action: str | None = None,
        attempted_email: str | None = None,
        attempted_tenant: str | None = None,
    ) -> None:
        now = clock()
        allowed = store.check_rate(
            f"source:{kind}:{source}", now=now, limit=source_rate_limit, window=source_rate_window
        )
        if allowed and token_scoped:
            allowed = store.check_rate(
                f"subject:{kind}:{subject}", now=now, limit=token_rate_limit, window=source_rate_window
            )
        if allowed:
            return
        if audit_action is not None:
            with closing(store.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                row = None
                if kind == "redeem":
                    row = db.execute("SELECT * FROM invitations WHERE token_hash=?", (subject,)).fetchone()
                elif kind == "verify-request":
                    row = db.execute("SELECT * FROM invitations WHERE redemption_hash=?", (subject,)).fetchone()
                elif kind == "verify-confirm":
                    row = db.execute(
                        """SELECT c.invitation_id, c.tenant_id, c.email, i.inviter, i.user_id
                           FROM verification_challenges c JOIN invitations i
                           ON i.invitation_id=c.invitation_id WHERE c.challenge_id=?""",
                        (subject,),
                    ).fetchone()
                elif kind == "admin-revoke":
                    row = db.execute("SELECT * FROM invitations WHERE invitation_id=?", (subject,)).fetchone()
                store.audit(
                    db,
                    now=now,
                    action=audit_action,
                    result="rate_limited",
                    inviter=row["inviter"] if row else admin_id if kind.startswith("admin-") else None,
                    email=row["email"] if row else attempted_email,
                    tenant_id=row["tenant_id"] if row else attempted_tenant,
                    invitation_id=row["invitation_id"] if row else subject if kind == "admin-revoke" else None,
                    user_id=row["user_id"] if row and "user_id" in row.keys() else None,
                    source=source,
                )
                db.commit()
        raise HTTPException(429, "Rate limit exceeded")

    def require_admin(authorization: str | None) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer credential required")
        if not secrets.compare_digest(authorization[7:].encode(), admin_token.encode()):
            raise HTTPException(401, "Invalid bearer credential")

    @app.get("/healthz")
    async def health(request: Request):
        rate_or_reject("health", "health", source_of(request))
        try:
            with closing(store.connect()) as db:
                db.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            raise HTTPException(503, "Identity storage unavailable") from None
        return {"status": "ok"}

    @app.post("/admin/invitations")
    async def create_invitation(
        payload: InvitationCreate,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        source = source_of(request)
        email = normalize_email(payload.email)
        rate_or_reject(
            "admin-create", "admin", source, audit_action="invitation.create",
            attempted_email=email, attempted_tenant=payload.tenant_id,
        )
        require_admin(authorization)
        now = clock()
        invitation_id, invitation_token = opaque_id("inv"), secrets.token_urlsafe(32)
        tenant_id = payload.tenant_id or opaque_id("ten")
        expires_at = now + (payload.expires_in_seconds or invitation_ttl)
        try:
            with closing(store.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                if payload.tenant_id is None:
                    db.execute("INSERT INTO tenants(tenant_id, created_at) VALUES (?, ?)", (tenant_id, now))
                elif db.execute("SELECT 1 FROM tenants WHERE tenant_id=?", (tenant_id,)).fetchone() is None:
                    store.audit(
                        db, now=now, action="invitation.create", result="unknown_tenant",
                        inviter=admin_id, email=email, tenant_id=tenant_id, source=source,
                    )
                    db.commit()
                    raise HTTPException(404, "Tenant not found")
                db.execute(
                    """INSERT INTO invitations
                       (invitation_id, tenant_id, email, inviter, token_hash, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (invitation_id, tenant_id, email, admin_id, token_digest(invitation_token), now, expires_at),
                )
                store.audit(
                    db, now=now, action="invitation.create", result="success", inviter=admin_id,
                    email=email, tenant_id=tenant_id, invitation_id=invitation_id, source=source,
                )
                db.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Invitation could not be created") from None
        return {
            "invitation_id": invitation_id,
            "tenant_id": tenant_id,
            "email": email,
            "expires_at": expires_at,
            "invitation_token": invitation_token,
        }

    @app.post("/admin/invitations/{invitation_id}/revoke")
    async def revoke_invitation(
        invitation_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        source = source_of(request)
        rate_or_reject(
            "admin-revoke", invitation_id, source, token_scoped=True, audit_action="invitation.revoke"
        )
        require_admin(authorization)
        now = clock()
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM invitations WHERE invitation_id=?", (invitation_id,)).fetchone()
            if row is None:
                store.audit(
                    db, now=now, action="invitation.revoke", result="not_found",
                    inviter=admin_id, invitation_id=invitation_id, source=source,
                )
                db.commit()
                raise HTTPException(404, "Invitation not found")
            if row["user_id"] is not None:
                result = "already_verified"
            elif row["revoked_at"] is not None:
                result = "already_revoked"
            else:
                result = "success"
                db.execute("UPDATE invitations SET revoked_at=? WHERE invitation_id=?", (now, invitation_id))
                db.execute(
                    "UPDATE verification_challenges SET invalidated_at=? WHERE invitation_id=? AND invalidated_at IS NULL",
                    (now, invitation_id),
                )
            store.audit(
                db, now=now, action="invitation.revoke", result=result, inviter=admin_id,
                email=row["email"], tenant_id=row["tenant_id"], invitation_id=invitation_id,
                user_id=row["user_id"], source=source,
            )
            db.commit()
        if result == "already_verified":
            raise HTTPException(409, "Invitation can no longer be revoked")
        return {"invitation_id": invitation_id, "status": "revoked"}

    @app.post("/invitations/redeem")
    async def redeem(payload: InvitationRedeem, request: Request):
        source = source_of(request)
        digest = token_digest(payload.invitation_token)
        email = normalize_email(payload.email)
        rate_or_reject(
            "redeem", digest, source, token_scoped=True,
            audit_action="invitation.redeem", attempted_email=email,
        )
        now = clock()
        redemption_token = secrets.token_urlsafe(32)
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM invitations WHERE token_hash=?", (digest,)).fetchone()
            valid = bool(
                row
                and secrets.compare_digest(row["email"].encode(), email.encode())
                and row["redeemed_at"] is None
                and row["revoked_at"] is None
                and row["expires_at"] > now
            )
            if valid:
                db.execute(
                    "UPDATE invitations SET redeemed_at=?, redemption_hash=? WHERE invitation_id=?",
                    (now, token_digest(redemption_token), row["invitation_id"]),
                )
            store.audit(
                db, now=now, action="invitation.redeem", result="success" if valid else "unavailable",
                inviter=row["inviter"] if row else None, email=email,
                tenant_id=row["tenant_id"] if row else None,
                invitation_id=row["invitation_id"] if row else None, source=source,
            )
            db.commit()
        if not valid:
            raise HTTPException(400, "Invitation unavailable")
        return {"status": "verification_required", "redemption_token": redemption_token}

    @app.post("/verify/request")
    async def request_verification(payload: VerificationRequest, request: Request):
        source = source_of(request)
        digest = token_digest(payload.redemption_token)
        rate_or_reject(
            "verify-request", digest, source, token_scoped=True, audit_action="verification.request"
        )
        now = clock()
        verification_token = secrets.token_urlsafe(32)
        challenge_id = opaque_id("ver")
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM invitations WHERE redemption_hash=?", (digest,)).fetchone()
            valid = bool(
                row
                and row["redeemed_at"] is not None
                and row["revoked_at"] is None
                and row["expires_at"] > now
                and row["user_id"] is None
                and row["verification_requests"] < verification_requests
            )
            if valid:
                db.execute(
                    "UPDATE verification_challenges SET invalidated_at=? "
                    "WHERE invitation_id=? AND invalidated_at IS NULL AND verified_at IS NULL",
                    (now, row["invitation_id"]),
                )
                db.execute(
                    """INSERT INTO verification_challenges
                       (challenge_id, invitation_id, tenant_id, email, token_hash,
                        created_at, expires_at, max_attempts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        challenge_id, row["invitation_id"], row["tenant_id"], row["email"],
                        token_digest(verification_token), now, now + verification_ttl, verification_attempts,
                    ),
                )
                db.execute(
                    "UPDATE invitations SET verification_requests=verification_requests+1 WHERE invitation_id=?",
                    (row["invitation_id"],),
                )
            store.audit(
                db, now=now, action="verification.request", result="success" if valid else "unavailable",
                inviter=row["inviter"] if row else None, email=row["email"] if row else None,
                tenant_id=row["tenant_id"] if row else None,
                invitation_id=row["invitation_id"] if row else None, source=source,
            )
            db.commit()
        if not valid:
            raise HTTPException(400, "Verification unavailable")
        sender.send_verification(row["email"], verification_token, now + verification_ttl)
        return {"status": "sent", "challenge_id": challenge_id, "expires_at": now + verification_ttl}

    @app.post("/verify/confirm")
    async def confirm_verification(payload: VerificationConfirm, request: Request):
        source = source_of(request)
        rate_or_reject(
            "verify-confirm", payload.challenge_id, source, token_scoped=True,
            audit_action="verification.confirm",
        )
        now = clock()
        supplied_digest = token_digest(payload.verification_token)
        user_id: str | None = None
        tenant_id: str | None = None
        with closing(store.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT c.*, i.inviter, i.revoked_at, i.user_id AS invitation_user_id
                   FROM verification_challenges c
                   JOIN invitations i ON i.invitation_id=c.invitation_id
                   WHERE c.challenge_id=?""",
                (payload.challenge_id,),
            ).fetchone()
            active = bool(
                row
                and row["invalidated_at"] is None
                and row["verified_at"] is None
                and row["revoked_at"] is None
                and row["invitation_user_id"] is None
                and row["expires_at"] > now
                and row["attempts"] < row["max_attempts"]
            )
            matched = bool(active and secrets.compare_digest(row["token_hash"], supplied_digest))
            if active and not matched:
                db.execute(
                    "UPDATE verification_challenges SET attempts=attempts+1 WHERE challenge_id=?",
                    (payload.challenge_id,),
                )
            if matched:
                user_id, tenant_id = opaque_id("usr"), row["tenant_id"]
                try:
                    db.execute(
                        "INSERT INTO users(user_id, tenant_id, email, created_at) VALUES (?, ?, ?, ?)",
                        (user_id, tenant_id, row["email"], now),
                    )
                except sqlite3.IntegrityError:
                    matched = False
                    user_id = None
                if matched:
                    db.execute(
                        "UPDATE verification_challenges SET verified_at=? WHERE challenge_id=?", (now, payload.challenge_id)
                    )
                    db.execute(
                        "UPDATE invitations SET user_id=?, redemption_hash=NULL WHERE invitation_id=?",
                        (user_id, row["invitation_id"]),
                    )
            store.audit(
                db, now=now, action="verification.confirm", result="success" if matched else "failed",
                inviter=row["inviter"] if row else None, email=row["email"] if row else None,
                tenant_id=row["tenant_id"] if row else None,
                invitation_id=row["invitation_id"] if row else None,
                user_id=user_id, source=source,
            )
            db.commit()
        if not matched:
            raise HTTPException(400, "Verification failed")
        return {"status": "verified", "user_id": user_id, "tenant_id": tenant_id}

    return app


def app_from_environment() -> FastAPI:
    return create_app(
        state_root=os.environ["IDENTITY_STATE_ROOT"],
        admin_token=os.environ["IDENTITY_ADMIN_TOKEN"],
        admin_id=os.environ.get("IDENTITY_ADMIN_ID", "owner-admin"),
        invitation_ttl=int(os.environ.get("IDENTITY_INVITATION_TTL", DEFAULT_INVITATION_TTL)),
        verification_ttl=int(os.environ.get("IDENTITY_VERIFICATION_TTL", DEFAULT_VERIFICATION_TTL)),
        verification_attempts=int(
            os.environ.get("IDENTITY_VERIFICATION_ATTEMPTS", DEFAULT_VERIFICATION_ATTEMPTS)
        ),
        verification_requests=int(
            os.environ.get("IDENTITY_VERIFICATION_REQUESTS", DEFAULT_VERIFICATION_REQUESTS)
        ),
    )


app = app_from_environment() if os.environ.get("IDENTITY_ADMIN_TOKEN") else None

"""Invitation-only TOTP identity service."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from approval_broker.app import TOTP_PERIOD, totp_code

INVITE_TTL = 86400
ENROLL_TTL = 600
ATTEMPTS = 5
RECOVERY_COUNT = 10
BLOCK_SECONDS = 300
OPAQUE = r"^[A-Za-z0-9_-]{20,100}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Invite(Strict):
    tenant_id: str | None = Field(None, pattern=OPAQUE)
    intended_display_name: str | None = Field(None, min_length=1, max_length=120)
    expires_in_seconds: int | None = Field(None, ge=60, le=2592000)


class Redeem(Strict):
    invitation_token: str = Field(min_length=20, max_length=512)
    display_name: str = Field(min_length=1, max_length=120)
    recovery_email: str | None = Field(None, min_length=3, max_length=320)


class Confirm(Strict):
    enrollment_id: str = Field(pattern=OPAQUE)
    totp_code: str = Field(pattern=r"^[0-9]{6}$")


class AccountTotp(Strict):
    account: str = Field(min_length=1, max_length=120)
    totp_code: str = Field(pattern=r"^[0-9]{6}$")
    purpose: str | None = Field(None, max_length=120)


class AccountRecovery(Strict):
    account: str = Field(min_length=1, max_length=120)
    recovery_code: str = Field(min_length=8, max_length=128)


def oid(p: str) -> str:
    return p + "_" + secrets.token_urlsafe(24)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def display(s: str) -> str:
    s = " ".join(s.split())
    if not s or len(s) > 120:
        raise HTTPException(422, "Invalid display identity")
    return s


def contact(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip().casefold()
    if len(s) > 320 or "@" not in s or s.startswith("@") or s.endswith("@"):
        raise HTTPException(422, "Invalid recovery contact")
    return s


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / "identity.sqlite3"
        if (
            not self.root.is_dir()
            or self.root.is_symlink()
            or (os.name == "posix" and self.root.stat().st_mode & 0o077)
        ):
            raise ValueError("Identity state root must be private regular directory")
        if self.path.exists() and (not self.path.is_file() or self.path.is_symlink()):
            raise ValueError("Identity database must be a regular file")
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        os.chmod(self.path, 0o600)
        if os.name == "posix" and self.path.stat().st_mode & 0o077:
            raise ValueError("Identity database must be private")
        with closing(self.db()) as d:
            user_columns = {row[1] for row in d.execute("PRAGMA table_info(users)")}
            if user_columns and "totp_secret_encrypted" not in user_columns:
                # Phase 1 identities did not enroll an authenticator. Preserve
                # their rows and require a fresh owner-issued invitation.
                if d.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_users_phase1'"
                ).fetchone():
                    raise ValueError("Incomplete Phase 1 identity migration")
                d.execute("ALTER TABLE users RENAME TO legacy_users_phase1")
                if d.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='invitations'"
                ).fetchone():
                    d.execute("ALTER TABLE invitations RENAME TO legacy_invitations_phase1")
            d.executescript("""
CREATE TABLE IF NOT EXISTS tenants(tenant_id TEXT PRIMARY KEY,created_at REAL NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),display_name TEXT NOT NULL,recovery_email TEXT,totp_secret_encrypted BLOB NOT NULL,last_totp_step INTEGER NOT NULL DEFAULT -1,failures INTEGER NOT NULL DEFAULT 0,blocked_until REAL NOT NULL DEFAULT 0,reenrollment_pending INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS invitations(invitation_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),intended_display_name TEXT,inviter TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,created_at REAL NOT NULL,expires_at REAL NOT NULL,redeemed_at REAL,revoked_at REAL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS pending_enrollments(enrollment_id TEXT PRIMARY KEY,invitation_id TEXT REFERENCES invitations(invitation_id),user_id TEXT REFERENCES users(user_id),tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),display_name TEXT NOT NULL,recovery_email TEXT,totp_secret_encrypted BLOB NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,max_attempts INTEGER NOT NULL,confirmed_at REAL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS recovery_codes(recovery_code_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(user_id),salt BLOB NOT NULL,code_hash TEXT NOT NULL,created_at REAL NOT NULL,used_at REAL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS audit_log(audit_id TEXT PRIMARY KEY,occurred_at REAL NOT NULL,action TEXT NOT NULL,result TEXT NOT NULL,tenant_id TEXT,invitation_id TEXT,user_id TEXT,source TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rate_limits(rate_key TEXT PRIMARY KEY,window_started REAL NOT NULL,count INTEGER NOT NULL) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'audit log is append-only'); END;
CREATE UNIQUE INDEX IF NOT EXISTS users_display_name_unique ON users(display_name COLLATE NOCASE);""")

    def db(self):
        d = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        d.row_factory = sqlite3.Row
        d.execute("PRAGMA foreign_keys=ON")
        d.execute("PRAGMA busy_timeout=5000")
        d.execute("PRAGMA synchronous=FULL")
        return d

    def audit(self, d, now, action, result, source, tenant=None, invite=None, user=None):
        d.execute(
            """INSERT INTO audit_log
               (audit_id,occurred_at,action,result,tenant_id,invitation_id,user_id,source)
               VALUES(?,?,?,?,?,?,?,?)""",
            (oid("aud"), now, action, result, tenant, invite, user, source),
        )

    def limit(self, key, now, n, w):
        with closing(self.db()) as d:
            d.execute("BEGIN IMMEDIATE")
            r = d.execute("SELECT * FROM rate_limits WHERE rate_key=?", (sha(key),)).fetchone()
            if not r or now - r["window_started"] >= w:
                d.execute(
                    "INSERT INTO rate_limits VALUES(?,?,1) ON CONFLICT(rate_key) DO UPDATE SET window_started=excluded.window_started,count=1",
                    (sha(key), now),
                )
                d.commit()
                return True
            if r["count"] >= n:
                d.commit()
                return False
            d.execute("UPDATE rate_limits SET count=count+1 WHERE rate_key=?", (sha(key),))
            d.commit()
            return True


def create_app(
    *,
    state_root: str | Path,
    admin_token: str,
    internal_token: str,
    encryption_key: str | bytes,
    recovery_pepper: str | bytes,
    admin_id="owner-admin",
    invitation_ttl=INVITE_TTL,
    enrollment_ttl=ENROLL_TTL,
    enrollment_attempts=ATTEMPTS,
    recovery_code_count=RECOVERY_COUNT,
    source_rate_limit=100,
    source_rate_window=60,
    token_rate_limit=10,
    trusted_proxy_cidrs=(),
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    if (
        min(len(admin_token), len(internal_token)) < 32
        or min(invitation_ttl, enrollment_ttl, enrollment_attempts, recovery_code_count) < 1
    ):
        raise ValueError("Invalid identity settings")
    try:
        f = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
    except (ValueError, TypeError) as e:
        raise ValueError("A valid Fernet application key is required") from e
    pepper = recovery_pepper.encode() if isinstance(recovery_pepper, str) else recovery_pepper
    if len(pepper) < 16:
        raise ValueError("Recovery-code pepper must have at least 16 bytes")
    try:
        proxies = tuple(ipaddress.ip_network(x, strict=False) for x in trusted_proxy_cidrs)
    except ValueError as e:
        raise ValueError("Invalid trusted proxy CIDR") from e
    st = Store(state_root)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.identity_store = st

    def source(q):
        peer = q.client.host if q.client else "unknown"
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if not any(ip in n for n in proxies):
            return peer
        x = q.headers.get("x-forwarded-for")
        if not x:
            return peer
        a = [z.strip() for z in x.split(",")]
        try:
            if not a or any(not z for z in a):
                raise ValueError
            chain = [ipaddress.ip_address(z) for z in a] + [ip]
            while len(chain) > 1 and any(chain[-1] in n for n in proxies):
                chain.pop()
            return str(chain[-1])
        except ValueError:
            raise HTTPException(400, "Malformed forwarding header")

    def require(v, expected):
        if not v or not v.startswith("Bearer ") or not secrets.compare_digest(v[7:], expected):
            raise HTTPException(401, "Bearer credential required")

    def rate(kind, sub, src):
        n = clock()
        allowed = st.limit("s:" + kind + ":" + src, n, source_rate_limit, source_rate_window) and st.limit(
            "t:" + kind + ":" + sub, n, token_rate_limit, source_rate_window
        )
        if allowed:
            return
        with closing(st.db()) as d:
            d.execute("BEGIN IMMEDIATE")
            st.audit(d, n, kind + ".rate", "rate_limited", src)
            d.commit()
        raise HTTPException(429, "Rate limit exceeded")

    def secret():
        return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")

    def enc(s):
        return f.encrypt(s.encode())

    def dec(s):
        try:
            return f.decrypt(s).decode()
        except (InvalidToken, UnicodeDecodeError) as e:
            raise HTTPException(503, "Authenticator state unavailable") from e

    def uri(s, n):
        return f"otpauth://totp/{quote('Auto Browser:' + n)}?secret={s}&issuer=Auto%20Browser&algorithm=SHA1&digits=6&period={TOTP_PERIOD}"

    def chash(c, salt):
        return hashlib.sha256(pepper + salt + c.encode()).hexdigest()

    def codes(d, user, now):
        d.execute("UPDATE recovery_codes SET used_at=? WHERE user_id=? AND used_at IS NULL", (now, user))
        out = []
        for _ in range(recovery_code_count):
            c = secrets.token_urlsafe(12)
            salt = secrets.token_bytes(16)
            out.append(c)
            d.execute(
                "INSERT INTO recovery_codes VALUES(?,?,?,?,?,NULL)", (oid("rcv"), user, salt, chash(c, salt), now)
            )
        return out

    def check(d, u, code, now):
        if u["reenrollment_pending"] or now < u["blocked_until"]:
            return False
        step = int(now // TOTP_PERIOD)
        match = next(
            (
                x
                for x in range(step - 1, step + 2)
                if x > u["last_totp_step"]
                and secrets.compare_digest(totp_code(dec(u["totp_secret_encrypted"]), x), code)
            ),
            None,
        )
        if match is None:
            fails = u["failures"] + 1
            d.execute(
                "UPDATE users SET failures=?,blocked_until=? WHERE user_id=?",
                (fails, now + BLOCK_SECONDS if fails >= enrollment_attempts else 0, u["user_id"]),
            )
            return False
        d.execute("UPDATE users SET last_totp_step=?,failures=0,blocked_until=0 WHERE user_id=?", (match, u["user_id"]))
        return True

    @app.get("/healthz")
    async def health(request: Request):
        rate("health", "health", source(request))
        return {"status": "ok"}

    @app.post("/admin/invitations")
    async def create(p: Invite, request: Request, authorization: str | None = Header(None)):
        require(authorization, admin_token)
        src = source(request)
        rate("invite", "admin", src)
        now = clock()
        name = display(p.intended_display_name) if p.intended_display_name else None
        tenant = p.tenant_id or oid("ten")
        iid = oid("inv")
        tok = secrets.token_urlsafe(32)
        expires = now + (p.expires_in_seconds or invitation_ttl)
        with closing(st.db()) as d:
            d.execute("BEGIN IMMEDIATE")
            if not p.tenant_id:
                d.execute("INSERT INTO tenants VALUES(?,?)", (tenant, now))
            elif not d.execute("SELECT 1 FROM tenants WHERE tenant_id=?", (tenant,)).fetchone():
                d.commit()
                raise HTTPException(404, "Tenant not found")
            d.execute(
                "INSERT INTO invitations VALUES(?,?,?,?,?,?,?,NULL,NULL)",
                (iid, tenant, name, admin_id, sha(tok), now, expires),
            )
            st.audit(d, now, "invitation.create", "success", src, tenant, iid)
            d.commit()
        return {
            "invitation_id": iid,
            "tenant_id": tenant,
            "intended_display_name": name,
            "expires_at": expires,
            "invitation_token": tok,
        }

    @app.post("/admin/invitations/{invitation_id}/revoke")
    async def revoke(invitation_id: str, request: Request, authorization: str | None = Header(None)):
        require(authorization, admin_token)
        src = source(request)
        rate("revoke", invitation_id, src)
        now = clock()
        with closing(st.db()) as d:
            d.execute("BEGIN IMMEDIATE")
            r = d.execute("SELECT * FROM invitations WHERE invitation_id=?", (invitation_id,)).fetchone()
            if not r:
                d.commit()
                raise HTTPException(404, "Invitation not found")
            if r["redeemed_at"] is not None:
                d.commit()
                raise HTTPException(409, "Invitation can no longer be revoked")
            d.execute("UPDATE invitations SET revoked_at=? WHERE invitation_id=?", (now, invitation_id))
            st.audit(d, now, "invitation.revoke", "success", src, r["tenant_id"], invitation_id)
            d.commit()
        return {"invitation_id": invitation_id, "status": "revoked"}

    @app.post("/invitations/redeem")
    async def redeem(p: Redeem, request: Request, response: Response):
        src = source(request)
        h = sha(p.invitation_token)
        rate("redeem", h, src)
        now = clock()
        name = display(p.display_name)
        mail = contact(p.recovery_email)
        eid = oid("enr")
        sec = secret()
        with closing(st.db()) as d:
            d.execute("BEGIN IMMEDIATE")
            r = d.execute("SELECT * FROM invitations WHERE token_hash=?", (h,)).fetchone()
            existing = d.execute(
                "SELECT * FROM users WHERE display_name=? COLLATE NOCASE", (name,)
            ).fetchone()
            intended_matches = bool(
                r
                and r["intended_display_name"] is not None
                and secrets.compare_digest(r["intended_display_name"].casefold(), name.casefold())
            )
            ok = bool(
                r
                and r["redeemed_at"] is None
                and r["revoked_at"] is None
                and r["expires_at"] > now
                and (r["intended_display_name"] is None or intended_matches)
                and (
                    existing is None
                    or (intended_matches and r["tenant_id"] == existing["tenant_id"])
                )
            )
            if ok:
                user_id = existing["user_id"] if existing else None
                enrollment_tenant = existing["tenant_id"] if existing else r["tenant_id"]
                enrollment_mail = mail if mail is not None else existing["recovery_email"] if existing else None
                d.execute(
                    "INSERT INTO pending_enrollments VALUES(?,?,?,?,?,?,?,?,?,0,?,NULL)",
                    (
                        eid,
                        r["invitation_id"],
                        user_id,
                        enrollment_tenant,
                        name,
                        enrollment_mail,
                        enc(sec),
                        now,
                        now + enrollment_ttl,
                        enrollment_attempts,
                    ),
                )
                if existing:
                    d.execute("UPDATE users SET reenrollment_pending=1 WHERE user_id=?", (user_id,))
                d.execute("UPDATE invitations SET redeemed_at=? WHERE invitation_id=?", (now, r["invitation_id"]))
            st.audit(
                d,
                now,
                "invitation.redeem",
                "success" if ok else "unavailable",
                src,
                r["tenant_id"] if r else None,
                r["invitation_id"] if r else None,
            )
            d.commit()
        if not ok:
            raise HTTPException(400, "Invitation unavailable")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return {
            "status": "pending_enrollment",
            "enrollment_id": eid,
            "secret": sec,
            "provisioning_uri": uri(sec, name),
            "expires_at": now + enrollment_ttl,
        }

    @app.post("/enrollments/confirm")
    async def confirm(p: Confirm, request: Request, response: Response):
        src = source(request)
        rate("confirm", p.enrollment_id, src)
        now = clock()
        with closing(st.db()) as d:
            d.execute("BEGIN IMMEDIATE")
            r = d.execute("SELECT * FROM pending_enrollments WHERE enrollment_id=?", (p.enrollment_id,)).fetchone()
            step = int(now // TOTP_PERIOD)
            matched_step = next(
                (
                    candidate
                    for candidate in range(step - 1, step + 2)
                    if r and secrets.compare_digest(totp_code(dec(r["totp_secret_encrypted"]), candidate), p.totp_code)
                ),
                None,
            )
            ok = bool(
                r
                and r["confirmed_at"] is None
                and r["expires_at"] > now
                and r["attempts"] < r["max_attempts"]
                and matched_step is not None
            )
            uid = r["user_id"] if ok and r else None
            if r and not ok and r["confirmed_at"] is None:
                d.execute(
                    "UPDATE pending_enrollments SET attempts=attempts+1 WHERE enrollment_id=?", (p.enrollment_id,)
                )
            if (
                ok
                and uid is None
                and d.execute(
                    "SELECT 1 FROM users WHERE display_name=? COLLATE NOCASE", (r["display_name"],)
                ).fetchone()
            ):
                ok = False
            if ok:
                if uid is None:
                    uid = oid("usr")
                    d.execute(
                        "INSERT INTO users(user_id,tenant_id,display_name,recovery_email,totp_secret_encrypted,last_totp_step,created_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            uid,
                            r["tenant_id"],
                            r["display_name"],
                            r["recovery_email"],
                            r["totp_secret_encrypted"],
                            matched_step,
                            now,
                        ),
                    )
                else:
                    d.execute(
                        "UPDATE users SET display_name=?,recovery_email=?,totp_secret_encrypted=?,last_totp_step=?,failures=0,blocked_until=0,reenrollment_pending=0 WHERE user_id=?",
                        (r["display_name"], r["recovery_email"], r["totp_secret_encrypted"], matched_step, uid),
                    )
                out = codes(d, uid, now)
                d.execute("UPDATE pending_enrollments SET confirmed_at=? WHERE enrollment_id=?", (now, p.enrollment_id))
            st.audit(
                d,
                now,
                "enrollment.confirm",
                "success" if ok else "failed",
                src,
                r["tenant_id"] if r else None,
                r["invitation_id"] if r else None,
                uid,
            )
            d.commit()
        if not ok:
            raise HTTPException(400, "Enrollment failed")
        response.headers["Cache-Control"] = "no-store"
        return {
            "status": "enrolled",
            "user_id": uid,
            "tenant_id": r["tenant_id"],
            "display_name": r["display_name"],
            "recovery_codes": out,
        }

    @app.post("/internal/auth/verify")
    async def verify(p: AccountTotp, request: Request, authorization: str | None = Header(None)):
        require(authorization, internal_token)
        src = source(request)
        account = display(p.account)
        rate("verify", account, src)
        now = clock()
        with closing(st.db()) as d:
            d.execute("BEGIN IMMEDIATE")
            u = d.execute("SELECT * FROM users WHERE display_name=? COLLATE NOCASE", (account,)).fetchone()
            ok = bool(u and check(d, u, p.totp_code, now))
            st.audit(
                d,
                now,
                "auth.verify:" + str(p.purpose or "unspecified"),
                "success" if ok else "failed",
                src,
                u["tenant_id"] if u else None,
                None,
                u["user_id"] if u else None,
            )
            d.commit()
        if not ok:
            raise HTTPException(403, "Authentication failed")
        return {
            "account": u["display_name"],
            "user_id": u["user_id"],
            "tenant_id": u["tenant_id"],
            "display_name": u["display_name"],
        }

    @app.post("/internal/auth/recover")
    async def recover(
        p: AccountRecovery, request: Request, response: Response, authorization: str | None = Header(None)
    ):
        require(authorization, internal_token)
        src = source(request)
        account = display(p.account)
        rate("recover", account, src)
        now = clock()
        eid = oid("enr")
        sec = secret()
        with closing(st.db()) as d:
            d.execute("BEGIN IMMEDIATE")
            u = d.execute("SELECT * FROM users WHERE display_name=? COLLATE NOCASE", (account,)).fetchone()
            ok = False
            if u:
                for c in d.execute("SELECT * FROM recovery_codes WHERE user_id=? AND used_at IS NULL", (u["user_id"],)):
                    if secrets.compare_digest(c["code_hash"], chash(p.recovery_code, c["salt"])):
                        d.execute(
                            "UPDATE recovery_codes SET used_at=? WHERE recovery_code_id=?", (now, c["recovery_code_id"])
                        )
                        ok = True
                        break
            if ok:
                d.execute("UPDATE users SET reenrollment_pending=1 WHERE user_id=?", (u["user_id"],))
                d.execute(
                    "INSERT INTO pending_enrollments VALUES(?,NULL,?,?,?,?,?,?,?,0,?,NULL)",
                    (
                        eid,
                        u["user_id"],
                        u["tenant_id"],
                        u["display_name"],
                        u["recovery_email"],
                        enc(sec),
                        now,
                        now + enrollment_ttl,
                        enrollment_attempts,
                    ),
                )
            st.audit(
                d,
                now,
                "auth.recover",
                "success" if ok else "failed",
                src,
                u["tenant_id"] if u else None,
                None,
                u["user_id"] if u else None,
            )
            d.commit()
        if not ok:
            raise HTTPException(403, "Authentication failed")
        response.headers["Cache-Control"] = "no-store"
        return {
            "status": "pending_enrollment",
            "enrollment_id": eid,
            "secret": sec,
            "provisioning_uri": uri(sec, u["display_name"]),
            "expires_at": now + enrollment_ttl,
        }

    @app.post("/internal/auth/recovery-codes")
    async def regenerate(
        p: AccountTotp, request: Request, response: Response, authorization: str | None = Header(None)
    ):
        require(authorization, internal_token)
        src = source(request)
        account = display(p.account)
        rate("regen", account, src)
        now = clock()
        with closing(st.db()) as d:
            d.execute("BEGIN IMMEDIATE")
            u = d.execute("SELECT * FROM users WHERE display_name=? COLLATE NOCASE", (account,)).fetchone()
            ok = bool(u and check(d, u, p.totp_code, now))
            out = codes(d, u["user_id"], now) if ok else []
            st.audit(
                d,
                now,
                "recovery.regenerate",
                "success" if ok else "failed",
                src,
                u["tenant_id"] if u else None,
                None,
                u["user_id"] if u else None,
            )
            d.commit()
        if not ok:
            raise HTTPException(403, "Authentication failed")
        response.headers["Cache-Control"] = "no-store"
        return {"recovery_codes": out}

    return app


def app_from_environment():
    return create_app(
        state_root=os.environ["IDENTITY_STATE_ROOT"],
        admin_token=os.environ["IDENTITY_ADMIN_TOKEN"],
        internal_token=os.environ["IDENTITY_INTERNAL_TOKEN"],
        encryption_key=os.environ["IDENTITY_ENCRYPTION_KEY"],
        recovery_pepper=os.environ["IDENTITY_RECOVERY_PEPPER"],
        trusted_proxy_cidrs=tuple(filter(None, os.environ.get("IDENTITY_TRUSTED_PROXY_CIDRS", "").split(","))),
    )


app = app_from_environment() if os.environ.get("IDENTITY_ADMIN_TOKEN") else None

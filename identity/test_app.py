import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from approval_broker.app import TOTP_PERIOD, totp_code
from identity.app import create_app

ADMIN = "a" * 32
INTERNAL = "i" * 32


@pytest.fixture
def service(tmp_path: Path):
    now = [1_800_000_000.0]
    app = create_app(
        state_root=tmp_path / "identity",
        admin_token=ADMIN,
        internal_token=INTERNAL,
        encryption_key=Fernet.generate_key(),
        recovery_pepper="p" * 16,
        clock=lambda: now[0],
        source_rate_limit=1000,
        token_rate_limit=1000,
    )
    return TestClient(app), now, app


def enroll(client: TestClient, now: list[float], name: str = "Alice") -> tuple[dict, dict]:
    invitation = client.post(
        "/admin/invitations", headers={"Authorization": f"Bearer {ADMIN}"}, json={"intended_display_name": name}
    ).json()
    pending = client.post(
        "/invitations/redeem",
        json={
            "invitation_token": invitation["invitation_token"],
            "display_name": name,
            "recovery_email": "backup@example.com",
        },
    )
    assert pending.status_code == 200
    enrollment = pending.json()
    confirmed = client.post(
        "/enrollments/confirm",
        json={
            "enrollment_id": enrollment["enrollment_id"],
            "totp_code": totp_code(enrollment["secret"], int(now[0] // TOTP_PERIOD)),
        },
    )
    assert confirmed.status_code == 200
    return enrollment, confirmed.json()


def test_invite_secret_once_enrolls_encrypted_and_codes_once(service):
    client, now, app = service
    pending, user = enroll(client, now)
    assert pending["secret"] not in str(app.state.identity_store.path.read_bytes())
    assert len(user["recovery_codes"]) == 10
    assert (
        client.post(
            "/enrollments/confirm",
            json={
                "enrollment_id": pending["enrollment_id"],
                "totp_code": totp_code(pending["secret"], int(now[0] // TOTP_PERIOD)),
            },
        ).status_code
        == 400
    )
    assert (
        client.post("/invitations/redeem", json={"invitation_token": "x" * 32, "display_name": "Alice"}).status_code
        == 400
    )


def test_verify_is_bearer_protected_and_consumes_timestep(service):
    client, now, _ = service
    _, user = enroll(client, now)
    now[0] += TOTP_PERIOD
    # Use the stored enrollment secret from a second enrollment fixture instead of exposing a lookup API.
    invitation = client.post(
        "/admin/invitations", headers={"Authorization": f"Bearer {ADMIN}"}, json={"intended_display_name": "Bob"}
    ).json()
    pending = client.post(
        "/invitations/redeem", json={"invitation_token": invitation["invitation_token"], "display_name": "Bob"}
    ).json()
    client.post(
        "/enrollments/confirm",
        json={
            "enrollment_id": pending["enrollment_id"],
            "totp_code": totp_code(pending["secret"], int(now[0] // TOTP_PERIOD)),
        },
    )
    now[0] += TOTP_PERIOD
    payload = {
        "account": "bob",
        "totp_code": totp_code(pending["secret"], int(now[0] // TOTP_PERIOD)),
        "purpose": "portal-login",
    }
    assert client.post("/internal/auth/verify", json=payload).status_code == 401
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    assert client.post("/internal/auth/verify", headers=headers, json=payload).status_code == 200
    assert client.post("/internal/auth/verify", headers=headers, json=payload).status_code == 403
    assert user["display_name"] == "Alice"


def test_recovery_forces_new_enrollment(service):
    client, now, _ = service
    _, user = enroll(client, now)
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    recovered = client.post(
        "/internal/auth/recover", headers=headers, json={"account": "Alice", "recovery_code": user["recovery_codes"][0]}
    )
    assert recovered.status_code == 200
    assert (
        client.post(
            "/internal/auth/recover",
            headers=headers,
            json={"account": "Alice", "recovery_code": user["recovery_codes"][0]},
        ).status_code
        == 403
    )
    pending = recovered.json()
    assert (
        client.post(
            "/enrollments/confirm",
            json={
                "enrollment_id": pending["enrollment_id"],
                "totp_code": totp_code(pending["secret"], int(now[0] // TOTP_PERIOD)),
            },
        ).status_code
        == 200
    )


def test_only_trusted_proxy_can_supply_forwarded_source(tmp_path: Path):
    app = create_app(
        state_root=tmp_path / "state",
        admin_token=ADMIN,
        internal_token=INTERNAL,
        encryption_key=Fernet.generate_key(),
        recovery_pepper="p" * 16,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    with TestClient(app, client=("192.0.2.10", 5000)) as direct:
        assert direct.get("/healthz", headers={"X-Forwarded-For": "not-an-ip"}).status_code == 200
    with TestClient(app, client=("10.1.2.3", 5000)) as proxy:
        assert proxy.get("/healthz", headers={"X-Forwarded-For": "not-an-ip"}).status_code == 400


def test_wrong_identity_reuse_and_revocation_are_generic(service):
    client, _, _ = service
    invitation = client.post(
        "/admin/invitations", headers={"Authorization": f"Bearer {ADMIN}"}, json={"intended_display_name": "Bound"}
    ).json()
    wrong = client.post(
        "/invitations/redeem", json={"invitation_token": invitation["invitation_token"], "display_name": "Other"}
    )
    unknown = client.post("/invitations/redeem", json={"invitation_token": "x" * 32, "display_name": "Other"})
    assert (
        (wrong.status_code, wrong.json())
        == (unknown.status_code, unknown.json())
        == (400, {"detail": "Invitation unavailable"})
    )
    assert (
        client.post(
            f"/admin/invitations/{invitation['invitation_id']}/revoke", headers={"Authorization": f"Bearer {ADMIN}"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/invitations/redeem", json={"invitation_token": invitation["invitation_token"], "display_name": "Bound"}
        ).status_code
        == 400
    )


def test_invalid_confirmation_never_creates_user_and_locks(service):
    client, now, _ = service
    invitation = client.post("/admin/invitations", headers={"Authorization": f"Bearer {ADMIN}"}, json={}).json()
    pending = client.post(
        "/invitations/redeem", json={"invitation_token": invitation["invitation_token"], "display_name": "Locked"}
    ).json()
    payload = {"enrollment_id": pending["enrollment_id"], "totp_code": "000000"}
    for _ in range(5):
        assert client.post("/enrollments/confirm", json=payload).status_code == 400
    valid = {
        "enrollment_id": pending["enrollment_id"],
        "totp_code": totp_code(pending["secret"], int(now[0] // TOTP_PERIOD)),
    }
    assert client.post("/enrollments/confirm", json=valid).status_code == 400


def test_recovery_disables_old_authenticator_and_regeneration_needs_fresh_code(service):
    client, now, _ = service
    enrollment, user = enroll(client, now, "Recoverable")
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    now[0] += TOTP_PERIOD
    old = {"account": "recoverable", "totp_code": totp_code(enrollment["secret"], int(now[0] // TOTP_PERIOD))}
    recovered = client.post(
        "/internal/auth/recover",
        headers=headers,
        json={"account": "recoverable", "recovery_code": user["recovery_codes"][0]},
    ).json()
    assert client.post("/internal/auth/verify", headers=headers, json=old).status_code == 403
    assert (
        client.post(
            "/enrollments/confirm",
            json={
                "enrollment_id": recovered["enrollment_id"],
                "totp_code": totp_code(recovered["secret"], int(now[0] // TOTP_PERIOD)),
            },
        ).status_code
        == 200
    )
    now[0] += TOTP_PERIOD
    fresh = {"account": "recoverable", "totp_code": totp_code(recovered["secret"], int(now[0] // TOTP_PERIOD))}
    assert len(client.post("/internal/auth/recovery-codes", headers=headers, json=fresh).json()["recovery_codes"]) == 10
    assert client.post("/internal/auth/recovery-codes", headers=headers, json=fresh).status_code == 403


def test_login_attempt_lock_and_audit_never_contain_authenticator_secret(service):
    client, now, app = service
    enrollment, user = enroll(client, now, "AuditUser")
    now[0] += TOTP_PERIOD
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    bad = {"account": "audituser", "totp_code": "000000"}
    for _ in range(5):
        assert client.post("/internal/auth/verify", headers=headers, json=bad).status_code == 403
    good = {"account": "audituser", "totp_code": totp_code(enrollment["secret"], int(now[0] // TOTP_PERIOD))}
    assert client.post("/internal/auth/verify", headers=headers, json=good).status_code == 403
    assert enrollment["secret"] not in client.get("/healthz").text
    with closing(sqlite3.connect(app.state.identity_store.path)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(audit_log)")}
        audit = repr(db.execute("SELECT * FROM audit_log").fetchall())
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("DELETE FROM audit_log")
    assert {"secret", "code", "token_hash"}.isdisjoint(columns)
    assert enrollment["secret"] not in audit
    assert user["recovery_codes"][0] not in audit


def test_wrong_login_is_subject_rate_limited_with_the_same_failure_body(tmp_path: Path):
    now = [1_800_000_000.0]
    app = create_app(
        state_root=tmp_path / "rate-limited",
        admin_token=ADMIN,
        internal_token=INTERNAL,
        encryption_key=Fernet.generate_key(),
        recovery_pepper="p" * 16,
        clock=lambda: now[0],
        source_rate_limit=100,
        token_rate_limit=2,
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    payload = {"account": "missing-user", "totp_code": "000000", "purpose": "portal_login"}
    first = client.post("/internal/auth/verify", headers=headers, json=payload)
    second = client.post("/internal/auth/verify", headers=headers, json=payload)
    limited = client.post("/internal/auth/verify", headers=headers, json=payload)
    assert (first.status_code, first.json()) == (403, {"detail": "Authentication failed"})
    assert second.json() == first.json()
    assert (limited.status_code, limited.json()) == (429, {"detail": "Rate limit exceeded"})


def test_owner_can_force_reenrollment_with_a_fresh_bound_invitation(service):
    client, now, _ = service
    old_enrollment, user = enroll(client, now, "OwnerRecovery")
    invitation = client.post(
        "/admin/invitations",
        headers={"Authorization": f"Bearer {ADMIN}"},
        json={"tenant_id": user["tenant_id"], "intended_display_name": "OwnerRecovery"},
    ).json()
    replacement = client.post(
        "/invitations/redeem",
        json={"invitation_token": invitation["invitation_token"], "display_name": "ownerrecovery"},
    ).json()
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    now[0] += TOTP_PERIOD
    old_code = totp_code(old_enrollment["secret"], int(now[0] // TOTP_PERIOD))
    assert client.post(
        "/internal/auth/verify",
        headers=headers,
        json={"account": "OwnerRecovery", "totp_code": old_code},
    ).status_code == 403
    assert client.post(
        "/enrollments/confirm",
        json={
            "enrollment_id": replacement["enrollment_id"],
            "totp_code": totp_code(replacement["secret"], int(now[0] // TOTP_PERIOD)),
        },
    ).status_code == 200


def test_phase1_database_is_preserved_and_new_enrollment_tables_are_created(tmp_path: Path):
    root = tmp_path / "phase1"
    root.mkdir()
    database = root / "identity.sqlite3"
    with closing(sqlite3.connect(database)) as db:
        db.executescript("""
            CREATE TABLE tenants(tenant_id TEXT PRIMARY KEY, created_at REAL NOT NULL) WITHOUT ROWID;
            CREATE TABLE users(user_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE, created_at REAL NOT NULL) WITHOUT ROWID;
            CREATE TABLE invitations(invitation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                email TEXT NOT NULL, inviter TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                redemption_hash TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                redeemed_at REAL, revoked_at REAL, verification_requests INTEGER NOT NULL DEFAULT 0,
                user_id TEXT) WITHOUT ROWID;
            CREATE TABLE audit_log(audit_id TEXT PRIMARY KEY, occurred_at REAL NOT NULL,
                action TEXT NOT NULL, result TEXT NOT NULL, inviter TEXT, invitee_email TEXT,
                tenant_id TEXT, invitation_id TEXT, user_id TEXT, source TEXT) WITHOUT ROWID;
            CREATE TABLE rate_limits(rate_key TEXT PRIMARY KEY, window_started REAL NOT NULL,
                count INTEGER NOT NULL) WITHOUT ROWID;
            INSERT INTO tenants VALUES('legacy-tenant', 1);
            INSERT INTO users VALUES('legacy-user','legacy-tenant','old@example.com',1);
        """)
    app = create_app(
        state_root=root,
        admin_token=ADMIN,
        internal_token=INTERNAL,
        encryption_key=Fernet.generate_key(),
        recovery_pepper="p" * 16,
    )
    with closing(sqlite3.connect(app.state.identity_store.path)) as db:
        assert db.execute("SELECT email FROM legacy_users_phase1").fetchone()[0] == "old@example.com"
        assert {row[1] for row in db.execute("PRAGMA table_info(users)")} >= {
            "display_name", "totp_secret_encrypted", "reenrollment_pending"
        }

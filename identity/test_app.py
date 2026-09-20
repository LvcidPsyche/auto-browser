from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from identity.app import RecordingEmailSender, create_app

ADMIN = "admin-secret-credential-" + "x" * 32


def auth(token: str = ADMIN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def identity(tmp_path: Path):
    now = [1_800_000_000.0]
    sender = RecordingEmailSender()
    app = create_app(
        state_root=tmp_path / "identity-state",
        admin_token=ADMIN,
        sender=sender,
        invitation_ttl=120,
        verification_ttl=60,
        verification_attempts=3,
        verification_requests=2,
        source_rate_limit=1000,
        token_rate_limit=100,
        clock=lambda: now[0],
    )
    with TestClient(app) as client:
        yield client, sender, now, app.state.identity_store.path


def invite(client: TestClient, email: str, **extra) -> dict:
    response = client.post("/admin/invitations", headers=auth(), json={"email": email, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def redeem(client: TestClient, invitation: dict, email: str | None = None) -> dict:
    response = client.post(
        "/invitations/redeem",
        json={"invitation_token": invitation["invitation_token"], "email": email or invitation["email"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def request_code(client: TestClient, sender: RecordingEmailSender, redemption: dict) -> tuple[dict, str]:
    response = client.post("/verify/request", json={"redemption_token": redemption["redemption_token"]})
    assert response.status_code == 200, response.text
    return response.json(), str(sender.messages[-1]["token"])


def enroll(client: TestClient, sender: RecordingEmailSender, email: str) -> tuple[dict, dict]:
    invitation = invite(client, email)
    redemption = redeem(client, invitation)
    challenge, code = request_code(client, sender, redemption)
    verified = client.post(
        "/verify/confirm",
        json={"challenge_id": challenge["challenge_id"], "verification_token": code},
    )
    assert verified.status_code == 200, verified.text
    return invitation, verified.json()


def test_invited_email_redeems_and_verifies_but_other_email_cannot(identity) -> None:
    client, sender, _, _ = identity
    invitation = invite(client, "  Invited@Example.COM ")
    assert invitation["email"] == "invited@example.com"
    denied = client.post(
        "/invitations/redeem",
        json={"invitation_token": invitation["invitation_token"], "email": "other@example.com"},
    )
    assert denied.status_code == 400
    redemption = redeem(client, invitation, "INVITED@example.com")
    challenge, code = request_code(client, sender, redemption)
    assert sender.messages[-1]["email"] == "invited@example.com"
    verified = client.post(
        "/verify/confirm",
        json={"challenge_id": challenge["challenge_id"], "verification_token": code},
    )
    assert verified.status_code == 200
    assert verified.json()["status"] == "verified"


def test_invitation_is_single_use_and_non_transferable(identity) -> None:
    client, _, _, _ = identity
    invitation = invite(client, "one@example.com")
    redeem(client, invitation)
    first_failure = client.post(
        "/invitations/redeem",
        json={"invitation_token": invitation["invitation_token"], "email": "one@example.com"},
    )
    transfer_failure = client.post(
        "/invitations/redeem",
        json={"invitation_token": invitation["invitation_token"], "email": "two@example.com"},
    )
    assert (first_failure.status_code, first_failure.json()) == (400, {"detail": "Invitation unavailable"})
    assert transfer_failure.json() == first_failure.json()


def test_expired_revoked_used_and_unknown_invitations_fail_identically(identity) -> None:
    client, _, now, _ = identity
    used = invite(client, "used@example.com")
    redeem(client, used)
    expired = invite(client, "expired@example.com")
    revoked = invite(client, "revoked@example.com")
    assert client.post(
        f"/admin/invitations/{revoked['invitation_id']}/revoke", headers=auth(), json={}
    ).status_code == 200
    now[0] += 121
    cases = [
        (used["invitation_token"], used["email"]),
        (expired["invitation_token"], expired["email"]),
        (revoked["invitation_token"], revoked["email"]),
        ("forged-" + "z" * 40, "nobody@example.com"),
    ]
    responses = [
        client.post("/invitations/redeem", json={"invitation_token": token, "email": email}) for token, email in cases
    ]
    assert {(response.status_code, response.text) for response in responses} == {
        (400, '{"detail":"Invitation unavailable"}')
    }


def test_revocation_after_redemption_invalidates_verification(identity) -> None:
    client, sender, _, _ = identity
    invitation = invite(client, "cancelled@example.com")
    redemption = redeem(client, invitation)
    challenge, code = request_code(client, sender, redemption)
    assert client.post(
        f"/admin/invitations/{invitation['invitation_id']}/revoke", headers=auth(), json={}
    ).status_code == 200
    response = client.post(
        "/verify/confirm", json={"challenge_id": challenge["challenge_id"], "verification_token": code}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Verification failed"}


def test_forged_verification_tokens_and_ids_are_generic(identity) -> None:
    client, sender, _, _ = identity
    invitation = invite(client, "verify@example.com")
    challenge, _ = request_code(client, sender, redeem(client, invitation))
    bad_token = client.post(
        "/verify/confirm",
        json={"challenge_id": challenge["challenge_id"], "verification_token": "forged-" + "a" * 40},
    )
    bad_id = client.post(
        "/verify/confirm",
        json={"challenge_id": "ver_" + "b" * 32, "verification_token": "forged-" + "a" * 40},
    )
    assert (bad_token.status_code, bad_token.json()) == (400, {"detail": "Verification failed"})
    assert bad_id.json() == bad_token.json()


def test_verification_token_is_expiring_and_one_time(identity) -> None:
    client, sender, now, _ = identity
    expired_invitation = invite(client, "expired-code@example.com")
    expired_challenge, expired_code = request_code(client, sender, redeem(client, expired_invitation))
    now[0] += 61
    assert client.post(
        "/verify/confirm",
        json={"challenge_id": expired_challenge["challenge_id"], "verification_token": expired_code},
    ).status_code == 400

    fresh_invitation = invite(client, "one-code@example.com")
    fresh_challenge, fresh_code = request_code(client, sender, redeem(client, fresh_invitation))
    payload = {"challenge_id": fresh_challenge["challenge_id"], "verification_token": fresh_code}
    assert client.post("/verify/confirm", json=payload).status_code == 200
    assert client.post("/verify/confirm", json=payload).status_code == 400


def test_verification_attempt_and_request_limits_lock_challenge(identity) -> None:
    client, sender, _, _ = identity
    invitation = invite(client, "limited@example.com")
    redemption = redeem(client, invitation)
    first, first_code = request_code(client, sender, redemption)
    second, second_code = request_code(client, sender, redemption)
    assert first["challenge_id"] != second["challenge_id"]
    assert client.post("/verify/request", json={"redemption_token": redemption["redemption_token"]}).status_code == 400
    assert client.post(
        "/verify/confirm", json={"challenge_id": first["challenge_id"], "verification_token": first_code}
    ).status_code == 400
    for _ in range(3):
        assert client.post(
            "/verify/confirm",
            json={"challenge_id": second["challenge_id"], "verification_token": "wrong-" + "w" * 40},
        ).status_code == 400
    assert client.post(
        "/verify/confirm", json={"challenge_id": second["challenge_id"], "verification_token": second_code}
    ).status_code == 400


def test_per_source_and_per_token_rate_limits_trigger(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    sender = RecordingEmailSender()
    token_limited = create_app(
        state_root=tmp_path / "token", admin_token=ADMIN, sender=sender,
        source_rate_limit=100, token_rate_limit=2, clock=lambda: now[0],
    )
    with TestClient(token_limited) as client:
        invitation = invite(client, "rate@example.com")
        payload = {"invitation_token": "unknown-" + "q" * 40, "email": invitation["email"]}
        assert client.post("/invitations/redeem", json=payload).status_code == 400
        assert client.post("/invitations/redeem", json=payload).status_code == 400
        assert client.post("/invitations/redeem", json=payload).status_code == 429
        with closing(sqlite3.connect(token_limited.state.identity_store.path)) as db:
            assert db.execute(
                "SELECT count(*) FROM audit_log WHERE action='invitation.redeem' AND result='rate_limited'"
            ).fetchone()[0] == 1

    source_limited = create_app(
        state_root=tmp_path / "source", admin_token=ADMIN, sender=sender,
        source_rate_limit=2, token_rate_limit=100, clock=lambda: now[0],
    )
    with TestClient(source_limited) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").status_code == 429


def test_admin_endpoints_reject_missing_and_wrong_bearers(identity) -> None:
    client, _, _, _ = identity
    assert client.post("/admin/invitations", json={"email": "a@example.com"}).status_code == 401
    assert client.post(
        "/admin/invitations", headers=auth("wrong-" + "x" * 40), json={"email": "a@example.com"}
    ).status_code == 401
    invitation = invite(client, "a@example.com")
    path = f"/admin/invitations/{invitation['invitation_id']}/revoke"
    assert client.post(path, json={}).status_code == 401
    assert client.post(path, headers=auth("wrong-" + "x" * 40), json={}).status_code == 401


def test_two_users_have_distinct_opaque_ids_and_no_record_read_surface(identity) -> None:
    client, sender, _, _ = identity
    first_invitation, first = enroll(client, sender, "first@example.com")
    second_invitation, second = enroll(client, sender, "second@example.com")
    assert first["user_id"] != second["user_id"]
    assert first["tenant_id"] != second["tenant_id"]
    assert first["tenant_id"] == first_invitation["tenant_id"]
    assert second["tenant_id"] == second_invitation["tenant_id"]
    for identifier in (first["user_id"], second["user_id"], first["tenant_id"], second["tenant_id"]):
        assert client.get(f"/users/{identifier}").status_code == 404
        assert client.get(f"/tenants/{identifier}").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_audit_is_complete_and_contains_no_replayable_secrets(identity) -> None:
    client, sender, _, database = identity
    invitation = invite(client, "audit@example.com")
    client.post(
        "/invitations/redeem",
        json={"invitation_token": "unknown-" + "u" * 40, "email": "audit@example.com"},
    )
    redemption = redeem(client, invitation)
    challenge, code = request_code(client, sender, redemption)
    client.post(
        "/verify/confirm",
        json={"challenge_id": challenge["challenge_id"], "verification_token": "wrong-" + "w" * 40},
    )
    assert client.post(
        "/verify/confirm", json={"challenge_id": challenge["challenge_id"], "verification_token": code}
    ).status_code == 200
    revoked = invite(client, "revoke-audit@example.com")
    client.post(f"/admin/invitations/{revoked['invitation_id']}/revoke", headers=auth(), json={})
    with closing(sqlite3.connect(database)) as db:
        rows = db.execute(
            "SELECT action, result, inviter, invitee_email, tenant_id, occurred_at FROM audit_log"
        ).fetchall()
        columns = {row[1] for row in db.execute("PRAGMA table_info(audit_log)")}
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("UPDATE audit_log SET result='tampered'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("DELETE FROM audit_log")
    actions = [(row[0], row[1]) for row in rows]
    assert ("invitation.create", "success") in actions
    assert ("invitation.redeem", "unavailable") in actions
    assert ("invitation.redeem", "success") in actions
    assert ("verification.request", "success") in actions
    assert ("verification.confirm", "failed") in actions
    assert ("verification.confirm", "success") in actions
    assert ("invitation.revoke", "success") in actions
    assert all(row[5] == 1_800_000_000.0 for row in rows)
    assert {"token", "code", "token_hash", "redemption_hash", "bearer"}.isdisjoint(columns)
    rendered = repr(rows)
    for secret in (ADMIN, invitation["invitation_token"], redemption["redemption_token"], code):
        assert secret not in rendered


def test_database_and_directory_are_private_and_state_survives_restart(tmp_path: Path) -> None:
    root = tmp_path / "durable"
    sender = RecordingEmailSender()
    app = create_app(state_root=root, admin_token=ADMIN, sender=sender)
    with TestClient(app) as client:
        invitation = invite(client, "durable@example.com")
    restarted = create_app(state_root=root, admin_token=ADMIN, sender=sender)
    with TestClient(restarted) as client:
        assert redeem(client, invitation)["status"] == "verification_required"
    if os.name == "posix":
        assert root.stat().st_mode & 0o777 == 0o700
        assert (root / "identity.sqlite3").stat().st_mode & 0o777 == 0o600

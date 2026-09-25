from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from tenant_policy_service.app import create_app

TOKEN = "p" * 48


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def assertion(key: Ed25519PrivateKey, *, user: str = "user-a", tenant: str = "tenant-a") -> str:
    claims = {
        "sub": user,
        "tenant": tenant,
        "purpose": "site_policy_change",
        "iat": 1_800_000_000,
        "exp": 1_800_000_060,
        "jti": "opaque",
    }
    payload = encoded(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    return payload + "." + encoded(key.sign(payload.encode("ascii")))


def test_apply_derives_owner_only_from_signed_assertion(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = encoded(private.public_key().public_bytes_raw())
    calls = []

    class FakeProvisioner:
        def update_allowed_hosts(self, enrollment, hosts, *, expected_revision):
            calls.append((enrollment.user_id, enrollment.tenant_id, hosts, expected_revision))
            return SimpleNamespace(allowed_hosts=hosts, policy_revision=expected_revision + 1)

    app = create_app(
        internal_token=TOKEN,
        portal_assertion_public_key=public,
        state_root=tmp_path,
        compose_file=tmp_path / "compose.yml",
        clock=lambda: 1_800_000_001,
        provisioner_factory=FakeProvisioner,
    )
    with TestClient(app) as client:
        response = client.post(
            "/internal/allowed-hosts/apply",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "portal_assertion": assertion(private),
                "allowed_hosts": ["EXAMPLE.com", "example.com"],
                "expected_revision": 4,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "allowed_hosts": ["example.com"],
            "revision": 5,
            "controller_restarted": True,
        }
        assert calls == [("user-a", "tenant-a", ("example.com",), 4)]

        forged = client.post(
            "/internal/allowed-hosts/apply",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "portal_assertion": assertion(private),
                "allowed_hosts": ["example.com"],
                "expected_revision": 5,
                "user_id": "victim",
            },
        )
        assert forged.status_code == 422


def test_apply_requires_internal_bearer_and_valid_unexpired_assertion(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = encoded(private.public_key().public_bytes_raw())
    app = create_app(
        internal_token=TOKEN,
        portal_assertion_public_key=public,
        state_root=tmp_path,
        compose_file=tmp_path / "compose.yml",
        clock=lambda: 1_800_000_100,
        provisioner_factory=lambda: None,
    )
    payload = {
        "portal_assertion": assertion(private),
        "allowed_hosts": ["example.com"],
        "expected_revision": 0,
    }
    with TestClient(app) as client:
        assert client.post("/internal/allowed-hosts/apply", json=payload).status_code == 401
        assert client.post(
            "/internal/allowed-hosts/apply",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=payload,
        ).status_code == 403

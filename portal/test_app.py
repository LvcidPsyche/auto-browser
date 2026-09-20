from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from approval_broker.app import TOTP_PERIOD, totp_code
from portal.app import BROKER_OPEN_PATH, create_app

IDENTITY_TOKEN = "i" * 40
BROKER_TOKEN = "b" * 40
GATEWAY_TOKEN = "g" * 40
BROKER_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
ORIGIN = "https://portal.example"


class Upstreams:
    def __init__(self) -> None:
        self.used: set[tuple[str, str]] = set()
        self.identity_calls: list[tuple[str, dict]] = []
        self.broker_calls: list[tuple[str, str, dict | None, str | None]] = []
        self.gateway_calls: list[tuple[str, str, dict | None]] = []
        self.broker_codes: set[str] = set()
        self.active = False
        self.fail_active_binding = False

    def identity(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.identity_calls.append((request.url.path, body))
        if request.url.path == "/internal/auth/verify":
            if request.headers.get("authorization") != f"Bearer {IDENTITY_TOKEN}":
                return httpx.Response(401, json={"detail": "bad service credential"})
            account, code = body["account"], body["totp_code"]
            replay = (account, code)
            if replay in self.used:
                return httpx.Response(403, json={"detail": "Code already used"})
            self.used.add(replay)
            suffix = "2" if account == "second@example.com" else "1"
            return httpx.Response(200, json={
                "user_id": f"user-{suffix}", "tenant_id": f"tenant-{suffix}", "account": account,
            })
        if request.url.path == "/invitations/redeem":
            return httpx.Response(200, json={"status": "pending_enrollment", "enrollment_id": "enroll-1",
                                             "secret": "ONE-TIME-SECRET",
                                             "provisioning_uri": "otpauth://totp/example"})
        if request.url.path == "/enrollments/confirm":
            return httpx.Response(200, json={
                "status": "enrolled", "recovery_codes": ["r1", "r2"],
                "internal_token": IDENTITY_TOKEN,
            })
        if request.url.path == "/internal/auth/recovery-codes":
            return httpx.Response(200, json={"recovery_codes": ["new-1"], "credential": "hidden"})
        if request.url.path == "/internal/auth/recover":
            return httpx.Response(200, json={"status": "pending_enrollment", "enrollment_id": "recover-1",
                                             "secret": "RECOVERY-SECRET"})
        return httpx.Response(404)

    def broker(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.broker_calls.append((request.method, request.url.path, body, request.headers.get("authorization")))
        if request.headers.get("authorization") != f"Bearer {BROKER_TOKEN}":
            return httpx.Response(401)
        if request.method == "POST" and request.url.path == BROKER_OPEN_PATH:
            if body["totp_code"] in self.broker_codes:
                return httpx.Response(403, json={"detail": "Authenticator code already used"})
            self.broker_codes.add(body["totp_code"])
            self.active = True
            return httpx.Response(200, json={
                "id": "browser-1", "owner_token": BROKER_TOKEN,
                "controller_url": "http://controller/private",
            })
        if request.method == "DELETE" and request.url.path == "/owner/sessions/browser-1":
            self.active = False
            return httpx.Response(200, json={"owner_credential": BROKER_TOKEN})
        return httpx.Response(404)

    def gateway(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.gateway_calls.append((request.method, request.url.path, body))
        if request.headers.get("authorization") != f"Bearer {GATEWAY_TOKEN}":
            return httpx.Response(401)
        if request.method == "GET" and request.url.path == "/internal/connected-clients":
            return httpx.Response(200, json={"connections": [{
                "connection_ref": "conn-1", "client_id": "claude", "client_name": "Claude",
                "application_type": "web", "capabilities": ["browser"],
                "access_token": "must-not-leak",
            }]})
        if request.url.path == "/internal/active-user":
            if self.fail_active_binding and body["active"]:
                return httpx.Response(503, json={"detail": "binding unavailable"})
            return httpx.Response(200, json={"active": body["active"]})
        if request.url.path.endswith("/disconnect"):
            return httpx.Response(200, json={"status": "ok", "secret": GATEWAY_TOKEN})
        if request.url.path == "/internal/consent/preview":
            return httpx.Response(200, json={"client_name": "Codex", "capabilities": ["Navigate", "Click"]})
        if request.url.path == "/internal/consent":
            return httpx.Response(200, json={"redirect_url": "https://client.example/callback?code=safe"})
        return httpx.Response(404)


@pytest.fixture
def clock() -> list[float]:
    return [1_800_000_000.0]


@pytest.fixture
def upstreams() -> Upstreams:
    return Upstreams()


def app_at(tmp_path: Path, clock: list[float], upstreams: Upstreams, **kwargs):
    return create_app(
        state_root=tmp_path / "portal-state",
        identity_internal_token=IDENTITY_TOKEN,
        broker_owner_token=BROKER_TOKEN,
        broker_totp_secret=kwargs.pop("broker_totp_secret", BROKER_TOTP_SECRET),
        gateway_internal_token=GATEWAY_TOKEN,
        public_origin=ORIGIN,
        identity_transport=httpx.MockTransport(upstreams.identity),
        broker_transport=httpx.MockTransport(upstreams.broker),
        gateway_transport=httpx.MockTransport(upstreams.gateway),
        clock=lambda: clock[0],
        **kwargs,
    )


def login(client: TestClient, account: str = "owner@example.com", code: str = "111111") -> str:
    response = client.post(
        "/signin", headers={"Origin": ORIGIN}, json={"account": account, "totp_code": code},
    )
    assert response.status_code == 200, response.text
    csrf = client.cookies.get("ab_portal_csrf")
    assert csrf
    return csrf


def mutate(csrf: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


def test_broker_totp_secret_is_required_to_have_160_bits(tmp_path, clock, upstreams):
    with pytest.raises(ValueError, match="160 bits"):
        app_at(tmp_path, clock, upstreams, broker_totp_secret="JBSWY3DP")


def test_login_cookie_is_hardened_and_replay_is_rejected(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        response = client.post(
            "/signin", headers={"Origin": ORIGIN},
            json={"account": "owner@example.com", "totp_code": "111111"},
        )
        assert response.status_code == 200
        cookie = response.headers.get("set-cookie", "")
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
        replay = client.post(
            "/signin", headers={"Origin": ORIGIN},
            json={"account": "owner@example.com", "totp_code": "111111"},
        )
        assert replay.status_code == 403


def test_fresh_login_rotates_and_revokes_previous_cookie(tmp_path, clock, upstreams):
    app = app_at(tmp_path, clock, upstreams)
    with TestClient(app, base_url=ORIGIN) as client:
        login(client)
        old = client.cookies.get("ab_portal_session")
        login(client, code="555555")
        assert client.cookies.get("ab_portal_session") != old
        client.cookies.set("ab_portal_session", old, domain="portal.example", path="/")
        assert client.get("/api/session").status_code == 401
        assert client.post(
            "/api/browser/open", headers={"Origin": ORIGIN, "X-CSRF-Token": "forged"}, json={},
        ).status_code == 401
        with app.state.portal_store.connect() as db:
            cleared = db.execute(
                "SELECT authenticated_at FROM portal_sessions WHERE revoked_at IS NOT NULL"
            ).fetchall()
        assert cleared and all(row["authenticated_at"] == 0 for row in cleared)


def test_cookie_forgery_logout_and_csrf(tmp_path, clock, upstreams):
    app = app_at(tmp_path, clock, upstreams)
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set("ab_portal_session", "forged", domain="portal.example", path="/")
        assert client.get("/api/session").status_code == 401
        client.cookies.clear()
        csrf = login(client)
        assert client.post("/logout", headers={"Origin": ORIGIN}, json={}).status_code == 403
        assert client.post("/logout", headers=mutate(csrf), json={}).status_code == 200
        assert client.get("/api/session").status_code == 401
        assert client.post(
            "/api/browser/open", headers=mutate(csrf), json={},
        ).status_code == 401
        with app.state.portal_store.connect() as db:
            row = db.execute("SELECT authenticated_at FROM portal_sessions").fetchone()
        assert row["authenticated_at"] == 0


def test_absolute_and_idle_expiry(tmp_path, clock, upstreams):
    app = app_at(tmp_path, clock, upstreams, absolute_session_ttl=100, idle_session_ttl=10)
    with TestClient(app, base_url=ORIGIN) as client:
        login(client)
        clock[0] += 11
        assert client.get("/api/session").status_code == 401
        assert client.post(
            "/api/browser/open", headers={"Origin": ORIGIN, "X-CSRF-Token": "expired"}, json={},
        ).status_code == 401
        with app.state.portal_store.connect() as db:
            row = db.execute("SELECT authenticated_at FROM portal_sessions").fetchone()
        assert row["authenticated_at"] == 0

    absolute_clock = [1_800_100_000.0]
    absolute = app_at(
        tmp_path / "absolute", absolute_clock, Upstreams(),
        absolute_session_ttl=10, idle_session_ttl=1000,
    )
    with TestClient(absolute, base_url=ORIGIN) as client:
        login(client)
        absolute_clock[0] += 9
        assert client.get("/api/session").status_code == 200
        absolute_clock[0] += 2
        assert client.get("/api/session").status_code == 401


def test_open_inside_freshness_window_needs_no_second_human_code(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        csrf = login(client)
        response = client.post(
            "/api/browser/open", headers=mutate(csrf),
            json={"start_url": "https://example.com"},
        )
        assert response.json() == {"status": "open", "session_id": "browser-1"}
        assert [body["purpose"] for _, body in upstreams.identity_calls] == ["portal_login"]
        expected = totp_code(BROKER_TOTP_SECRET, int(clock[0] // TOTP_PERIOD))
        assert upstreams.broker_calls[-1][2]["totp_code"] == expected
        assert upstreams.broker_calls[-1][3] == f"Bearer {BROKER_TOKEN}"


def test_open_outside_freshness_window_requires_code_and_refreshes_it(tmp_path, clock, upstreams):
    with TestClient(
        app_at(tmp_path, clock, upstreams, authentication_freshness_ttl=120), base_url=ORIGIN,
    ) as client:
        csrf = login(client)
        clock[0] += 120
        assert "Fresh authenticator code" in client.get("/browser").text
        missing = client.post("/api/browser/open", headers=mutate(csrf), json={})
        assert missing.status_code == 422
        opened = client.post(
            "/api/browser/open", headers=mutate(csrf), json={"totp_code": "333333"},
        )
        assert opened.status_code == 200
        assert upstreams.identity_calls[-1][1] == {
            "account": "owner@example.com", "totp_code": "333333", "purpose": "browser_open",
        }
        assert client.get("/api/session").json()["browser_open_requires_code"] is False


def test_client_input_cannot_forge_or_extend_freshness(tmp_path, clock, upstreams):
    app = app_at(tmp_path, clock, upstreams, authentication_freshness_ttl=120)
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = login(client)
        with app.state.portal_store.connect() as db:
            authenticated_at = db.execute(
                "SELECT authenticated_at FROM portal_sessions WHERE revoked_at IS NULL"
            ).fetchone()["authenticated_at"]
        clock[0] += 120
        forged = client.post(
            "/api/browser/open?authentication_freshness_ttl=999999",
            headers={**mutate(csrf), "X-Authentication-Fresh": "true"},
            json={"authenticated_at": clock[0], "auth_fresh_until": clock[0] + 999999},
        )
        assert forged.status_code == 422
        assert not upstreams.broker_calls
        assert client.get("/api/session").json()["browser_open_requires_code"] is True
        with app.state.portal_store.connect() as db:
            unchanged = db.execute(
                "SELECT authenticated_at FROM portal_sessions WHERE revoked_at IS NULL"
            ).fetchone()["authenticated_at"]
        assert unchanged == authenticated_at


def test_login_code_cannot_be_reused_to_open(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        csrf = login(client)
        opened = client.post(
            "/api/browser/open", headers=mutate(csrf), json={"totp_code": "111111"},
        )
        assert opened.status_code == 200
        assert [body["purpose"] for _, body in upstreams.identity_calls] == ["portal_login"]
        assert client.post("/api/browser/close", headers=mutate(csrf), json={}).status_code == 200
        upstreams.broker_calls.clear()
        clock[0] += 120
        denied = client.post(
            "/api/browser/open", headers=mutate(csrf), json={"totp_code": "111111"},
        )
        assert denied.status_code == 403
        assert not upstreams.broker_calls


def test_gateway_binding_failure_closes_broker_and_releases_owner(tmp_path, clock, upstreams):
    upstreams.fail_active_binding = True
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        csrf = login(client)
        failed = client.post(
            "/api/browser/open", headers=mutate(csrf), json={"totp_code": "333333"},
        )
        assert failed.status_code == 502
        assert [(method, path) for method, path, _, _ in upstreams.broker_calls] == [
            ("POST", "/owner/sessions"), ("DELETE", "/owner/sessions/browser-1"),
        ]
        assert client.get("/api/session").json()["browser"] == "closed"


def test_second_distinct_user_fails_closed_while_node_is_owned(tmp_path, clock, upstreams):
    app = app_at(tmp_path, clock, upstreams)
    with TestClient(app, base_url=ORIGIN) as first:
        csrf = login(first)
        assert first.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "333333"}).status_code == 200
        first.cookies.clear()
        csrf = login(first, "second@example.com", "222222")
        denied = first.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "444444"})
        assert denied.status_code == 409
        assert len(upstreams.broker_calls) == 1


def test_close_clears_local_owner_and_allows_next_user(tmp_path, clock, upstreams):
    app = app_at(tmp_path, clock, upstreams)
    with TestClient(app, base_url=ORIGIN) as first:
        csrf = login(first)
        first.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "333333"})
        assert first.post("/api/browser/close", headers=mutate(csrf), json={}).json() == {"status": "closed"}
        first.cookies.clear()
        csrf = login(first, "second@example.com", "222222")
        assert first.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "444444"}).status_code == 200


def test_broker_credential_and_private_fields_never_leak(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        csrf = login(client)
        opened = client.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "333333"})
        listed = client.get("/api/connections")
        client.post("/api/browser/close", headers=mutate(csrf), json={})
        combined = opened.text + listed.text
        assert BROKER_TOKEN not in combined
        assert BROKER_TOTP_SECRET not in combined
        assert GATEWAY_TOKEN not in combined
        assert "controller_url" not in combined and "access_token" not in combined


def test_connections_are_scoped_and_disconnect_is_narrow(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        csrf = login(client)
        listed = client.get("/api/connections").json()
        assert listed == {"connections": [{
            "connection_ref": "conn-1", "client_id": "claude", "client_name": "Claude",
            "application_type": "web", "capabilities": ["browser"],
        }]}
        query = upstreams.gateway_calls[-1]
        assert query[1] == "/internal/connected-clients"
        disconnected = client.post(
            "/api/connections/conn-1/disconnect", headers=mutate(csrf), json={},
        )
        assert disconnected.json() == {"status": "disconnected", "connection_id": "conn-1"}
        assert upstreams.gateway_calls[-1][2] == {"user_id": "user-1", "tenant_id": "tenant-1"}
        for forbidden in ("/proxy", "/api/proxy", "/vnc", "/controller", "/owner/sessions"):
            assert client.get(forbidden).status_code == 404


def test_invitation_secrets_are_no_store_shown_once_and_filtered(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        redeemed = client.post(
            "/api/invitations/redeem", headers={"Origin": ORIGIN},
            json={"invitation_token": "invite-token-long-enough", "display_name": "Owner",
                  "recovery_email": "owner@example.com"},
        )
        assert redeemed.json()["enrollment_id"] == "enroll-1"
        assert redeemed.json()["secret"] == "ONE-TIME-SECRET"
        confirmed = client.post(
            "/api/enrollments/confirm", headers={"Origin": ORIGIN},
            json={"enrollment_id": "enroll-1", "totp_code": "123456"},
        )
        assert confirmed.json()["recovery_codes"] == ["r1", "r2"]
        assert "internal_token" not in confirmed.json()
        assert confirmed.headers["cache-control"].startswith("no-store")


def test_recovery_regeneration_requires_session_csrf_and_fresh_totp(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        assert client.post(
            "/api/recovery-codes/regenerate", headers={"Origin": ORIGIN}, json={"totp_code": "333333"},
        ).status_code == 401
        csrf = login(client)
        regenerated = client.post(
            "/api/recovery-codes/regenerate", headers=mutate(csrf), json={"totp_code": "333333"},
        )
        assert regenerated.json() == {"recovery_codes": ["new-1"]}
        assert upstreams.identity_calls[-1][1] == {
            "account": "owner@example.com", "totp_code": "333333", "purpose": "recovery_codes",
        }


def test_recovery_flow_proxies_one_time_enrollment_without_persistence(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        begun = client.post(
            "/api/recovery/begin", headers={"Origin": ORIGIN},
            json={"account": "Owner", "recovery_code": "recovery-code"},
        )
        assert begun.json()["enrollment_id"] == "recover-1"
        assert begun.json()["secret"] == "RECOVERY-SECRET"
        confirmed = client.post(
            "/api/recovery/confirm", headers={"Origin": ORIGIN},
            json={"enrollment_id": "recover-1", "totp_code": "123456"},
        )
        assert confirmed.json()["recovery_codes"] == ["r1", "r2"]
        assert upstreams.identity_calls[-2][0] == "/internal/auth/recover"
        assert upstreams.identity_calls[-2][1] == {"account": "Owner", "recovery_code": "recovery-code"}


def test_oauth_consent_uses_gateway_preview_and_only_returned_redirect(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN, follow_redirects=False) as client:
        csrf = login(client)
        params = {"authorization_request": "authorization-request-secret"}
        preview = client.get("/oauth/authorize", params=params)
        assert preview.status_code == 200
        assert "Codex" in preview.text and "Navigate" in preview.text
        consent = client.post(
            "/oauth/authorize", headers=mutate(csrf),
            data={"authorization_request": params["authorization_request"],
                  "decision": "approve", "csrf_token": csrf},
        )
        assert consent.status_code == 303
        assert consent.headers["location"] == "https://client.example/callback?code=safe"
        assert "evil.example" not in consent.headers["location"]


def test_authenticated_browser_page_matches_gateway_portal_link(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        unauthenticated = client.get("/browser?connection=opaque")
        assert unauthenticated.status_code == 200
        assert unauthenticated.history[0].status_code == 303
        assert unauthenticated.history[0].headers["location"] == "/signin?next=%2Fbrowser"
        login(client)
        page = client.get("/browser?connection=opaque")
        assert page.status_code == 200
        assert "Fresh authenticator code" not in page.text
        assert "/api/browser/open" in page.text and "/api/browser/close" in page.text
        clock[0] += 120
        assert "Fresh authenticator code" in client.get("/browser").text


def test_all_responses_get_security_headers_and_mutations_require_origin(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        page = client.get("/signin")
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["strict-transport-security"].startswith("max-age=")
        denied = client.post("/signin", json={"account": "owner@example.com", "totp_code": "111111"})
        assert denied.status_code == 403

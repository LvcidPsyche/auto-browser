from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from portal.app import BROKER_OPEN_PATH, SOLE_OWNER_DENIAL_AR, create_app

IDENTITY_TOKEN = "i" * 40
BROKER_TOKEN = "b" * 40
GATEWAY_TOKEN = "g" * 40
ASSERTION_PRIVATE_KEY = base64.urlsafe_b64encode(
    Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
).rstrip(b"=").decode()
ORIGIN = "https://portal.example"


class Upstreams:
    def __init__(self) -> None:
        self.used: set[tuple[str, str]] = set()
        self.identity_calls: list[tuple[str, dict]] = []
        self.broker_calls: list[tuple[str, str, dict | None, str | None]] = []
        self.gateway_calls: list[tuple[str, str, dict | None]] = []
        self.broker_assertions: set[str] = set()
        self.active = False
        self.fail_active_binding = False
        self.site_requests: list[dict] = []
        self.profile_calls: list[tuple] = []
        self.saved_profiles: set[str] = set()
        self.novnc_calls: list[str] = []
        self.typed_text: list[str] = []

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
            if body["portal_assertion"] in self.broker_assertions:
                return httpx.Response(403, json={"detail": "Assertion already used"})
            self.broker_assertions.add(body["portal_assertion"])
            self.active = True
            return httpx.Response(200, json={
                "id": "browser-1", "owner_token": BROKER_TOKEN,
                "controller_url": "http://controller/private",
            })
        if request.method == "DELETE" and request.url.path == "/owner/sessions/browser-1":
            self.active = False
            return httpx.Response(200, json={"owner_credential": BROKER_TOKEN})
        if request.method == "GET" and request.url.path == "/owner/visual-access":
            if self.active:
                return httpx.Response(200, json={"session_id": "browser-1"})
            return httpx.Response(403, json={"detail": "Owner must open a verified browser session first"})
        if request.method == "POST" and request.url.path == "/owner/sessions/browser-1/auth-profiles":
            self.profile_calls.append(("save", body["profile_name"]))
            self.saved_profiles.add(body["profile_name"])
            return httpx.Response(200, json={"profile_name": body["profile_name"]})
        if request.method == "POST" and request.url.path == "/owner/sessions/browser-1/type":
            if not self.active:
                return httpx.Response(403, json={"detail": "Owner must open a verified browser session first"})
            self.typed_text.append(body["text"])
            return httpx.Response(200, json={"action": "type"})
        if request.method == "POST" and request.url.path.startswith("/owner/auth-profiles/") and request.url.path.endswith("/rename"):
            name = request.url.path.split("/")[3]
            self.profile_calls.append(("rename", name, body["new_name"]))
            self.saved_profiles.discard(name)
            self.saved_profiles.add(body["new_name"])
            return httpx.Response(200, json={"profile_name": body["new_name"], "previous_name": name})
        if request.method == "DELETE" and request.url.path.startswith("/owner/auth-profiles/"):
            name = request.url.path.split("/")[3]
            self.profile_calls.append(("delete", name))
            self.saved_profiles.discard(name)
            return httpx.Response(200, json={"profile_name": name, "deleted": True})
        if request.method in ("GET", "HEAD") and request.url.path.startswith("/owner/vnc/"):
            self.novnc_calls.append(request.url.path.removeprefix("/owner/vnc"))
            if not self.active:
                return httpx.Response(403, json={"detail": "Owner must open a verified browser session first"})
            if request.url.path == "/owner/vnc/vnc.html":
                return httpx.Response(200, text="<html>fake novnc page</html>", headers={"content-type": "text/html"})
            return httpx.Response(404)
        return httpx.Response(404)

    def gateway(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.gateway_calls.append((request.method, request.url.path, body))
        if request.url.path.startswith("/internal/site-requests"):
            authorization = request.headers.get("authorization", "")
            if not authorization.startswith("Bearer ") or authorization == f"Bearer {GATEWAY_TOKEN}":
                return httpx.Response(401)
            if request.method == "GET" and request.url.path == "/internal/site-requests":
                return httpx.Response(200, json={"requests": self.site_requests})
            if request.method == "GET":
                request_id = request.url.path.rsplit("/", 1)[-1]
                item = next((item for item in self.site_requests if item["request_id"] == request_id), None)
                return httpx.Response(200, json=item) if item else httpx.Response(404)
            if request.method == "POST" and request.url.path.endswith("/decision"):
                request_id = request.url.path.split("/")[-2]
                item = next((item for item in self.site_requests if item["request_id"] == request_id), None)
                if item is None:
                    return httpx.Response(404)
                item["status"] = body["decision"]
                return httpx.Response(200, json=item)
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
        portal_assertion_private_key=kwargs.pop(
            "portal_assertion_private_key", ASSERTION_PRIVATE_KEY
        ),
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


def test_portal_assertion_private_key_must_be_valid(tmp_path, clock, upstreams):
    with pytest.raises(ValueError, match="Ed25519"):
        app_at(tmp_path, clock, upstreams, portal_assertion_private_key="invalid")


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
        assertion = upstreams.broker_calls[-1][2]["portal_assertion"]
        assert assertion.count(".") == 1
        assert "totp_code" not in upstreams.broker_calls[-1][2]
        assert upstreams.broker_calls[-1][3] == f"Bearer {BROKER_TOKEN}"


def test_repeated_open_rejoins_live_browser_without_second_broker_open(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        csrf = login(client)
        first = client.post("/api/browser/open", headers=mutate(csrf), json={})
        repeated = client.post("/api/browser/open", headers=mutate(csrf), json={})

        assert first.status_code == 200
        assert repeated.status_code == 200
        assert repeated.json() == first.json() == {"status": "open", "session_id": "browser-1"}
        opens = [call for call in upstreams.broker_calls if call[:2] == ("POST", BROKER_OPEN_PATH)]
        assert len(opens) == 1


def test_second_open_waits_for_identical_inflight_request(tmp_path, clock, upstreams):
    app = app_at(tmp_path, clock, upstreams)
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = login(client)
        with app.state.portal_store.connect() as db:
            db.execute(
                "INSERT INTO browser_ownership_v2"
                "(user_id,tenant_id,broker_session_id,claimed_at) VALUES(?,?,NULL,?)",
                ("user-1", "tenant-1", clock[0]),
            )

        def finish_first_open() -> None:
            time.sleep(0.1)
            upstreams.active = True
            with app.state.portal_store.connect() as db:
                db.execute(
                    "UPDATE browser_ownership_v2 SET broker_session_id=? "
                    "WHERE user_id=? AND tenant_id=?",
                    ("browser-1", "user-1", "tenant-1"),
                )

        finisher = threading.Thread(target=finish_first_open)
        finisher.start()
        repeated = client.post("/api/browser/open", headers=mutate(csrf), json={})
        finisher.join(timeout=2)

        assert not finisher.is_alive()
        assert repeated.status_code == 200
        assert repeated.json() == {"status": "open", "session_id": "browser-1"}
        assert not [call for call in upstreams.broker_calls if call[:2] == ("POST", BROKER_OPEN_PATH)]


def test_open_browser_keeps_portal_session_alive_until_manual_close(tmp_path, clock, upstreams):
    app = app_at(tmp_path, clock, upstreams, idle_session_ttl=10, absolute_session_ttl=20)
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = login(client)
        opened = client.post("/api/browser/open", headers=mutate(csrf), json={})
        assert opened.status_code == 200

        # Both normal limits are long past, but this identity still owns the live browser.
        clock[0] += 1_000
        state = client.get("/api/session")
        assert state.status_code == 200
        assert state.json()["browser"] == "open"

        closed = client.post("/api/browser/close", headers=mutate(csrf), json={})
        assert closed.status_code == 200
        assert client.get("/api/session").status_code == 401


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


def test_manual_close_ends_portal_session_and_login_code_cannot_be_reused(tmp_path, clock, upstreams):
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
        assert denied.status_code == 401
        replayed_login = client.post(
            "/signin",
            headers={"Origin": ORIGIN},
            json={"account": "owner@example.com", "totp_code": "111111"},
        )
        assert replayed_login.status_code == 403
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


def test_second_distinct_user_gets_independent_ownership(tmp_path, clock, upstreams):
    app = app_at(tmp_path, clock, upstreams)
    with TestClient(app, base_url=ORIGIN) as first:
        csrf = login(first)
        assert first.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "333333"}).status_code == 200
        first.cookies.clear()
        csrf = login(first, "second@example.com", "222222")
        opened = first.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "444444"})
        assert opened.status_code == 200
        assert len(upstreams.broker_calls) == 2


def test_broker_route_uses_authenticated_identity_and_ignores_client_ids(
    tmp_path, clock, upstreams
):
    routed: list[tuple[str, str]] = []
    calls: list[tuple[str, str]] = []

    def make_broker(label: str) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((label, request.headers["authorization"]))
            return httpx.Response(200, json={"id": f"browser-{label}"})

        return httpx.AsyncClient(
            base_url=f"http://broker-{label}:18001", transport=httpx.MockTransport(handler)
        )

    brokers = {"user-1": make_broker("one"), "user-2": make_broker("two")}

    def resolver(user_id: str, tenant_id: str):
        routed.append((user_id, tenant_id))
        suffix = "one" if user_id == "user-1" else "two"
        return brokers[user_id], f"owner-{suffix}-" + "x" * 32

    app = app_at(tmp_path, clock, upstreams, broker_resolver=resolver)
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = login(client)
        opened = client.post("/api/browser/open", headers=mutate(csrf), json={
            "user_id": "user-2", "tenant_id": "tenant-2", "stack": "two",
        })
        assert opened.json()["session_id"] == "browser-one"
    asyncio.run(brokers["user-1"].aclose())
    asyncio.run(brokers["user-2"].aclose())
    assert routed == [("user-1", "tenant-1")]
    assert calls == [("one", "Bearer owner-one-" + "x" * 32)]


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
        assert ASSERTION_PRIVATE_KEY not in combined
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
        for forbidden in ("/proxy", "/api/proxy", "/controller", "/owner/sessions"):
            assert client.get(forbidden).status_code == 404
        # /vnc is now a real, authenticated route (the noVNC viewer proxy), so
        # it must not 404 -- but with no open browser it must still refuse,
        # never fall through to the private noVNC socket.
        assert client.get("/vnc").status_code == 403


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


def test_public_invitation_page_completes_authenticator_enrollment(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        invitation = client.get("/invite/invite-token-long-enough")
        assert invitation.status_code == 200
        assert "action=/enroll" in invitation.text
        enrolled = client.post(
            "/enroll",
            headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
            content=(
                "invitation_token=invite-token-long-enough&display_name=Owner"
                "&recovery_email=owner%40example.com"
            ),
        )
        assert enrolled.status_code == 200
        # The tap-to-open link (useful on the same phone) still carries the
        # full otpauth URI, and a scannable QR is now rendered inline...
        assert "otpauth://totp/example" in enrolled.text
        assert "<svg" in enrolled.text
        # ...but the raw manual-entry secret only appears behind the
        # "اضغط لإظهار المفتاح" collapse, never as bare visible page text.
        assert "اضغط لإظهار المفتاح" in enrolled.text
        assert "<details>" in enrolled.text
        assert "ONE-TIME-SECRET" in enrolled.text
        confirmed = client.post(
            "/enroll/confirm",
            headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
            content="enrollment_id=enroll-1&totp_code=123456",
        )
        assert confirmed.status_code == 200
        assert "r1" in confirmed.text and "r2" in confirmed.text
        assert "Continue to sign in" in confirmed.text


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


def test_allowed_sites_are_user_scoped_normalized_and_require_fresh_auth(tmp_path, clock, upstreams):
    policies = {
        ("user-1", "tenant-1"): {"allowed_hosts": ["example.com", "keep.example"], "revision": 1},
        ("user-2", "tenant-2"): {"allowed_hosts": ["private.example"], "revision": 4},
    }

    def resolve(user_id: str, tenant_id: str):
        return policies[(user_id, tenant_id)]

    async def apply(user_id: str, tenant_id: str, hosts: list[str], revision: int):
        current = policies[(user_id, tenant_id)]
        assert revision == current["revision"]
        current["allowed_hosts"] = list(hosts)
        current["revision"] += 1
        return current.copy()

    app = app_at(tmp_path, clock, upstreams, policy_resolver=resolve, policy_applier=apply)
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = login(client)
        added = client.post(
            "/api/sites", headers=mutate(csrf), json={"hostname": " BÜCHER.example "},
        )
        assert added.status_code == 200
        assert added.json()["allowed_hosts"] == ["example.com", "keep.example", "xn--bcher-kva.example"]
        assert client.post(
            "/api/sites", headers=mutate(csrf), json={"hostname": "EXAMPLE.com"},
        ).status_code == 409
        assert client.post(
            "/api/sites", headers=mutate(csrf), json={"hostname": "a" * 254},
        ).status_code == 422

        clock[0] += 120
        assert client.request(
            "DELETE", "/api/sites/example.com", headers=mutate(csrf), json={},
        ).status_code == 422
        removed = client.request(
            "DELETE", "/api/sites/example.com", headers=mutate(csrf),
            json={"totp_code": "222222"},
        )
        assert removed.status_code == 200
        assert policies[("user-1", "tenant-1")]["allowed_hosts"] == [
            "keep.example", "xn--bcher-kva.example",
        ]
        assert policies[("user-2", "tenant-2")]["allowed_hosts"] == ["private.example"]
        assert upstreams.identity_calls[-1][1]["purpose"] == "site_policy_change"


def test_site_request_approval_applies_before_decision_and_failed_apply_keeps_policy(
    tmp_path, clock, upstreams,
):
    policy = {"allowed_hosts": ["example.com"], "revision": 1}
    upstreams.site_requests = [{
        "request_id": "request-1", "hostname": "shop.example", "reason": "Check orders",
        "created_at": clock[0], "expires_at": clock[0] + 300, "status": "pending",
    }]
    fail = [True]

    def resolve(_user_id: str, _tenant_id: str):
        return policy.copy()

    async def apply(_user_id: str, _tenant_id: str, hosts: list[str], revision: int):
        assert revision == policy["revision"]
        if fail[0]:
            raise RuntimeError("simulated apply failure")
        policy["allowed_hosts"] = list(hosts)
        policy["revision"] += 1
        return policy.copy()

    app = app_at(tmp_path, clock, upstreams, policy_resolver=resolve, policy_applier=apply)
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = login(client)
        page = client.get("/sites")
        assert "shop.example" in page.text and "Check orders" in page.text
        failed = client.post(
            "/api/site-requests/request-1/decision", headers=mutate(csrf),
            json={"decision": "approved"},
        )
        assert failed.status_code == 502
        assert policy == {"allowed_hosts": ["example.com"], "revision": 1}
        assert upstreams.site_requests[0]["status"] == "pending"

        fail[0] = False
        approved = client.post(
            "/api/site-requests/request-1/decision", headers=mutate(csrf),
            json={"decision": "approved"},
        )
        assert approved.status_code == 200
        assert policy["allowed_hosts"] == ["example.com", "shop.example"]
        assert upstreams.site_requests[0]["status"] == "approved"
        decision_call = upstreams.gateway_calls[-1]
        assert decision_call[2] == {"decision": "approved"}


def test_enrollment_qr_renders_inline_without_logging_the_secret(tmp_path, clock, upstreams, caplog):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        with caplog.at_level("DEBUG"):
            enrolled = client.post(
                "/enroll",
                headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
                content="invitation_token=invite-token-long-enough&display_name=Owner",
            )
        assert enrolled.status_code == 200
        # A real, scannable QR is rendered inline -- no third-party QR service
        # is ever contacted (there is no outbound call to make one at all).
        assert "<svg" in enrolled.text
        assert "ONE-TIME-SECRET" not in caplog.text
        assert "otpauth://totp/example" not in caplog.text


def test_viewer_denies_unauthenticated_missing_session_and_cross_identity(tmp_path, clock, upstreams):
    app = app_at(tmp_path, clock, upstreams)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/vnc/vnc.html").status_code == 401
        csrf = login(client)
        denied = client.get("/vnc/vnc.html")
        assert denied.status_code == 403
        assert denied.json()["detail"] == "لا يمكن عرض المتصفح الآن — يجب فتح جلسة متصفح أولاً من نفس حسابك."
        assert not upstreams.novnc_calls

        opened = client.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "333333"})
        assert opened.status_code == 200
        served = client.get("/vnc/vnc.html")
        assert served.status_code == 200
        assert "fake novnc page" in served.text
        assert upstreams.novnc_calls == ["/vnc.html"]

        client.cookies.clear()
        login(client, "second@example.com", "222222")
        other = client.get("/vnc/vnc.html")
        assert other.status_code == 403
        assert other.json()["detail"] == SOLE_OWNER_DENIAL_AR
        assert upstreams.novnc_calls == ["/vnc.html"]


def test_type_bridge_forwards_text_and_stays_owner_only(tmp_path, clock, upstreams):
    """The Arabic-typing bridge next to the viewer: same gates as the viewer itself.

    The noVNC keyboard channel does not reliably carry Arabic typed through a
    phone IME, so the owner clicks a field in the viewer and sends text through
    this box instead. It must require an open, owned session -- exactly like
    the viewer -- and forward the text unchanged to the one live session.
    """
    app = app_at(tmp_path, clock, upstreams)
    with TestClient(app, base_url=ORIGIN) as client:
        unauthenticated = client.post("/api/browser/type", headers={"Origin": ORIGIN}, json={"text": "hi"})
        assert unauthenticated.status_code == 401

        csrf = login(client)
        not_open = client.post("/api/browser/type", headers=mutate(csrf), json={"text": "hi"})
        assert not_open.status_code == 403
        assert not_open.json()["detail"] == "لا يمكن عرض المتصفح الآن — يجب فتح جلسة متصفح أولاً من نفس حسابك."
        assert not upstreams.typed_text

        opened = client.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "333333"})
        assert opened.status_code == 200

        sent = client.post("/api/browser/type", headers=mutate(csrf), json={"text": "مرحبا"})
        assert sent.status_code == 200
        assert sent.json() == {"status": "sent"}
        assert upstreams.typed_text == ["مرحبا"]

        client.cookies.clear()
        second_csrf = login(client, "second@example.com", "222222")
        other = client.post("/api/browser/type", headers=mutate(second_csrf), json={"text": "nope"})
        assert other.status_code == 403
        assert other.json()["detail"] == SOLE_OWNER_DENIAL_AR
        assert upstreams.typed_text == ["مرحبا"]


def test_viewer_websocket_denies_without_session_and_rechecks_each_frame(tmp_path, clock, upstreams, monkeypatch):
    import portal.app as portal_app

    sent: list[bytes] = []

    class FakeUpstream:
        async def send(self, frame):
            sent.append(frame)

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(30)

    @asynccontextmanager
    async def fake_connect(*_args, **_kwargs):
        yield FakeUpstream()

    monkeypatch.setattr(portal_app, "ws_connect", fake_connect)
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        # starlette's TestClient does not carry the shared cookie jar into a
        # websocket handshake automatically, unlike a real same-origin browser
        # tab opening a WebSocket back to its own page; pass it explicitly.
        def connect():
            cookie_header = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
            return client.websocket_connect("/vnc/websockify", headers={"Cookie": cookie_header})

        with pytest.raises(Exception):
            with connect():
                pass
        csrf = login(client)
        with pytest.raises(Exception):
            with connect():
                pass
        client.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "333333"})
        with connect() as socket:
            socket.send_bytes(b"first-frame")
            upstreams.active = False
            socket.send_bytes(b"second-frame")
            with pytest.raises(Exception):
                socket.receive_bytes()
        assert b"second-frame" not in sent


def test_live_viewer_renews_idle_session_and_can_reconnect(tmp_path, clock, upstreams, monkeypatch):
    import portal.app as portal_app

    class FakeUpstream:
        async def send(self, _frame):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(30)

    @asynccontextmanager
    async def fake_connect(*_args, **_kwargs):
        yield FakeUpstream()

    monkeypatch.setattr(portal_app, "ws_connect", fake_connect)
    app = app_at(tmp_path, clock, upstreams, idle_session_ttl=6, absolute_session_ttl=60)
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = login(client)
        client.post("/api/browser/open", headers=mutate(csrf), json={})

        def connect():
            cookie_header = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
            return client.websocket_connect("/vnc/websockify", headers={"Cookie": cookie_header})

        with app.state.portal_store.connect() as db:
            before = db.execute(
                "SELECT idle_expires_at FROM portal_sessions WHERE revoked_at IS NULL"
            ).fetchone()["idle_expires_at"]

        with connect():
            clock[0] += 3
            time.sleep(2.2)

        with app.state.portal_store.connect() as db:
            after = db.execute(
                "SELECT idle_expires_at FROM portal_sessions WHERE revoked_at IS NULL"
            ).fetchone()["idle_expires_at"]
        assert after > before

        # Past the original idle deadline, but still inside the heartbeat-renewed lease.
        clock[0] = before + 1
        with connect():
            pass


def test_auth_profiles_save_list_rename_delete_round_trip(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        csrf = login(client)
        client.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "333333"})

        saved = client.post("/api/auth-profiles", headers=mutate(csrf), json={"profile_name": "facebook"})
        assert saved.status_code == 200
        assert saved.json() == {"status": "saved", "profile_name": "facebook"}
        assert ("save", "facebook") in upstreams.profile_calls
        assert client.get("/api/auth-profiles").json() == {"profiles": ["facebook"]}

        renamed = client.post(
            "/api/auth-profiles/facebook/rename", headers=mutate(csrf), json={"new_name": "fb"},
        )
        assert renamed.status_code == 200
        assert client.get("/api/auth-profiles").json() == {"profiles": ["fb"]}

        deleted = client.request(
            "DELETE", "/api/auth-profiles/fb", headers=mutate(csrf), json={},
        )
        assert deleted.status_code == 200
        assert client.get("/api/auth-profiles").json() == {"profiles": []}


def test_auth_profiles_are_denied_to_a_second_identity(tmp_path, clock, upstreams):
    with TestClient(app_at(tmp_path, clock, upstreams), base_url=ORIGIN) as client:
        csrf = login(client)
        client.post("/api/browser/open", headers=mutate(csrf), json={"totp_code": "333333"})
        client.post("/api/auth-profiles", headers=mutate(csrf), json={"profile_name": "facebook"})

        client.cookies.clear()
        csrf2 = login(client, "second@example.com", "222222")
        denied_list = client.get("/api/auth-profiles")
        assert denied_list.status_code == 403
        assert denied_list.json()["detail"] == SOLE_OWNER_DENIAL_AR

        # A second identity must never be able to open pre-logged-in as the
        # owner's saved Facebook login, even by guessing the saved name...
        denied_open = client.post(
            "/api/browser/open", headers=mutate(csrf2),
            json={"totp_code": "444444", "auth_profile": "facebook"},
        )
        assert denied_open.status_code == 403
        assert denied_open.json()["detail"] == SOLE_OWNER_DENIAL_AR
        # ...but it can still open its own plain browser (unchanged,
        # pre-existing behaviour) once it stops asking for someone else's login.
        assert client.post(
            "/api/browser/open", headers=mutate(csrf2), json={"totp_code": "555555"},
        ).status_code == 200

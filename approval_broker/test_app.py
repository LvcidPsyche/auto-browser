from __future__ import annotations

import asyncio
import base64
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from approval_broker.app import create_app, totp_code

OWNER, AGENT, OTHER, UPSTREAM = "o" * 40, "a" * 40, "b" * 40, "u" * 40


def auth(token: str) -> dict[str, str]: return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    value = [1_700_000_000.0]
    monkeypatch.setattr("approval_broker.app.time.time", lambda: value[0])
    return value


def app_at(tmp_path: Path, handler, **kwargs):
    private = tmp_path / "private"; private.mkdir(mode=0o700, exist_ok=True)
    return create_app(owner_token=OWNER, agent_token=AGENT, agent_tokens=kwargs.pop("agent_tokens", None),
                      upstream_token=UPSTREAM, transport=httpx.MockTransport(handler),
                      totp_db_path=private / "totp.sqlite3", **kwargs)


def enroll(client: TestClient, clock: list[float]) -> str:
    secret = client.post("/owner/totp/setup", headers=auth(OWNER)).json()["secret"]
    assert client.post("/owner/totp/activate", headers=auth(OWNER), json={"totp_code": totp_code(secret, int(clock[0] // 30))}).status_code == 200
    return secret


def fresh(secret: str, clock: list[float]) -> str:
    clock[0] += 30
    return totp_code(secret, int(clock[0] // 30))


def signed_assertion(
    key: Ed25519PrivateKey, clock: list[float], *, user: str = "user-a",
    tenant: str = "tenant-a", purpose: str = "browser_open", expires_in: int = 60,
    jti: str = "assertion-id-123456",
) -> str:
    claims = {
        "sub": user, "tenant": tenant, "purpose": purpose,
        "iat": int(clock[0]), "exp": int(clock[0]) + expires_in, "jti": jti,
    }
    payload = base64.urlsafe_b64encode(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(key.sign(payload.encode())).rstrip(b"=").decode()
    return f"{payload}.{signature}"


def test_owner_totp_opens_one_session_with_optional_profile(tmp_path: Path, clock: list[float]) -> None:
    active, calls = False, []
    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active
        payload = json.loads(request.content) if request.content else None; calls.append((request.method, request.url.path, payload))
        if request.method == "GET": return httpx.Response(200, json=[{"id": "owner-1", "status": "active"}] if active else [])
        if request.method == "POST" and request.url.path == "/sessions": active = True; return httpx.Response(200, json={"id": "owner-1"})
        return httpx.Response(200, json={})
    with TestClient(app_at(tmp_path, upstream)) as client:
        secret = enroll(client, clock)
        assert client.get("/owner/visual-access", headers=auth(OWNER)).status_code == 403
        assert client.post("/owner/sessions", headers=auth(OWNER), json={"start_url": "https://example.com", "auth_profile": "shop-one", "totp_code": fresh(secret, clock)}).status_code == 200
        assert client.get("/owner/visual-access", headers=auth(OWNER)).json() == {"session_id": "owner-1"}
        assert client.get("/owner/visual-access", headers=auth(AGENT)).status_code == 403
        assert ("POST", "/sessions", {"name": "owner-login", "start_url": "https://example.com", "auth_profile": "shop-one"}) in calls
        assert client.post("/owner/sessions", headers=auth(OWNER), json={"start_url": "https://example.com", "totp_code": fresh(secret, clock)}).status_code == 409


def test_agent_uses_only_open_owner_session_no_totp_or_creation(tmp_path: Path, clock: list[float]) -> None:
    active, calls = False, []
    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active
        payload = json.loads(request.content) if request.content else None; calls.append((request.method, request.url.path, payload))
        if request.method == "GET": return httpx.Response(200, json=[{"id": "owner-1", "status": "active"}] if active else [])
        if request.method == "POST" and request.url.path == "/sessions": active = True; return httpx.Response(200, json={"id": "owner-1"})
        return httpx.Response(200, json={"ok": True})
    with TestClient(app_at(tmp_path, upstream, agent_tokens=f"other:{OTHER}")) as client:
        secret = enroll(client, clock)
        assert client.post("/requests", headers=auth(AGENT), json={"purpose": "orders"}).status_code == 403
        client.post("/owner/sessions", headers=auth(OWNER), json={"start_url": "https://example.com", "totp_code": fresh(secret, clock)})
        request_id = client.post("/requests", headers=auth(AGENT), json={"purpose": "orders"}).json()["id"]
        assert client.post(f"/requests/{request_id}/sessions", headers=auth(AGENT)).status_code == 404
        assert client.post("/mcp/tools/call", headers=auth(AGENT), json={"name": "browser.create_session", "arguments": {"request_id": request_id}}).status_code == 404
        assert client.post(f"/requests/{request_id}/actions/click", headers=auth(OTHER), json={"arguments": {}}).status_code == 404
        assert client.post(f"/requests/{request_id}/actions/click", headers=auth(AGENT), json={"arguments": {"x": 1}}).status_code == 200
        assert ("POST", "/sessions/owner-1/actions/click", {"x": 1}) in calls
        assert client.post(f"/requests/{request_id}/complete", headers=auth(AGENT)).status_code == 200
        assert not any(method == "DELETE" for method, _, _ in calls)


def test_closure_and_restart_cannot_be_adopted_or_resurrected(tmp_path: Path, clock: list[float]) -> None:
    active = False
    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active
        if request.method == "GET": return httpx.Response(200, json=[{"id": "owner-1", "status": "active"}] if active else [])
        if request.method == "POST" and request.url.path == "/sessions": active = True; return httpx.Response(200, json={"id": "owner-1"})
        if request.method == "DELETE": active = False
        return httpx.Response(200, json={"ok": True})
    app = app_at(tmp_path, upstream)
    with TestClient(app) as client:
        secret = enroll(client, clock)
        client.post("/owner/sessions", headers=auth(OWNER), json={"start_url": "https://example.com", "totp_code": fresh(secret, clock)})
        request_id = client.post("/requests", headers=auth(AGENT), json={"purpose": "orders"}).json()["id"]
        assert client.delete("/owner/sessions/owner-1", headers=auth(OWNER)).status_code == 200
        assert client.get("/owner/visual-access", headers=auth(OWNER)).status_code == 403
        assert client.get(f"/requests/{request_id}/observe", headers=auth(AGENT)).status_code == 403
        used_code = totp_code(secret, int(clock[0] // 30))
        assert client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "totp_code": used_code,
        }).status_code == 403
        # Reopen it, then simulate a broker process restart while the controller
        # session remains live.
        assert client.post("/owner/sessions", headers=auth(OWNER), json={"start_url": "https://example.com", "totp_code": fresh(secret, clock)}).status_code == 200
    with TestClient(app_at(tmp_path, upstream)) as restarted:
        # An active controller session is rejected: this new broker has no trusted binding.
        assert restarted.post("/requests", headers=auth(AGENT), json={"purpose": "orders"}).status_code == 403


def test_agent_reuses_one_approved_grant_only_for_its_current_session(tmp_path: Path, clock: list[float]) -> None:
    active = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "reused-id", "status": "active"}] if active else [])
        if request.method == "POST" and request.url.path == "/sessions":
            active = True
            return httpx.Response(200, json={"id": "reused-id"})
        if request.method == "DELETE":
            active = False
        return httpx.Response(200, json={})

    with TestClient(app_at(tmp_path, upstream)) as client:
        secret = enroll(client, clock)
        client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "totp_code": fresh(secret, clock),
        })
        first = client.post("/mcp/tools/call", headers=auth(AGENT), json={
            "name": "browser.request_access", "arguments": {"purpose": "first task"},
        }).json()
        retry = client.post("/mcp/tools/call", headers=auth(AGENT), json={
            "name": "browser.request_access", "arguments": {"purpose": "another task"},
        }).json()
        assert retry["id"] == first["id"]
        assert client.post(f"/requests/{first['id']}/complete", headers=auth(AGENT)).json()["status"] == "completed"
        replacement = client.post("/requests", headers=auth(AGENT), json={"purpose": "new task"}).json()
        assert replacement["id"] != first["id"]
        assert client.delete("/owner/sessions/reused-id", headers=auth(OWNER)).status_code == 200
        client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "totp_code": fresh(secret, clock),
        })
        next_session = client.post("/requests", headers=auth(AGENT), json={"purpose": "next session"}).json()
        assert next_session["id"] not in {first["id"], replacement["id"]}


def test_agent_session_status_is_safe_and_portal_is_opt_in(tmp_path: Path, clock: list[float]) -> None:
    active = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "owner-1", "status": "active"}] if active else [])
        if request.method == "POST" and request.url.path == "/sessions":
            active = True
            return httpx.Response(200, json={"id": "owner-1"})
        return httpx.Response(200, json={})

    with TestClient(app_at(tmp_path, upstream, portal_url="https://portal.example/")) as client:
        absent = client.post("/mcp/tools/call", headers=auth(AGENT), json={
            "name": "browser.session_status", "arguments": {},
        }).json()
        assert absent == {"status": "absent", "portal_url": "https://portal.example"}
        secret = enroll(client, clock)
        client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "totp_code": fresh(secret, clock),
        })
        ready = client.post("/mcp/tools/call", headers=auth(AGENT), json={
            "name": "browser.session_status", "arguments": {},
        }).json()
        assert ready == {"status": "ready", "portal_url": "https://portal.example"}
        tools = {entry["name"] for entry in client.get("/mcp/tools", headers=auth(AGENT)).json()}
        assert "browser.session_status" in tools


def test_portal_assertion_is_signed_scoped_fresh_and_single_use(
    tmp_path: Path, clock: list[float]
) -> None:
    active = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "owner-1", "status": "active"}] if active else [])
        if request.method == "POST" and request.url.path == "/sessions":
            active = True
            return httpx.Response(200, json={"id": "owner-1"})
        if request.method == "DELETE":
            active = False
        return httpx.Response(200, json={})

    key = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).rstrip(b"=").decode()
    app = app_at(
        tmp_path, upstream, portal_assertion_public_key=public,
        expected_user_id="user-a", expected_tenant_id="tenant-a",
    )
    with TestClient(app) as client:
        valid = signed_assertion(key, clock)
        opened = client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "portal_assertion": valid,
        })
        assert opened.status_code == 200
        assert client.delete("/owner/sessions/owner-1", headers=auth(OWNER)).status_code == 200
        assert client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "portal_assertion": valid,
        }).status_code == 403
        for assertion in (
            signed_assertion(key, clock, expires_in=-1, jti="expired-assertion-1"),
            signed_assertion(key, clock, user="user-b", jti="wrong-user-assertion"),
            signed_assertion(key, clock, tenant="tenant-b", jti="wrong-tenant-assertion"),
            signed_assertion(key, clock, purpose="other", jti="wrong-purpose-assertion"),
            "unsigned-payload-that-is-long-enough.invalid-signature-that-is-long-enough",
        ):
            response = client.post("/owner/sessions", headers=auth(OWNER), json={
                "start_url": "https://example.com", "portal_assertion": assertion,
            })
            assert response.status_code == 403
        assert client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "totp_code": "123456",
        }).status_code == 403


def test_owner_deny_or_revoke_blocks_agent_until_next_owner_session(tmp_path: Path, clock: list[float]) -> None:
    active = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "owner-1", "status": "active"}] if active else [])
        if request.method == "POST" and request.url.path == "/sessions":
            active = True
            return httpx.Response(200, json={"id": "owner-1"})
        if request.method == "DELETE":
            active = False
        return httpx.Response(200, json={})

    with TestClient(app_at(tmp_path, upstream)) as client:
        secret = enroll(client, clock)
        client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "totp_code": fresh(secret, clock),
        })
        denied = client.post("/requests", headers=auth(AGENT), json={"purpose": "deny me"}).json()["id"]
        assert client.post(f"/requests/{denied}/deny", headers=auth(OWNER)).status_code == 200
        assert client.post("/requests", headers=auth(AGENT), json={"purpose": "retry"}).status_code == 403
        assert client.delete("/owner/sessions/owner-1", headers=auth(OWNER)).status_code == 200
        client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "totp_code": fresh(secret, clock),
        })
        revoked = client.post("/requests", headers=auth(AGENT), json={"purpose": "revoke me"}).json()["id"]
        assert client.post(f"/requests/{revoked}/revoke", headers=auth(OWNER)).status_code == 200
        assert client.post("/requests", headers=auth(AGENT), json={"purpose": "retry again"}).status_code == 403


@pytest.mark.parametrize("portal", [
    "http://portal.example", "https://user@portal.example", "https://portal.example/path",
    "https://portal.example/?token=x", "https://portal.example/#fragment",
])
def test_portal_url_accepts_only_a_public_https_origin(tmp_path: Path, portal: str) -> None:
    def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with pytest.raises(ValueError, match="public HTTPS origin"):
        app_at(tmp_path, upstream, portal_url=portal)


def test_owner_revoke_waits_for_an_inflight_action_then_blocks_later_actions(tmp_path: Path, clock: list[float]) -> None:
    active = False
    action_started, allow_action_finish = threading.Event(), threading.Event()

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "owner-1", "status": "active"}] if active else [])
        if request.method == "POST" and request.url.path == "/sessions":
            active = True
            return httpx.Response(200, json={"id": "owner-1"})
        if request.url.path.endswith("/actions/click"):
            action_started.set()
            assert allow_action_finish.wait(2)
        return httpx.Response(200, json={"ok": True})

    with TestClient(app_at(tmp_path, upstream)) as client:
        secret = enroll(client, clock)
        client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "totp_code": fresh(secret, clock),
        })
        grant = client.post("/requests", headers=auth(AGENT), json={"purpose": "orders"}).json()["id"]
        action_response, revoke_response = [], []
        action_thread = threading.Thread(target=lambda: action_response.append(client.post(
            f"/requests/{grant}/actions/click", headers=auth(AGENT), json={"arguments": {}},
        )))
        action_thread.start()
        assert action_started.wait(1)
        revoke_thread = threading.Thread(target=lambda: revoke_response.append(client.post(
            f"/requests/{grant}/revoke", headers=auth(OWNER),
        )))
        revoke_thread.start()
        assert not revoke_response
        allow_action_finish.set()
        action_thread.join(2); revoke_thread.join(2)
        assert action_response[0].status_code == 200
        assert revoke_response[0].status_code == 200
        assert client.post(f"/requests/{grant}/actions/click", headers=auth(AGENT), json={"arguments": {}}).status_code == 403


def test_revoking_old_grant_also_stops_newer_active_grant(tmp_path: Path, clock: list[float]) -> None:
    active = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "owner-1", "status": "active"}] if active else [])
        if request.method == "POST" and request.url.path == "/sessions":
            active = True
            return httpx.Response(200, json={"id": "owner-1"})
        return httpx.Response(200, json={})

    with TestClient(app_at(tmp_path, upstream)) as client:
        secret = enroll(client, clock)
        assert client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "totp_code": fresh(secret, clock),
        }).status_code == 200
        old = client.post("/requests", headers=auth(AGENT), json={"purpose": "old"}).json()["id"]
        assert client.post(f"/requests/{old}/complete", headers=auth(AGENT)).status_code == 200
        current = client.post("/requests", headers=auth(AGENT), json={"purpose": "current"}).json()["id"]
        assert current != old
        assert client.post(f"/requests/{old}/revoke", headers=auth(OWNER)).status_code == 200
        assert client.get(f"/requests/{current}/observe", headers=auth(AGENT)).status_code == 403
        assert client.post("/requests", headers=auth(AGENT), json={"purpose": "retry"}).status_code == 403


def test_owner_can_delete_and_rename_auth_profiles(tmp_path: Path, clock: list[float]) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, payload))
        if request.method == "POST" and request.url.path == "/auth-profiles/shop-one/rename":
            return httpx.Response(200, json={"profile_name": "shop-two", "previous_name": "shop-one"})
        if request.method == "DELETE" and request.url.path == "/auth-profiles/shop-two":
            return httpx.Response(200, json={"profile_name": "shop-two", "deleted": True})
        return httpx.Response(404)

    with TestClient(app_at(tmp_path, upstream)) as client:
        renamed = client.post(
            "/owner/auth-profiles/shop-one/rename", headers=auth(OWNER), json={"new_name": "shop-two"},
        )
        assert renamed.status_code == 200
        assert renamed.json() == {"profile_name": "shop-two", "previous_name": "shop-one"}
        deleted = client.delete("/owner/auth-profiles/shop-two", headers=auth(OWNER))
        assert deleted.status_code == 200
        assert deleted.json() == {"profile_name": "shop-two", "deleted": True}
        assert ("POST", "/auth-profiles/shop-one/rename", {"new_name": "shop-two"}) in calls
        assert ("DELETE", "/auth-profiles/shop-two", None) in calls


def test_agent_cannot_manage_auth_profiles(tmp_path: Path, clock: list[float]) -> None:
    with TestClient(app_at(tmp_path, lambda request: httpx.Response(200, json={}))) as client:
        assert client.delete("/owner/auth-profiles/shop-one", headers=auth(AGENT)).status_code == 403
        assert client.post(
            "/owner/auth-profiles/shop-one/rename", headers=auth(AGENT), json={"new_name": "x"},
        ).status_code == 403


def test_malformed_profile_names_are_rejected_before_reaching_the_controller(
    tmp_path: Path, clock: list[float],
) -> None:
    calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    with TestClient(app_at(tmp_path, upstream)) as client:
        assert client.delete("/owner/auth-profiles/bad*name", headers=auth(OWNER)).status_code == 400
        assert client.post(
            "/owner/auth-profiles/shop-one/rename", headers=auth(OWNER), json={"new_name": "bad*name"},
        ).status_code == 422
        assert not calls


def test_owner_vnc_static_requires_an_open_session_and_proxies_files(tmp_path: Path, clock: list[float]) -> None:
    active = False
    novnc_calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "owner-1", "status": "active"}] if active else [])
        if request.method == "POST" and request.url.path == "/sessions":
            active = True
            return httpx.Response(200, json={"id": "owner-1"})
        return httpx.Response(200, json={})

    def novnc(request: httpx.Request) -> httpx.Response:
        novnc_calls.append(request.url.path)
        return httpx.Response(200, text="<html>vnc</html>", headers={"content-type": "text/html"})

    app = app_at(tmp_path, upstream, novnc_transport=httpx.MockTransport(novnc))
    with TestClient(app) as client:
        secret = enroll(client, clock)
        denied = client.get("/owner/vnc/vnc.html", headers=auth(OWNER))
        assert denied.status_code == 403
        assert not novnc_calls
        assert client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "totp_code": fresh(secret, clock),
        }).status_code == 200
        served = client.get("/owner/vnc/vnc.html", headers=auth(OWNER))
        assert served.status_code == 200
        assert "vnc" in served.text
        assert novnc_calls == ["/vnc.html"]
        assert client.get("/owner/vnc/../../etc/passwd", headers=auth(OWNER)).status_code == 404
        assert client.get("/owner/vnc/vnc.html", headers=auth(AGENT)).status_code == 403


def test_owner_vnc_websocket_requires_bearer_and_open_session_and_rechecks_frames(
    tmp_path: Path, clock: list[float], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import approval_broker.app as broker_module

    active = False
    sent: list[bytes] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "owner-1", "status": "active"}] if active else [])
        if request.method == "POST" and request.url.path == "/sessions":
            active = True
            return httpx.Response(200, json={"id": "owner-1"})
        return httpx.Response(200, json={})

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

    monkeypatch.setattr(broker_module, "ws_connect", fake_connect)
    app = app_at(tmp_path, upstream)
    with TestClient(app) as client:
        secret = enroll(client, clock)
        # No bearer at all, and an agent bearer, must both be refused.
        with pytest.raises(Exception):
            with client.websocket_connect("/owner/vnc/websockify"):
                pass
        with pytest.raises(Exception):
            with client.websocket_connect("/owner/vnc/websockify", headers=auth(AGENT)):
                pass
        # No open session yet.
        with pytest.raises(Exception):
            with client.websocket_connect("/owner/vnc/websockify", headers=auth(OWNER)):
                pass
        client.post("/owner/sessions", headers=auth(OWNER), json={
            "start_url": "https://example.com", "totp_code": fresh(secret, clock),
        })
        with client.websocket_connect("/owner/vnc/websockify", headers=auth(OWNER)) as socket:
            socket.send_bytes(b"first-frame")
            active = False
            socket.send_bytes(b"second-frame")
            with pytest.raises(Exception):
                socket.receive_bytes()
        assert b"second-frame" not in sent

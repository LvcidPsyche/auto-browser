"""Gateway policy tests; no real Cloudflare or broker calls."""

import base64
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import owner_gateway.app as gateway_module
from owner_gateway.app import AccessValidator, create_app


@pytest.fixture
def gateway():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()

    def b64(number):
        return base64.urlsafe_b64encode(number.to_bytes((number.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

    jwk = {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": "test-key", "n": b64(numbers.n), "e": b64(numbers.e)}
    calls = []
    state = {"session_id": None, "guard_status": 200}

    def respond(request):
        if request.url.path == "/cdn-cgi/access/certs":
            return httpx.Response(200, json={"keys": [jwk]})
        calls.append(request)
        if request.url.path == "/owner/visual-access":
            assert request.headers["authorization"] == "Bearer broker-secret"
            if state["guard_status"] != 200 or state["session_id"] is None:
                return httpx.Response(state["guard_status"] if state["guard_status"] != 200 else 403)
            return httpx.Response(200, json={"session_id": state["session_id"]})
        if request.url.path == "/owner.js":
            source = Path(__file__).resolve().parents[1] / "approval_broker" / "owner.js"
            return httpx.Response(200, content=source.read_bytes(), headers={"content-type": "text/javascript"})
        return httpx.Response(200, json={"ok": True})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    validator = AccessValidator("example.cloudflareaccess.com", "audience-1", "owner@example.com", upstream)
    app = create_app(validator=validator, client=upstream, owner_token="broker-secret",
                     public_origin="https://secure-browser.fareeqk.com")

    def token(**overrides):
        claims = {"iss": "https://example.cloudflareaccess.com", "aud": ["audience-1"],
                  "email": "owner@example.com", "iat": 1_780_000_000, "exp": 1_900_000_000}
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})

    with TestClient(app) as client:
        yield client, token, calls, state


def test_missing_forged_wrong_audience_and_email(gateway):
    client, token, calls, _ = gateway
    assert client.get("/owner").status_code == 401
    assert client.get("/owner", headers={"Cf-Access-Jwt-Assertion": "forged.jwt.token"}).status_code == 401
    assert client.get("/owner", headers={"Cf-Access-Jwt-Assertion": token(aud=["other"])}).status_code == 401
    assert client.get("/owner", headers={"Cf-Access-Jwt-Assertion": token(email="other@example.com")}).status_code == 401
    assert calls == []


def test_owner_allowlist_and_server_side_bearer(gateway):
    client, token, calls, _ = gateway
    headers = {"Cf-Access-Jwt-Assertion": token(), "Authorization": "Bearer attacker"}
    assert client.get("/owner", headers=headers).status_code == 200
    assert calls[-1].headers["authorization"] == "Bearer broker-secret"
    assert "cf-access-jwt-assertion" not in calls[-1].headers
    assert "cookie" not in calls[-1].headers
    assert client.get("/requests", headers=headers).status_code == 200
    assert client.post("/owner/sessions", headers={**headers, "Origin": "https://secure-browser.fareeqk.com"}, json={}).status_code == 200
    assert client.post("/owner/sessions", headers=headers, json={}).status_code == 403


def test_agent_and_controller_routes_never_forward(gateway):
    client, token, calls, _ = gateway
    headers = {"Cf-Access-Jwt-Assertion": token()}
    for path in ("/mcp", "/mcp/tools", "/requests/grant/observe", "/requests/grant/complete", "/sessions", "/docs"):
        assert client.get(path, headers=headers).status_code == 404
    assert calls == []


def test_visual_route_requires_totp_opened_session(gateway):
    client, token, calls, state = gateway
    headers = {"Cf-Access-Jwt-Assertion": token()}
    assert client.get("/vnc/vnc.html", headers=headers).status_code == 403
    assert all(call.url.path != "/vnc.html" for call in calls)
    state["session_id"] = "session-one"
    assert client.get("/vnc/vnc.html?path=vnc/websockify", headers=headers).status_code == 200
    assert calls[-1].url.path == "/vnc.html"
    state["session_id"] = None
    assert client.get("/vnc/vnc.html", headers=headers).status_code == 403
    state["session_id"] = "session-two"
    assert client.get("/vnc/vnc.html", headers=headers).status_code == 200
    state["guard_status"] = 502
    assert client.get("/vnc/vnc.html", headers=headers).status_code == 403
    assert client.get("/vnc/%2e%2e/mcp", headers=headers).status_code == 404


def test_visual_websocket_denies_missing_or_agent_jwt_and_closed_session(gateway):
    client, token, calls, state = gateway
    headers = {"Cf-Access-Jwt-Assertion": token(), "Origin": "https://secure-browser.fareeqk.com"}
    with pytest.raises(Exception):
        with client.websocket_connect("/vnc/websockify"):
            pass
    with pytest.raises(Exception):
        with client.websocket_connect("/vnc/websockify", headers={**headers, "Origin": "https://evil.example"}):
            pass
    with pytest.raises(Exception):
        with client.websocket_connect("/vnc/websockify", headers={**headers, "Cf-Access-Jwt-Assertion": token(email="agent@example.com")}):
            pass
    with pytest.raises(Exception):
        with client.websocket_connect("/vnc/websockify", headers=headers):
            pass
    assert all(call.url.path != "/websockify" for call in calls)


@pytest.mark.parametrize("next_session", [None, "session-two"])
def test_websocket_rechecks_session_before_each_control_frame(gateway, monkeypatch, next_session):
    client, token, _, state = gateway
    state["session_id"] = "session-one"
    sent = []

    class FakeUpstream:
        async def send(self, frame):
            sent.append(frame)

        async def __aiter__(self):
            import asyncio
            await asyncio.sleep(30)
            if False:
                yield b""

    @asynccontextmanager
    async def fake_connect(*args, **kwargs):
        yield FakeUpstream()

    monkeypatch.setattr(gateway_module, "ws_connect", fake_connect)
    headers = {"Cf-Access-Jwt-Assertion": token(), "Origin": "https://secure-browser.fareeqk.com"}
    with client.websocket_connect("/vnc/websockify", headers=headers) as socket:
        socket.send_bytes(b"first-frame")
        state["session_id"] = next_session
        socket.send_bytes(b"second-frame")
        with pytest.raises(Exception):
            socket.receive_bytes()
    assert b"second-frame" not in sent


def test_owner_script_has_no_credential_prompt_or_local_vnc(gateway):
    client, token, _, _ = gateway
    response = client.get("/owner.js", headers={"Cf-Access-Jwt-Assertion": token()})
    assert response.status_code == 200
    assert "127.0.0.1" not in response.text
    assert "window.prompt(\"رمز المالك\")" not in response.text
    assert "Authorization: `Bearer ${ownerToken}`" not in response.text
    assert "/vnc/vnc.html" in response.text

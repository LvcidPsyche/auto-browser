from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_gateway.app import create_app

INTERNAL = "i" * 48
BROKER = "b" * 48
ISSUER = "https://mcp-browser.example"
RESOURCE = f"{ISSUER}/mcp"
PORTAL = "https://secure-browser.example"
REDIRECT = "https://client.example/oauth/callback"


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def pkce(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
def clock() -> list[float]:
    return [1_800_000_000.0]


@pytest.fixture
def broker_calls() -> list[dict]:
    return []


def app_at(tmp_path: Path, clock: list[float], broker_calls: list[dict]):
    private = tmp_path / "private"
    private.mkdir(mode=0o700, exist_ok=True)

    def broker(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {BROKER}"
        body = json.loads(request.content)
        broker_calls.append(body)
        name, args = body["params"]["name"], body["params"]["arguments"]
        if name == "browser.session_status":
            value = {"status": "ready", "session_id": "raw-browser-session"}
        elif name == "browser.request_access":
            value = {"id": f"raw-{args['purpose']}", "session_id": "raw-browser-session", "status": "approved"}
        else:
            value = {"id": args["request_id"], "session_id": "raw-browser-session", "ok": True}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(value)}]
        }})

    return create_app(
        issuer_url=ISSUER, resource_url=RESOURCE, portal_url=PORTAL,
        internal_token=INTERNAL, broker_token=BROKER,
        database_path=private / "oauth.sqlite3", broker_transport=httpx.MockTransport(broker),
        clock=lambda: clock[0],
    )


def register(client: TestClient, *, name: str = "Assistant") -> str:
    response = client.post("/register", json={
        "client_name": name, "application_type": "web", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def authorize(client: TestClient, client_id: str, *, state: str = "opaque-state", verifier: str | None = None):
    verifier = verifier or ("v" * 43)
    response = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": pkce(verifier), "code_challenge_method": "S256",
        "state": state, "scope": "browser", "resource": RESOURCE,
    }, follow_redirects=False)
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query["authorization_request"][0], verifier


def consent(client: TestClient, authorization_request: str, *, user: str = "user-a", tenant: str = "tenant-a") -> str:
    response = client.post("/internal/consent", headers=bearer(INTERNAL), json={
        "authorization_request": authorization_request, "user_id": user,
        "tenant_id": tenant, "approve": True,
    })
    assert response.status_code == 200, response.text
    redirect = response.json()["redirect_url"]
    parsed = parse_qs(urlsplit(redirect).query)
    assert parsed["state"] == ["opaque-state"]
    assert parsed["iss"] == [ISSUER]
    return parsed["code"][0]


def exchange(client: TestClient, client_id: str, code: str, verifier: str):
    return client.post("/token", data={
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "redirect_uri": REDIRECT, "code_verifier": verifier, "resource": RESOURCE,
    })


def connection(client: TestClient, *, user: str = "user-a", tenant: str = "tenant-a") -> tuple[str, str, str]:
    client_id = register(client)
    request_id, verifier = authorize(client, client_id)
    code = consent(client, request_id, user=user, tenant=tenant)
    response = exchange(client, client_id, code, verifier)
    assert response.status_code == 200, response.text
    return client_id, response.json()["access_token"], response.json()["refresh_token"]


def mcp(client: TestClient, token: str, name: str, arguments: dict):
    return client.post("/mcp", headers=bearer(token), json={
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })


def activate(client: TestClient, user: str, tenant: str, active: bool = True):
    return client.post("/internal/active-user", headers=bearer(INTERNAL), json={
        "user_id": user, "tenant_id": tenant, "active": active,
    })


def test_metadata_and_unauthorized_mcp_challenge(tmp_path: Path, clock, broker_calls) -> None:
    with TestClient(app_at(tmp_path, clock, broker_calls)) as client:
        auth = client.get("/.well-known/oauth-authorization-server").json()
        assert auth["code_challenge_methods_supported"] == ["S256"]
        assert auth["authorization_response_iss_parameter_supported"] is True
        assert client.get("/.well-known/oauth-protected-resource").json()["resource"] == RESOURCE
        denied = client.post("/mcp", json={})
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == (
            f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"'
        )


def test_dcr_public_clients_and_redirect_validation(tmp_path: Path, clock, broker_calls) -> None:
    with TestClient(app_at(tmp_path, clock, broker_calls)) as client:
        identifier = register(client, name="  Nice\n <Client>  ")
        assert identifier
        native = client.post("/register", json={
            "client_name": "Native", "application_type": "native",
            "redirect_uris": ["http://127.0.0.1:32123/callback"],
        })
        assert native.status_code == 201
        assert client.post("/register", json={
            "client_name": "Bad", "application_type": "web",
            "redirect_uris": ["http://client.example/callback"],
        }).status_code == 400
        assert client.post("/register", json={
            "client_name": "Secret", "application_type": "web", "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "client_secret_basic",
        }).status_code == 400


def test_full_dcr_consent_pkce_and_mcp_flow(tmp_path: Path, clock, broker_calls) -> None:
    app = app_at(tmp_path, clock, broker_calls)
    with TestClient(app) as client:
        client_id = register(client)
        authorization_request, verifier = authorize(client, client_id)
        preview = client.post("/internal/consent/preview", headers=bearer(INTERNAL), json={
            "authorization_request": authorization_request,
            "user_id": "user-a", "tenant_id": "tenant-a",
        })
        assert preview.json() == {
            "client_id": client_id, "client_name": "Assistant",
            "capabilities": ["Browser status", "Ordinary browser navigation and interaction"],
        }
        code = consent(client, authorization_request)
        assert client.post("/internal/consent/preview", headers=bearer(INTERNAL), json={
            "authorization_request": authorization_request,
        }).status_code == 400
        exchanged = exchange(client, client_id, code, verifier)
        assert exchanged.status_code == 200
        access = exchanged.json()["access_token"]
        assert activate(client, "user-a", "tenant-a").status_code == 200
        init = client.post("/mcp", headers=bearer(access), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        assert init.status_code == 200
        listed = client.post("/mcp", headers=bearer(access), json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }).json()["result"]["tools"]
        assert {tool["name"] for tool in listed} >= {"browser.session_status", "browser.request_access", "browser.click"}
        status = json.loads(mcp(client, access, "browser.session_status", {}).json()["result"]["content"][0]["text"])
        assert status["state"] == "ready"
        assert status["portal_url"].startswith(f"{PORTAL}/browser?")
        requested = json.loads(mcp(client, access, "browser.request_access", {"purpose": "orders"}).json()["result"]["content"][0]["text"])
        assert requested["request_id"] and "raw-orders" not in json.dumps(requested)
        clicked = mcp(client, access, "browser.click", {"request_id": requested["request_id"], "arguments": {"x": 1}})
        assert clicked.status_code == 200
        assert broker_calls[-1]["params"]["arguments"]["request_id"] == "raw-orders"
        assert client_id


@pytest.mark.parametrize("verifier", [None, "w" * 43])
def test_missing_or_wrong_pkce_verifier_is_rejected(tmp_path: Path, clock, broker_calls, verifier) -> None:
    with TestClient(app_at(tmp_path, clock, broker_calls)) as client:
        client_id = register(client)
        request_id, correct = authorize(client, client_id)
        code = consent(client, request_id)
        data = {"grant_type": "authorization_code", "client_id": client_id, "code": code,
                "redirect_uri": REDIRECT, "resource": RESOURCE}
        if verifier is not None:
            data["code_verifier"] = verifier
        response = client.post("/token", data=data)
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        assert correct != verifier


def test_code_replay_and_wrong_redirect_are_rejected(tmp_path: Path, clock, broker_calls) -> None:
    with TestClient(app_at(tmp_path, clock, broker_calls)) as client:
        client_id = register(client)
        request_id, verifier = authorize(client, client_id)
        code = consent(client, request_id)
        wrong = client.post("/token", data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": "https://client.example/wrong", "code_verifier": verifier, "resource": RESOURCE,
        })
        assert wrong.status_code == 400
        assert exchange(client, client_id, code, verifier).status_code == 200
        assert exchange(client, client_id, code, verifier).json()["error"] == "invalid_grant"


def test_rfc8707_resource_is_required_and_exact(tmp_path: Path, clock, broker_calls) -> None:
    with TestClient(app_at(tmp_path, clock, broker_calls)) as client:
        client_id = register(client)
        denied = client.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
            "code_challenge": pkce("v" * 43), "code_challenge_method": "S256",
            "scope": "browser", "resource": RESOURCE + "/",
        }, follow_redirects=False)
        assert parse_qs(urlsplit(denied.headers["location"]).query)["error"] == ["invalid_request"]
        request_id, verifier = authorize(client, client_id)
        code = consent(client, request_id)
        response = client.post("/token", data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier, "resource": RESOURCE + "/",
        })
        assert response.json()["error"] == "invalid_target"


def test_refresh_rotation_and_replay_revokes_family(tmp_path: Path, clock, broker_calls) -> None:
    with TestClient(app_at(tmp_path, clock, broker_calls)) as client:
        client_id, access, refresh = connection(client)
        assert activate(client, "user-a", "tenant-a").status_code == 200
        rotated = client.post("/token", data={
            "grant_type": "refresh_token", "client_id": client_id,
            "refresh_token": refresh, "resource": RESOURCE,
        })
        assert rotated.status_code == 200
        new_access = rotated.json()["access_token"]
        replay = client.post("/token", data={
            "grant_type": "refresh_token", "client_id": client_id,
            "refresh_token": refresh, "resource": RESOURCE,
        })
        assert replay.json()["error"] == "invalid_grant"
        assert client.post("/mcp", headers=bearer(access), json={}).status_code == 401
        assert client.post("/mcp", headers=bearer(new_access), json={}).status_code == 401


def test_two_users_can_be_active_but_cannot_use_each_others_mapping(tmp_path: Path, clock, broker_calls) -> None:
    with TestClient(app_at(tmp_path, clock, broker_calls)) as client:
        _, access_a, _ = connection(client, user="user-a", tenant="tenant")
        _, access_b, _ = connection(client, user="user-b", tenant="tenant")
        assert activate(client, "user-b", "tenant").status_code == 200
        request_b = json.loads(mcp(client, access_b, "browser.request_access", {"purpose": "b-task"}).json()["result"]["content"][0]["text"])["request_id"]
        assert activate(client, "user-a", "tenant").status_code == 200
        assert mcp(client, access_a, "browser.request_access", {"purpose": "a-task"}).status_code == 200
        status_a = json.loads(mcp(client, access_a, "browser.session_status", {}).json()["result"]["content"][0]["text"])
        assert status_a["state"] == "ready"
        assert activate(client, "user-b", "tenant", False).status_code == 200
        guessed = mcp(client, access_a, "browser.click", {"request_id": request_b, "arguments": {}})
        assert guessed.status_code == 404
        forbidden = mcp(client, access_a, "browser.request_access", {"purpose": "x", "user_id": "user-b"})
        assert forbidden.status_code == 400


def test_access_token_identity_selects_distinct_broker_and_credential(
    tmp_path: Path, clock
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    calls: list[tuple[str, str, str]] = []

    def make_client(label: str) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append((label, request.headers["authorization"], body["params"]["name"]))
            value = {"status": "ready"} if body["params"]["name"] == "browser.session_status" else {
                "id": f"raw-{label}", "status": "approved"
            }
            return httpx.Response(200, json={"result": {
                "content": [{"type": "text", "text": json.dumps(value)}]
            }})

        return httpx.AsyncClient(
            base_url=f"http://broker-{label}:18001", transport=httpx.MockTransport(handler)
        )

    brokers = {"user-a": make_client("a"), "user-b": make_client("b")}

    def resolver(user_id: str, tenant_id: str):
        assert tenant_id == "tenant"
        return brokers[user_id], f"agent-{user_id}-" + "x" * 32

    app = create_app(
        issuer_url=ISSUER, resource_url=RESOURCE, portal_url=PORTAL,
        internal_token=INTERNAL, broker_token=BROKER,
        database_path=private / "oauth.sqlite3", broker_resolver=resolver,
        clock=lambda: clock[0],
    )
    with TestClient(app) as client:
        _, access_a, _ = connection(client, user="user-a", tenant="tenant")
        _, access_b, _ = connection(client, user="user-b", tenant="tenant")
        assert activate(client, "user-a", "tenant").status_code == 200
        assert activate(client, "user-b", "tenant").status_code == 200
        assert mcp(client, access_a, "browser.session_status", {}).status_code == 200
        assert mcp(client, access_b, "browser.session_status", {}).status_code == 200
        forged = mcp(client, access_a, "browser.request_access", {
            "purpose": "x", "tenant_id": "tenant", "user_id": "user-b",
        })
        assert forged.status_code == 400
    asyncio.run(brokers["user-a"].aclose())
    asyncio.run(brokers["user-b"].aclose())
    assert calls == [
        ("a", "Bearer agent-user-a-" + "x" * 32, "browser.session_status"),
        ("b", "Bearer agent-user-b-" + "x" * 32, "browser.session_status"),
    ]


def test_disconnect_scoped_to_user_invalidates_access_and_refresh(tmp_path: Path, clock, broker_calls) -> None:
    with TestClient(app_at(tmp_path, clock, broker_calls)) as client:
        client_id, access, refresh = connection(client)
        listed = client.get("/internal/connected-clients", headers=bearer(INTERNAL),
                            params={"user_id": "user-a", "tenant_id": "tenant-a"}).json()["connections"]
        ref = listed[0]["connection_ref"]
        assert client.post(f"/internal/connected-clients/{ref}/disconnect", headers=bearer(INTERNAL), json={
            "user_id": "other", "tenant_id": "tenant-a",
        }).status_code == 404
        assert client.post(f"/internal/connected-clients/{ref}/disconnect", headers=bearer(INTERNAL), json={
            "user_id": "user-a", "tenant_id": "tenant-a",
        }).status_code == 200
        assert client.post("/mcp", headers=bearer(access), json={}).status_code == 401
        refreshed = client.post("/token", data={
            "grant_type": "refresh_token", "client_id": client_id,
            "refresh_token": refresh, "resource": RESOURCE,
        })
        assert refreshed.json()["error"] == "invalid_grant"


def test_credentials_and_raw_ids_do_not_leak_or_rest_plaintext(tmp_path: Path, clock, broker_calls) -> None:
    app = app_at(tmp_path, clock, broker_calls)
    with TestClient(app) as client:
        client_id, access, refresh = connection(client)
        assert activate(client, "user-a", "tenant-a").status_code == 200
        status = mcp(client, access, "browser.session_status", {})
        request = mcp(client, access, "browser.request_access", {"purpose": "private"})
        public_text = status.text + request.text + json.dumps(broker_calls)
        assert BROKER not in public_text
        assert "raw-browser-session" not in status.text + request.text
        database_bytes = (tmp_path / "private" / "oauth.sqlite3").read_bytes()
        assert access.encode() not in database_bytes
        assert refresh.encode() not in database_bytes
        assert client_id.encode() in database_bytes  # client ids are public, not credentials

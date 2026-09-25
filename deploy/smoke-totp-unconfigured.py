"""Non-mutating production check before the owner enrolls a phone."""

import os

import httpx

base = "http://127.0.0.1:18001"
owner = {"Authorization": f"Bearer {os.environ['BROKER_OWNER_TOKEN']}"}
agent_token = os.environ["BROKER_AGENT_TOKENS"].split(",", 1)[0].split(":", 1)[1]
agent = {"Authorization": f"Bearer {agent_token}"}

with httpx.Client(base_url=base, timeout=10) as client:
    status = client.get("/owner/totp", headers=owner)
    assert status.status_code == 200 and status.json() == {"status": "unconfigured"}, status.status_code
    assert client.get("/owner/totp", headers=agent).status_code == 403
    assert client.get("/owner/visual-access", headers=owner).status_code == 403
    assert client.post("/requests", headers=agent, json={"purpose": "smoke check"}).status_code == 403
    tools = client.post("/mcp", headers=agent, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })
    assert tools.status_code == 200
    names = {item["name"] for item in tools.json()["result"]["tools"]}
    assert "browser.request_access" in names and "browser.create_session" not in names
    before = client.get("/owner/sessions", headers=owner)
    assert before.status_code == 200 and not any(s.get("status") == "active" for s in before.json())
    denied = client.post(
        "/owner/sessions",
        headers=owner,
        json={"start_url": "https://example.com", "totp_code": "000000"},
    )
    assert denied.status_code == 403, denied.status_code
    after = client.get("/owner/sessions", headers=owner)
    assert after.status_code == 200 and not any(s.get("status") == "active" for s in after.json())

print("PASS: unconfigured authenticator fails closed; agent cannot start sessions; no session created")

"""Live non-sensitive end-to-end check against the private Hetzner pilot."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:18001"
PROFILE = "pilot_example"


def credentials() -> tuple[str, str, str]:
    values = dict(line.split("=", 1) for line in Path(".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    agents = dict(part.split(":", 1) for part in values["BROKER_AGENT_TOKENS"].split(","))
    return values["BROKER_OWNER_TOKEN"], agents["assistant_one"], agents["assistant_two"]


def call(path: str, token: str | None = None, method: str = "GET", body: dict | None = None) -> tuple[int, object]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=40) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except HTTPError as error:
        return error.code, {}


def expect(status: int, expected: int, label: str) -> None:
    if status != expected:
        raise RuntimeError(f"{label}: expected HTTP {expected}, got {status}")
    print(f"PASS {label}: HTTP {status}")


def main() -> None:
    owner, agent_one, agent_two = credentials()
    expect(call("/requests")[0], 401, "unauthenticated access denied")
    expect(call("/owner/sessions", agent_one)[0], 403, "agent cannot use owner routes")
    created_session = None
    try:
        status, created = call("/owner/sessions", owner, "POST", {"start_url": "https://example.com"})
        expect(status, 200, "owner creates visible session")
        created_session = created["id"]
        status, _ = call(f"/owner/sessions/{created_session}/auth-profiles", owner, "POST", {"profile_name": PROFILE})
        expect(status, 200, "owner saves named profile")
        status, profiles = call("/owner/auth-profiles", owner)
        expect(status, 200, "owner lists profiles")
        if PROFILE not in json.dumps(profiles):
            raise RuntimeError("Saved profile not found")
        print("PASS saved profile is listed")
    finally:
        if created_session:
            status, _ = call(f"/owner/sessions/{created_session}", owner, "DELETE")
            expect(status, 200, "owner closes login session")

    status, grant = call("/requests", agent_one, "POST", {
        "profile": PROFILE,
        "start_url": "https://example.com",
        "purpose": "Non-sensitive integration smoke test",
    })
    expect(status, 202, "agent requests access")
    request_id = grant["id"]
    expect(call(f"/requests/{request_id}/sessions", agent_one, "POST")[0], 403, "unapproved start denied")
    expect(call(f"/requests/{request_id}", agent_two)[0], 404, "other agent cannot view request")
    status, approved = call(f"/requests/{request_id}/approve", owner, "POST", {})
    expect(status, 200, "owner approves request")
    if approved["expires_at"] is not None:
        raise RuntimeError("Task approval unexpectedly has a time cutoff")
    try:
        status, session = call(f"/requests/{request_id}/sessions", agent_one, "POST")
        expect(status, 200, "approved agent opens saved profile")
        if not session.get("id"):
            raise RuntimeError("Browser session id missing")
        expect(call(f"/requests/{request_id}/observe", agent_one)[0], 200, "approved agent observes page")
        expect(call(f"/requests/{request_id}/observe", agent_two)[0], 404, "other agent cannot observe")
    finally:
        status, _ = call(f"/requests/{request_id}/revoke", owner, "POST")
        expect(status, 200, "owner revokes access and closes session")
    expect(call(f"/requests/{request_id}/observe", agent_one)[0], 403, "revoked agent denied")

    status, second_grant = call("/requests", agent_one, "POST", {
        "profile": PROFILE,
        "start_url": "https://example.com",
        "purpose": "Completion cleanup smoke test",
    })
    expect(status, 202, "agent requests another task")
    second_id = second_grant["id"]
    expect(call(f"/requests/{second_id}/approve", owner, "POST", {})[0], 200,
           "owner approves task without time cutoff")
    expect(call(f"/requests/{second_id}/sessions", agent_one, "POST")[0], 200,
           "agent opens approved task session")
    status, completed = call(f"/requests/{second_id}/complete", agent_one, "POST")
    expect(status, 200, "agent completes task and closes session")
    if completed["status"] != "completed" or completed["session_id"] is not None:
        raise RuntimeError("Completed task left browser session active")
    expect(call(f"/requests/{second_id}/observe", agent_one)[0], 403,
           "completed agent denied further access")


if __name__ == "__main__":
    main()

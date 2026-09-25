"""Measure one non-sensitive live browser session without touching saved logins."""

from __future__ import annotations

import getpass
import json
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:18001"


def request(path: str, token: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urlopen(Request(BASE + path, data=data, headers=headers, method=method), timeout=30) as response:
        return json.load(response)


def main() -> None:
    values = dict(line.split("=", 1) for line in Path(".env").read_text().splitlines() if "=" in line)
    token = values["BROKER_OWNER_TOKEN"]
    sessions = request("/owner/sessions", token)
    if any(item.get("status") == "active" for item in sessions):
        raise SystemExit("An active browser session exists; refusing to disturb it.")
    session_id = None
    try:
        code = getpass.getpass("Current phone authenticator code: ").strip()
        created = request("/owner/sessions", token, "POST", {
            "start_url": "https://example.com", "totp_code": code,
        })
        session_id = created["id"]
        time.sleep(5)
        stats = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}} {{.CPUPerc}}",
             "auto-browser-browser-node-1", "auto-browser-controller-1", "auto-browser-approval-broker-1"],
            check=True, capture_output=True, text=True,
        )
        print(stats.stdout, end="")
    finally:
        if session_id:
            request(f"/owner/sessions/{session_id}", token, "DELETE")
            print("Closed measurement session.")


if __name__ == "__main__":
    main()

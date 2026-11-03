#!/usr/bin/env python3
"""A real Chromium download through the controller, read back by the controller (compose-smoke).

The controller does not run as browser-node's uid (10001): it runs as the owner of ./data (CI's runner) or 10002.
It never reads Chromium's own download file: Playwright's save_as streams the bytes from the browser-node server
into the controller's artifacts. This proves that end to end. A file is served from inside browser-node on
localhost (in the default ALLOWED_HOSTS), the session navigates to it, and the captured copy must hold the same
bytes, readable by the controller's own uid.

Run from the repo root with the default compose stack up. Never prints the token.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = f"http://127.0.0.1:{os.environ.get('API_PORT', '8000')}"
PORT = 8099


def compose(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", "compose", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"FAIL: docker compose {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def api(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    token = Path("data/api-token").read_text(encoding="utf-8").strip()
    # API is the local controller (http://127.0.0.1:<port>), never a URL from input.
    request = urllib.request.Request(API + path, method=method, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data=data, timeout=120) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")[:200]


def main() -> int:
    content = f"auto-browser download probe {secrets.token_hex(8)}\n"
    compose(
        "exec",
        "-T",
        "browser-node",
        "sh",
        "-c",
        f"mkdir -p /tmp/dlprobe && printf '%s' '{content}' > /tmp/dlprobe/probe.bin",
    )
    compose(
        "exec",
        "-d",
        "-T",
        "browser-node",
        "python3",
        "-m",
        "http.server",
        str(PORT),
        "--bind",
        "127.0.0.1",
        "--directory",
        "/tmp/dlprobe",  # noqa: S108 - inside the browser-node container, not on the host
    )
    time.sleep(1)
    status, session = api("POST", "/sessions", {"name": "download-smoke", "start_url": "https://example.com"})
    if status != 200 or not isinstance(session, dict):
        print(f"FAIL: create session: HTTP {status} {session}")
        return 1
    sid = session["id"]
    try:
        status, result = api("POST", f"/sessions/{sid}/actions/navigate", {"url": f"http://localhost:{PORT}/probe.bin"})
        print(f"navigate to a download: HTTP {status} (a download aborts the navigation)")
        records: list = []
        for _ in range(20):
            status, records = api("GET", f"/sessions/{sid}/downloads")
            if isinstance(records, list) and records:
                break
            time.sleep(0.5)
        if not isinstance(records, list) or not records:
            print(f"FAIL: no download recorded: HTTP {status} {records}")
            return 1
        record = records[-1]
        print("record:", {key: record.get(key) for key in ("status", "filename", "path", "failure")})
        if record.get("status") != "completed":
            print("FAIL: the download did not complete")
            return 1
        uid = compose("exec", "-T", "controller", "awk", "/^Uid:/ {print $2}", "/proc/1/status").strip()
        owner = compose("exec", "-T", "controller", "stat", "-c", "%u %a", record["path"]).strip()
        read = compose("exec", "-T", "controller", "cat", record["path"])
        print(f"controller uid {uid}; capture owner and mode {owner}")
        if read != content:
            print("FAIL: the controller read different bytes from its capture")
            return 1
        if owner.split()[0] != uid:
            print(f"FAIL: the capture belongs to uid {owner.split()[0]}, not the controller's {uid}")
            return 1
        if uid in {"0", "10001"}:
            print(f"FAIL: the controller runs as uid {uid}")
            return 1
        print("OK: Chromium's download was captured and read back by the controller (not root, not the browser's uid)")
        return 0
    finally:
        api("DELETE", f"/sessions/{sid}")
        compose("exec", "-T", "browser-node", "pkill", "-f", f"http.server {PORT}", check=False)


if __name__ == "__main__":
    sys.exit(main())

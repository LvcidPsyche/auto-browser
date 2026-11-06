#!/usr/bin/env python3
"""Check the controller's BROWSER_SANDBOX=on gate against a real browser-node (CI's compose-smoke).

usage: check_sandbox_gate.py --expect 200|409|503 [--isolated] [--log-must-say TEXT] [--api-port N] [--data DIR]

--expect 200: a session is created, then closed. Shared mode: the shared browser-node's state names exactly the
              endpoint file's content (the run the controller connected to). --isolated (docker_ephemeral): the
              session container's own state is a verified on with /proc evidence (assert_browser_sandbox.py's rules).
--expect 409: session creation is refused by the gate (a browser-node that is not a verified "on").
--expect 503: session creation fails because the controller cannot reach the browser-node (one that refused to
              start publishes no endpoint).
--log-must-say (with 409 or 503): and the controller's log (docker compose logs controller) says so.

The token is read from DIR/api-token and never printed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def call(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - a loopback http URL
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - a loopback URL
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", type=int, choices=(200, 409, 503), required=True)
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--data", default="data")
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--log-must-say", default=None)
    args = parser.parse_args(argv[1:])
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from assert_browser_sandbox import problems, redact_endpoints

    data = Path(args.data)
    token = (data / "api-token").read_text(encoding="utf-8").strip()
    base = f"http://127.0.0.1:{args.api_port}"
    for _ in range(90):
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=5):  # noqa: S310
                break
        except OSError:
            time.sleep(2)
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    status, body = call("POST", f"{base}/sessions", token, {"name": "sandbox-gate", "start_url": "https://example.com"})
    print(f"create session: HTTP {status}")
    if status != args.expect:
        print(f"FAIL: expected HTTP {args.expect}, got {status}: {redact_endpoints(json.dumps(body))[:300]}")
        return 1
    if args.expect == 200 and args.isolated:
        state_file = data / "browser-sessions" / body["id"] / "profile" / "browser-sandbox.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        close_status, _ = call("DELETE", f"{base}/sessions/{body['id']}", token)
        found = problems(state, require_proc=True)
        if found or close_status != 200:
            print(
                f"FAIL: the isolated container's state: {redact_endpoints(found or 'fine')}; close: HTTP {close_status}"
            )
            return 1
        print("OK: the isolated session's container ran with a verified sandbox (/proc evidence)")
        return 0
    if args.expect == 200:
        close_status, _ = call("DELETE", f"{base}/sessions/{body['id']}", token)
        if close_status != 200:
            print(f"FAIL: closing the session answered HTTP {close_status}")
            return 1
        state = json.loads((data / "browser-profile" / "browser-sandbox.json").read_text(encoding="utf-8"))
        endpoint = (data / "browser-profile" / "browser-ws-endpoint.txt").read_text(encoding="utf-8").strip()
        if state.get("endpoint") != endpoint:
            print(
                f"FAIL: the state names {redact_endpoints(state.get('endpoint'))!r}, "
                f"the endpoint file holds {redact_endpoints(endpoint)!r}"
            )
            return 1
        print("OK: the session ran on a verified browser-node, and the state names the endpoint the controller used")
    else:
        if args.log_must_say is not None:
            logs = subprocess.run(
                # Only what was logged since this check began: an older run's WARNING must not satisfy it.
                [shutil.which("docker") or "docker", "compose", "logs", "--since", started, "controller"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            if args.log_must_say not in logs:
                print(f"FAIL: the controller's log does not say {args.log_must_say!r}")
                return 1
        print("OK: the controller refused the session")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

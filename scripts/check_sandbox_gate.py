#!/usr/bin/env python3
"""Check the controller's BROWSER_SANDBOX=on gate against a real browser-node (CI's compose-smoke).

usage: check_sandbox_gate.py --expect 200|409 [--api-port N] [--data DIR]

--expect 200: a session is created (the shared browser-node's snapshot is a verified "on"), then closed, and the
              state's endpoint is exactly the endpoint file's content (the run the controller connected to).
--expect 409: session creation is refused (a browser-node that refused to start sandboxed).

The token is read from DIR/api-token and never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def call(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
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
    parser.add_argument("--expect", type=int, choices=(200, 409), required=True)
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--data", default="data")
    args = parser.parse_args(argv[1:])
    data = Path(args.data)
    token = (data / "api-token").read_text(encoding="utf-8").strip()
    base = f"http://127.0.0.1:{args.api_port}"
    for _ in range(90):
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=5):  # noqa: S310
                break
        except OSError:
            time.sleep(2)
    status, body = call("POST", f"{base}/sessions", token, {"name": "sandbox-gate", "start_url": "https://example.com"})
    print(f"create session: HTTP {status}")
    if status != args.expect:
        print(f"FAIL: expected HTTP {args.expect}, got {status}: {json.dumps(body)[:300]}")
        return 1
    if args.expect == 200:
        call("DELETE", f"{base}/sessions/{body['id']}", token)
        state = json.loads((data / "browser-profile" / "browser-sandbox.json").read_text(encoding="utf-8"))
        endpoint = (data / "browser-profile" / "browser-ws-endpoint.txt").read_text(encoding="utf-8").strip()
        if state.get("endpoint") != endpoint:
            print(f"FAIL: the state names {state.get('endpoint')!r}, the endpoint file holds {endpoint!r}")
            return 1
        print("OK: the session ran on a verified browser-node, and the state names the endpoint the controller used")
    else:
        print("OK: the controller refused the session")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

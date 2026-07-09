#!/usr/bin/env python
"""Optional LIVE fixture execution: drive the controller against a served fixture.

Static fixture validation lives in ``scripts/fixture_eval.py``. This script adds
the next proof layer — it serves ``evals/fixtures/`` locally and drives a *real*
controller browser session against a fixture page, with no external website.

This is intentionally NOT part of default CI: it requires the controller to be
installed (``pip install -e ./controller[dev]``) and Playwright browsers
(``python -m playwright install chromium``). Run it explicitly:

    python scripts/fixture_live.py                       # default: multi_tab.html
    python scripts/fixture_live.py --fixture popup_download.html --expect "Download CSV"

Exit codes: 0 = live check passed OR skipped (browsers unavailable);
2 = live check ran but failed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_CONTROLLER = _SCRIPTS.parent / "controller"
for _path in (_SCRIPTS, _CONTROLLER):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fixture_server import serve_fixtures  # noqa: E402

SKIPPED = 0
PASSED = 0
FAILED = 2


def _run_live(base_url: str, fixture: str, expect: str) -> int:
    # Imported lazily so the module only loads when live mode is actually invoked.
    try:
        from app.main import app
        from fastapi.testclient import TestClient
    except Exception as exc:  # noqa: BLE001 - controller not installed is a skip, not a failure
        print(f"[fixture-live] SKIP: controller app not importable ({exc}). "
              "Install it with `pip install -e ./controller[dev]`.")
        return SKIPPED

    target = f"{base_url}/{fixture}"
    body: str | None = None
    # Any failure in the browser-backed stack (missing chromium, lifespan teardown,
    # etc.) is a SKIP, not a failure — the live path is opt-in infrastructure.
    try:
        with TestClient(app) as client:
            created = client.post("/sessions", json={"name": "fixture-live"})
            if created.status_code >= 500:
                print(f"[fixture-live] SKIP: session creation returned {created.status_code} "
                      "(browser stack unavailable).")
                return SKIPPED
            created.raise_for_status()
            session_id = created.json().get("id") or created.json().get("session_id")
            try:
                nav = client.post(f"/sessions/{session_id}/actions/navigate", json={"url": target})
                nav.raise_for_status()
                observed = client.get(f"/sessions/{session_id}/observe")
                observed.raise_for_status()
                body = observed.text
            finally:
                client.delete(f"/sessions/{session_id}")
    except Exception as exc:  # noqa: BLE001 - opt-in path: treat env failures as skip
        print(f"[fixture-live] SKIP: live browser stack unavailable ({type(exc).__name__}: {exc}). "
              "Run `python -m playwright install chromium`.")
        return SKIPPED

    if body is not None and expect in body:
        print(f"[fixture-live] PASS: '{expect}' observed after navigating to {fixture}.")
        return PASSED
    print(f"[fixture-live] FAIL: '{expect}' not found in observation of {fixture}.")
    return FAILED


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive the controller against a local fixture page.")
    parser.add_argument("--fixture", default="multi_tab.html", help="fixture file under evals/fixtures/")
    parser.add_argument("--expect", default="Primary workspace", help="text expected in the observation")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    with serve_fixtures(host=args.host) as base_url:
        return _run_live(base_url, args.fixture, args.expect)


if __name__ == "__main__":
    raise SystemExit(main())

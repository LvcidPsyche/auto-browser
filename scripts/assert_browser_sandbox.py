#!/usr/bin/env python3
"""Fail unless a browser container's browser-sandbox.json proves Chromium's sandbox engaged.

CI's compose-smoke runs it after starting browser-node with BROWSER_SANDBOX=on on a runner whose kernel allows
unprivileged user namespaces (kernel.apparmor_restrict_unprivileged_userns=0): the sandbox must then be "on", with at
least one verified renderer in its own user namespace. Unlike sandbox-report.mjs, which only prints, this exits 1, so
a regression that stops the sandbox engaging, or makes the verification vacuous, turns CI red.

usage: assert_browser_sandbox.py <path to browser-sandbox.json> [--require-proc]

--require-proc (CI, on a kernel of 5.9 or later) also refuses evidence from chrome://sandbox instead of /proc.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def problems(state: object, *, require_proc: bool = False) -> list[str]:
    if not isinstance(state, dict):
        return ["the state is not a JSON object"]
    found = []
    if state.get("setting") != "on":
        # Under auto a runner that allows the sandbox engages it too: that would hide BROWSER_SANDBOX not arriving.
        found.append(f"setting is {state.get('setting')!r}, not 'on'")
    if state.get("sandbox") != "on":
        found.append(f"sandbox is {state.get('sandbox')!r} ({state.get('cause')}): {state.get('detail')}")
    verified = state.get("verified")
    if not isinstance(verified, dict):
        found.append("no verification evidence")
        return found
    renderers = verified.get("renderers")
    if not isinstance(renderers, int) or isinstance(renderers, bool) or renderers < 1:
        found.append(f"verified renderers is {renderers!r}, not at least 1")
    if verified.get("renderer_userns_differs") is not True:
        found.append("renderers are not shown in a user namespace of their own")
    filters = verified.get("renderer_seccomp_filters")
    browser_filters = verified.get("browser_seccomp_filters")
    if require_proc and "method" in verified:
        found.append(f"evidence came from {verified.get('method')}, not /proc (a kernel of 5.9 or later reports it)")
    if verified.get("method") != "chrome://sandbox":
        if not isinstance(filters, list) or not filters or not isinstance(browser_filters, int):
            found.append("no seccomp filter counts")
        elif not all(isinstance(count, int) and count > browser_filters for count in filters):
            found.append(f"renderer seccomp filters {filters} are not above the browser's {browser_filters}")
    return found


def main(argv: list[str]) -> int:
    args = [arg for arg in argv[1:] if arg != "--require-proc"]
    if len(args) != 1:
        print("usage: assert_browser_sandbox.py <path to browser-sandbox.json> [--require-proc]")
        return 2
    path = Path(args[0])
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"FAIL: could not read {path}: {exc}")
        return 1
    print(f"{path}: {json.dumps(state)}")
    found = problems(state, require_proc="--require-proc" in argv)
    for problem in found:
        print(f"FAIL: {problem}")
    if not found:
        print("OK: Chromium's sandbox engaged and was verified")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

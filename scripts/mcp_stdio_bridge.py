#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_ROOT = ROOT / "controller"
if str(CONTROLLER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_ROOT))

from app.mcp_stdio import main  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:8000/mcp"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def effective_base_url(argv: list[str], environ: dict[str, str]) -> str:
    """The URL the bridge will use: --base-url on the command line, else the environment, else the default."""
    for index, arg in enumerate(argv):
        if arg == "--base-url" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--base-url="):
            return arg.partition("=")[2]
    return environ.get("AUTO_BROWSER_BASE_URL") or DEFAULT_BASE_URL


def default_token_file(argv: list[str], environ: dict[str, str]) -> str | None:
    """Point the bridge at this checkout's data/api-token, for a loopback controller only.

    The compose controller writes its token there, so a checkout's bridge needs no
    token configured. A remote controller has a different token, and sending this
    checkout's token to that host would hand it a credential it did not issue.
    A bridge installed with uvx cannot know where the checkout is, which is why
    the module itself has no default.
    """
    if "AUTO_BROWSER_TOKEN_FILE" in environ:
        return None
    host = urlparse(effective_base_url(argv, environ)).hostname
    if host in LOOPBACK_HOSTS:
        environ["AUTO_BROWSER_TOKEN_FILE"] = str(ROOT / "data" / "api-token")
        return None
    return f"not defaulting AUTO_BROWSER_TOKEN_FILE: {host or 'the base URL'} is not a loopback host"


if __name__ == "__main__":
    skipped = default_token_file(sys.argv[1:], os.environ)
    if skipped:
        print(
            f"mcp_stdio_bridge: {skipped}; pass --token-file or AUTO_BROWSER_TOKEN_FILE to send a token",
            file=sys.stderr,
        )
    raise SystemExit(main())

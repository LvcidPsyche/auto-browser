#!/usr/bin/env python
"""Tiny static server for the local eval fixtures.

Serves ``evals/fixtures/`` (HTML + data files) over loopback so fixtures can be
driven live in a real browser without depending on any external website. Used by
``scripts/fixture_live.py`` and available as a standalone command.

    python scripts/fixture_server.py --port 8100      # serve until Ctrl-C
    python scripts/fixture_server.py --port 0          # ephemeral port (prints URL)

Or programmatically:

    from scripts.fixture_server import serve_fixtures
    with serve_fixtures() as base_url:
        ...  # GET {base_url}/multi_tab.html
"""

from __future__ import annotations

import argparse
import contextlib
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_ROOT = ROOT / "evals" / "fixtures"


class _QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that doesn't spam stderr with a line per request."""

    def log_message(self, *_args: object) -> None:  # noqa: D401 - silence access log
        return


@contextlib.contextmanager
def serve_fixtures(
    directory: str | Path = DEFAULT_FIXTURE_ROOT,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> Iterator[str]:
    """Serve ``directory`` on a background thread; yield the base URL.

    ``port=0`` binds an ephemeral port. The server is shut down on exit.
    """
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"fixture directory not found: {directory}")

    handler = partial(_QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bound_host, bound_port = server.server_address[:2]
    try:
        yield f"http://{bound_host}:{bound_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve local eval fixtures over loopback.")
    parser.add_argument("--fixture-root", default=str(DEFAULT_FIXTURE_ROOT))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100, help="0 for an ephemeral port")
    args = parser.parse_args()

    with serve_fixtures(args.fixture_root, host=args.host, port=args.port) as base_url:
        print(f"Serving {args.fixture_root} at {base_url} (Ctrl-C to stop)")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\nstopping fixture server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

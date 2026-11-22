"""The Codex host bridge script, scripts/codex_host_bridge.py, as the tests that drive it load it."""

from __future__ import annotations

import importlib.util
import socketserver
import sys
from pathlib import Path

BRIDGE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "codex_host_bridge.py"


def load_bridge(name: str, path: Path = BRIDGE_SCRIPT):
    """Load the script (or a copy of it at ``path``) as module ``name``, each test file's own, so a file's patches stay
    its own; UnixStreamServer is missing on Windows (import only)."""
    shimmed = not hasattr(socketserver, "UnixStreamServer")
    if shimmed:
        socketserver.UnixStreamServer = socketserver.TCPServer
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if shimmed:
            del socketserver.UnixStreamServer

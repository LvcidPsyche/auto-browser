#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info < (3, 11):
    raise SystemExit("auto-browser controller tooling requires Python 3.11+")

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_ROOT = ROOT / "controller"
if str(CONTROLLER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_ROOT))

from app.mcp_stdio import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

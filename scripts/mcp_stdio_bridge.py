#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_ROOT = ROOT / "controller"
if str(CONTROLLER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_ROOT))

# The compose controller writes its token to ./data/api-token in this checkout, so
# a checkout's bridge needs no token configured. A bridge installed with uvx cannot
# know where the checkout is, which is why the module itself has no default.
os.environ.setdefault("AUTO_BROWSER_TOKEN_FILE", str(ROOT / "data" / "api-token"))

from app.mcp_stdio import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

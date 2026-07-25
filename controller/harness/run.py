from __future__ import annotations

import sys
from pathlib import Path

CONTROLLER_ROOT = Path(__file__).resolve().parents[1]
if str(CONTROLLER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_ROOT))

from app.harness.run import main

if __name__ == "__main__":
    raise SystemExit(main())

"""pytest configuration — ensure app package is importable from tests."""

import os
import sys
from pathlib import Path

# Add controller directory to sys.path so `from app.xxx import ...` works
sys.path.insert(0, str(Path(__file__).parent))

# API_BIND_SCOPE defaults to `exposed` so that an undeclared deployment fails
# closed (app/auth_policy.py). A TestClient run is loopback by construction and
# never publishes anything, so the suite declares that rather than inheriting a
# production-safety default it does not model. Tests that exercise the exposed
# path build their own Settings and must not rely on this.
os.environ.setdefault("API_BIND_SCOPE", "loopback")

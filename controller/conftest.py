"""pytest configuration — ensure app package is importable from tests."""

import sys
from pathlib import Path

# Add controller directory to sys.path so `from app.xxx import ...` works
sys.path.insert(0, str(Path(__file__).parent))

# The suite's environment defaults live in tests/__init__.py, which ships in
# the controller image; conftest.py does not.

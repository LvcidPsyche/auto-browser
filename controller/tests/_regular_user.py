"""Make os.geteuid report a regular user, for the tests of what shadow browsing does once it starts.

Chromium cannot start sandboxed as root, so enable_shadow_browse refuses when os.geteuid() is 0. CI's
controller-tests job runs the suite as root in the image, where those tests would stop at the refusal.
regular_user() changes only what os.geteuid reports (creating it on Windows, which has none): the process
keeps its real uid, so a test using it must hand enable_shadow_browse a stand-in launch, since a real
Chromium started as root still fails. test_shadow_browse_sandbox.py pins the refusal itself.
"""

from __future__ import annotations

import os
from unittest.mock import patch

REGULAR_UID = 1000


def regular_user():
    """A context manager: os.geteuid reports a regular user inside it (created where the platform has none)."""
    return patch.object(os, "geteuid", create=True, return_value=REGULAR_UID)

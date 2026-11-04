"""app.drop_caps (D2): the cannot-switch branch's command starts with no capability in any set and no_new_privs.

Linux, as root (the CI root step and the image): a child drops, then reads its own /proc/self/status, and a command
exec'd through `python -m app.drop_caps --` reads its status the same way.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

ROOT_ON_LINUX = sys.platform == "linux" and hasattr(os, "geteuid") and os.geteuid() == 0


def _caps(text: str) -> dict[str, str]:
    return {
        line.split(":")[0]: line.split()[1]
        for line in text.splitlines()
        if line.startswith(("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs"))
    }


@unittest.skipUnless(ROOT_ON_LINUX, "needs root on Linux (the image, CI's root step)")
class DropCapsTests(unittest.TestCase):
    def test_the_exec_d_command_holds_nothing(self) -> None:
        before = _caps(open("/proc/self/status", encoding="utf-8").read())
        self.assertNotEqual(before["CapEff"], "0000000000000000", "the test needs a root with capabilities")
        result = subprocess.run(
            [sys.executable, "-m", "app.drop_caps", "--", "cat", "/proc/self/status"],
            capture_output=True,
            text=True,
            check=True,
        )
        after = _caps(result.stdout)
        for key in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
            self.assertEqual(after[key], "0000000000000000", (key, after))
        self.assertEqual(after["NoNewPrivs"], "1")

    def test_a_bad_invocation_is_refused(self) -> None:
        result = subprocess.run([sys.executable, "-m", "app.drop_caps", "cat"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 64)


if __name__ == "__main__":
    unittest.main()

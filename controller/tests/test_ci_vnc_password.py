"""CI's compose-smoke exercises the shared browser-node with a VNC password.

Both browser-node VNC checks in compose-smoke pass --expect-password only when
VNC_PASSWORD is set, and the job never set it, so CI only ever proved a
password-less noVNC (security type None) and never the path operators are told
to use. The job now sets a fixed CI-only VNC_PASSWORD (it guards nothing real)
for every step, so both checks require VNC authentication, and the isolated
smoke's browser-node probe runs strict instead of printing its RESIDUAL line.

Regex over the workflow, no PyYAML; skipped where .github/ is absent (the
controller image).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

CI_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _job(text: str, name: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text)
    assert match, f"no {name} job in ci.yml"
    return match.group(1)


@unittest.skipUnless(CI_WORKFLOW.is_file(), ".github/ is not shipped inside the controller image")
class ComposeSmokeVncPasswordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.job = _job(CI_WORKFLOW.read_text(encoding="utf-8"), "compose-smoke")

    def test_the_job_sets_a_vnc_password_for_every_step(self) -> None:
        # Job-level env (4-space indent), so doctor, both VNC checks and the isolated smoke all see it.
        env = re.search(r"(?ms)^    env:\n((?:^      .*\n)+)", self.job)
        self.assertIsNotNone(env, "compose-smoke has no job-level env")
        value = re.search(r"(?m)^      VNC_PASSWORD:\s*(\S.*?)\s*$", env.group(1))
        self.assertIsNotNone(value, "compose-smoke does not set VNC_PASSWORD")
        self.assertNotIn(value.group(1).strip("'\""), ("", "''", '""'))

    def test_both_browser_node_vnc_checks_expect_the_password(self) -> None:
        checks = re.findall(r"check_vnc_exposure\.py[^\n]*\n[^\n]*", self.job)
        self.assertEqual(len(checks), 2, checks)
        for check in checks:
            self.assertRegex(check, r"--expect-password")


if __name__ == "__main__":
    unittest.main()

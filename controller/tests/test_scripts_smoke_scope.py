"""The smoke scripts' teardown only removes what the smoke run created.

smoke_isolated_session.sh and its tunnel twin removed every container and network
carrying auto-browser.managed=true and ran `compose down` with no project, so on a
machine that also runs the operator's own stack the smoke deleted their live session
containers and, sharing the folder's default project name, their controller too.

The dynamic test runs the real smoke_snapshot_managed / smoke_cleanup (scripts/load_env.sh)
in bash against a stand-in docker that records its calls. The static test covers every
smoke script. Repo-layout checks: skipped where scripts/ is absent (the controller
image) or bash is missing.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_scripts_token_argv import BASH, SCRIPTS, _bash_path

SMOKE_SCRIPTS = sorted(SCRIPTS.glob("smoke_*.sh")) if SCRIPTS.is_dir() else []

FAKE_DOCKER = """#!/usr/bin/env bash
state="$FAKE_DOCKER_DIR"
echo "$*" >> "$state/calls.log"
case "$1 $2" in
  "ps -aq") cat "$state/containers.txt" ;;
  "network ls") cat "$state/networks.txt" ;;
esac
exit 0
"""


@unittest.skipUnless(SCRIPTS.is_dir() and BASH, "scripts/ or bash is not available here")
class SmokeCleanupScopeTests(unittest.TestCase):
    def test_cleanup_removes_only_what_appeared_after_the_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp = Path(tempdir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            docker = fake_bin / "docker"
            docker.write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")
            docker.chmod(0o755)
            state = _bash_path(temp)
            script = (
                f'export PATH="{_bash_path(fake_bin)}:$PATH"; export FAKE_DOCKER_DIR="{state}"; '
                f'source "{_bash_path(SCRIPTS / "load_env.sh")}"; '
                f'printf "user-c1\\nuser-c2\\n" > "{state}/containers.txt"; '
                f'printf "user-net\\n" > "{state}/networks.txt"; '
                "smoke_snapshot_managed; "
                f'printf "user-c1\\nuser-c2\\nsmoke-c1\\n" > "{state}/containers.txt"; '
                f'printf "user-net\\nsmoke-net\\n" > "{state}/networks.txt"; '
                "COMPOSE=(docker compose -p smoke-proj -f docker-compose.yml); "
                "smoke_cleanup"
            )
            result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (temp / "calls.log").read_text(encoding="utf-8").splitlines()

        self.assertIn("rm -f smoke-c1", calls)
        self.assertIn("network rm smoke-net", calls)
        self.assertFalse([c for c in calls if "user-" in c and ("rm" in c.split())], calls)
        down = [c for c in calls if "down" in c.split()]
        self.assertEqual(len(down), 1, calls)
        self.assertIn("compose -p smoke-proj", down[0])
        # The stack goes down between the containers and the networks.
        self.assertLess(calls.index("rm -f smoke-c1"), calls.index(down[0]))
        self.assertLess(calls.index(down[0]), calls.index("network rm smoke-net"))


@unittest.skipUnless(SMOKE_SCRIPTS, "scripts/ is not available here")
class SmokeScriptsAreScopedTests(unittest.TestCase):
    def test_no_smoke_script_removes_by_the_managed_label_alone(self) -> None:
        for script in SMOKE_SCRIPTS:
            with self.subTest(script=script.name):
                text = script.read_text(encoding="utf-8")
                self.assertNotIn("auto-browser.managed", text)
                self.assertNotRegex(text, r"xargs\s+(-\S+\s+)*docker\s+(rm|network\s+rm)")

    def test_every_smoke_compose_names_its_own_project(self) -> None:
        for script in SMOKE_SCRIPTS:
            with self.subTest(script=script.name):
                text = script.read_text(encoding="utf-8")
                composes = re.findall(r"^COMPOSE=\((.*)\)$", text, flags=re.M)
                self.assertTrue(composes, "no COMPOSE array")
                for compose in composes:
                    self.assertIn('-p "${SMOKE_PROJECT}"', compose)


if __name__ == "__main__":
    unittest.main()

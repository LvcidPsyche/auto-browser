"""The root (Glama) image's entrypoint starts its stdio bridge only once uvicorn answers /healthz.

It started uvicorn in the background, polled /healthz for 60 seconds and then started the bridge whatever the answer:
a uvicorn that had already exited (a failed start, which --lifespan on now makes an exit) left the bridge talking to
nothing, and the container running. It now keeps uvicorn's pid, ends with uvicorn's status when uvicorn exits before
it is healthy, and exits 1 when the wait runs out. Run here with stubs for uvicorn, curl, sleep (a no-op, so the
60-second wait takes none) and the bridge's python. A repo-layout check: scripts/ is not in the controller image.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._bash import BASH, BASH_DIR, NO_BASH_REASON, path_after

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "scripts" / "glama_entrypoint.sh"
REAL_SLEEP = shutil.which("sleep", path=os.pathsep.join([BASH_DIR, os.environ.get("PATH", "")]))
STUB = "#!/usr/bin/env bash\n"


@unittest.skipUnless(ENTRYPOINT.is_file(), "scripts/ is not in this tree (the controller image ships app/ and tests/)")
@unittest.skipIf(BASH is None or REAL_SLEEP is None, NO_BASH_REASON)
class TheBridgeStartsOnlyOnAHealthyServerTests(unittest.TestCase):
    def _run(self, uvicorn: str, curl_status: int) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        (scratch / "app" / "app").mkdir(parents=True)  # the root image's layout: the app package at /app/app
        stubs = scratch / "bin"
        stubs.mkdir()
        calls = scratch / "calls.log"
        # A uvicorn that keeps running: until the test is over, with its output closed so the run's pipes can close.
        stop = scratch / "stop"
        self.addCleanup(stop.touch)
        running = (
            f'exec >/dev/null 2>&1; while [ -d "{scratch.as_posix()}" ] && [ ! -e "{stop.as_posix()}" ]; '
            f'do "{Path(REAL_SLEEP).as_posix()}" 0.2; done'
        )
        # One that logs as uvicorn does (its access log goes to stdout), then keeps running.
        logging = f'echo "INFO:     127.0.0.1 - \\"POST /mcp HTTP/1.1\\" 200 OK"; {running}'
        bodies = {
            "uvicorn": {"running": running, "logging": logging}.get(uvicorn, uvicorn),
            "curl": f'echo curl >> "{calls.as_posix()}"; exit {curl_status}',
            "sleep": "exit 0",
            "python": f'echo "bridge $*" >> "{calls.as_posix()}"; echo bridge-started',
        }
        for name, body in bodies.items():
            (stubs / name).write_text(STUB + body + "\n", encoding="utf-8", newline="\n")
            (stubs / name).chmod(0o755)
        # The script's own paths, moved under the scratch directory: /app (where the app package is looked for).
        script = re.sub(r"(?<![\w/])/app\b", f"{scratch.as_posix()}/app", ENTRYPOINT.read_text(encoding="utf-8"))
        (scratch / "entrypoint.sh").write_text(script, encoding="utf-8", newline="\n")
        env = {"PATH": path_after(stubs)}
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        result = subprocess.run(
            [BASH, "--posix", (scratch / "entrypoint.sh").as_posix()],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=env,
        )
        return result, calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []

    def test_a_uvicorn_that_exits_before_it_is_healthy_ends_the_script_with_its_status(self) -> None:
        result, calls = self._run(uvicorn="exit 3", curl_status=7)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertNotIn("bridge-started", result.stdout)
        self.assertIn("uvicorn exited", result.stderr)

    def test_a_uvicorn_that_exits_0_before_it_is_healthy_still_fails_the_script(self) -> None:
        result, _ = self._run(uvicorn="exit 0", curl_status=7)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("bridge-started", result.stdout)

    def test_a_uvicorn_never_healthy_in_60_seconds_ends_the_script_with_1(self) -> None:
        result, calls = self._run(uvicorn="running", curl_status=7)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("bridge-started", result.stdout)
        self.assertEqual(calls.count("curl"), 60)
        self.assertIn("not healthy", result.stderr)

    def test_a_healthy_server_gets_the_bridge(self) -> None:
        result, calls = self._run(uvicorn="running", curl_status=0)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bridge-started", result.stdout)
        self.assertEqual(calls[0], "curl")
        self.assertTrue(calls[-1].startswith("bridge -m app.mcp_stdio --base-url http://127.0.0.1:8000/mcp"), calls)

    def test_uvicorn_s_log_stays_off_the_stdout_the_bridge_answers_on(self) -> None:
        # The bridge speaks MCP's JSON-RPC on stdout, one message a line; uvicorn's access log there put lines such as
        # `INFO: ... "POST /mcp HTTP/1.1" 200 OK` among them (measured on the root image).
        result, _ = self._run(uvicorn="logging", curl_status=0)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["bridge-started"])
        self.assertIn("POST /mcp", result.stderr)


@unittest.skipUnless(ENTRYPOINT.is_file(), "scripts/ is not in this tree (the controller image ships app/ and tests/)")
class TheEntrypointsSettingsLoadTests(unittest.TestCase):
    def test_the_settings_it_gives_uvicorn_are_ones_the_controller_accepts(self) -> None:
        # It set SESSION_ISOLATION_MODE=glama_inspect, which 1.15.0 refuses: every start of the image failed at import.
        from app.config import Settings

        start = re.search(
            r"((?:^[A-Z_]+=\S+ \\\n)+)\s+uvicorn app\.main:app", ENTRYPOINT.read_text(encoding="utf-8"), re.M
        )
        self.assertIsNotNone(start, "no settings before the uvicorn command")
        given = dict(line.rstrip(" \\").split("=", 1) for line in start.group(1).splitlines())
        settings = Settings(_env_file=None, **given)
        self.assertFalse(settings.connect_at_startup, given)  # this image runs no browser-node to connect to

"""A malformed CONTROLLER_ALLOWED_HOSTS stops the controller's start, where it used to leave it serving 500s.

The Host check refuses a malformed entry when the app's middleware is built, which Starlette does at the app's first
ASGI call, the lifespan's startup. Under uvicorn's default `--lifespan auto` an exception there reads as an app that
does not speak the lifespan protocol: uvicorn logged "ASGI 'lifespan' protocol appears unsupported." at INFO and
served, the app's own startup never ran, and every request failed with a 500 as the build failed again. With
`--lifespan on` it is a failed start, and uvicorn exits. Each command that starts the app passes the flag (the start
commands test_graceful_shutdown.py finds), and the image's own command, run here with a malformed entry, exits
without serving. A repo-layout check: the controller image ships only app/ and tests/, so it skips there.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_graceful_shutdown import _start_lines

REPO = Path(__file__).resolve().parents[2]
CONTROLLER = REPO / "controller"
DOCKERFILE = CONTROLLER / "Dockerfile"
# "*" may only lead an entry, as "*.example.com" (app/host_patterns.py).
MALFORMED_ALLOWED_HOSTS = "localhost,local*host"
# How long the start may take to fail: the app's import alone takes seconds on a slow runner.
START_LIMIT_SECONDS = 90.0


def _image_command() -> list[str]:
    (line,) = [line for line in DOCKERFILE.read_text(encoding="utf-8").splitlines() if line.startswith("CMD ")]
    return json.loads(line.removeprefix("CMD "))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serves(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@unittest.skipUnless(DOCKERFILE.is_file(), "the repo's start commands are not in this tree")
class TheLifespanIsOnTests(unittest.TestCase):
    def test_each_command_that_starts_the_app_turns_the_lifespan_on(self) -> None:
        lines = _start_lines()
        self.assertTrue(lines)
        for name, line in lines:
            with self.subTest(file=name):
                # `--lifespan on` in a shell line, `"--lifespan", "on"` in an exec-form CMD.
                self.assertRegex(line, r'--lifespan"?[,\s]+"?on\b', f"{name} starts uvicorn without --lifespan on")

    def test_the_image_command_exits_without_serving_on_a_malformed_allowed_host(self) -> None:
        command = _image_command()
        self.assertEqual(command[:2], ["uvicorn", "app.main:app"])
        port = _free_port()
        command[command.index("--host") + 1] = "127.0.0.1"
        command[command.index("--port") + 1] = str(port)
        env = {**os.environ, "PYTHONPATH": str(CONTROLLER), "CONTROLLER_ALLOWED_HOSTS": MALFORMED_ALLOWED_HOSTS}
        # A working directory with no .env in it, which Settings would read.
        with tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [sys.executable, "-m", *command], cwd=workdir, env=env, stdout=output, stderr=subprocess.STDOUT
            )
            try:
                deadline = time.monotonic() + START_LIMIT_SECONDS
                served = False
                while process.poll() is None and time.monotonic() < deadline and not served:
                    served = _serves(port)
                    time.sleep(0.2)
                exited = process.poll() is not None
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=30)
            output.seek(0)
            log = output.read().decode("utf-8", errors="replace")
        self.assertFalse(served, f"uvicorn served with CONTROLLER_ALLOWED_HOSTS={MALFORMED_ALLOWED_HOSTS!r}:\n{log}")
        self.assertTrue(exited, f"uvicorn neither served nor exited in {START_LIMIT_SECONDS:.0f} s:\n{log}")
        # 3, uvicorn's status for a failed startup, as the docs and the CHANGELOG say: an import error is 1.
        self.assertEqual(process.returncode, 3, log)
        self.assertIsNotNone(re.search(r"invalid host pattern 'local\*host'", log), log)

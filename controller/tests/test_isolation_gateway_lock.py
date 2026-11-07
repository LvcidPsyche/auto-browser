"""Two parallel provisions probe the daemon's GwPriority support once and warn about the gateway hazard once (D8-2)."""

from __future__ import annotations

import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer

THREADS = 6


class SlowAttrs(dict):
    """Attrs whose read pauses, widening the window between the hazard check and the flag it sets."""

    def get(self, key, default=None):
        time.sleep(0.05)
        return super().get(key, default)


class GatewayProbeLockTests(unittest.TestCase):
    def provisioner(self, **overrides: str) -> tuple[DockerBrowserNodeProvisioner, FakeDockerClient]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_root = Path(tmp.name) / "data"
        data_root.mkdir()
        settings = Settings(_env_file=None, ARTIFACT_ROOT=str(data_root / "artifacts"), **overrides)
        controller = FakeDockerContainer(
            container_id="controller-id",
            name="controller",
            attrs=SlowAttrs({"NetworkSettings": {"Networks": {"auto-browser_default": {}}}}),
        )
        client = FakeDockerClient(controller)
        provisioner = DockerBrowserNodeProvisioner(settings, client=client)
        provisioner._controller_container = controller
        return provisioner, client

    @staticmethod
    def in_parallel(work) -> None:
        barrier = threading.Barrier(THREADS)

        def run() -> None:
            barrier.wait()
            work()

        threads = [threading.Thread(target=run) for _ in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def test_parallel_provisions_read_the_daemon_version_once(self) -> None:
        provisioner, client = self.provisioner()
        calls: list[int] = []
        real_version = client.version

        def slow_version() -> dict:
            calls.append(1)
            time.sleep(0.05)
            return real_version()

        client.version = slow_version

        self.in_parallel(lambda: provisioner._gw_priority_supported(client))

        self.assertEqual(len(calls), 1)

    def test_parallel_provisions_warn_about_the_hazard_once(self) -> None:
        provisioner, _ = self.provisioner()

        with self.assertLogs("app.session_isolation", level=logging.WARNING) as logs:
            self.in_parallel(lambda: provisioner._warn_gateway_hazard("aaa-session-x-net"))

        self.assertEqual(len([line for line in logs.output if "GwPriority" in line]), 1)


if __name__ == "__main__":
    unittest.main()

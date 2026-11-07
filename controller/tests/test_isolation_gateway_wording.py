"""The gateway-hazard warning says which API version is too old, and that its test compares names only (D8-4, D8-5)."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer


class GatewayWarningWordingTests(unittest.TestCase):
    def warning(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(_env_file=None, ARTIFACT_ROOT=str(Path(tmp) / "artifacts"))
            controller = FakeDockerContainer(
                container_id="controller-id",
                name="controller",
                attrs={"NetworkSettings": {"Networks": {"auto-browser_default": {}}}},
            )
            provisioner = DockerBrowserNodeProvisioner(settings, client=FakeDockerClient(controller))
            provisioner._controller_container = controller
            with self.assertLogs("app.session_isolation", level=logging.WARNING) as logs:
                provisioner._warn_gateway_hazard("aaa-session-x-net")
        return logs.output[0]

    def test_d8_5_it_covers_a_client_speaking_an_old_api_version_too(self) -> None:
        text = self.warning()

        self.assertIn("API version this client speaks", text)
        self.assertIn("below 1.48", text)

    def test_d8_4_it_says_the_check_compares_network_names_only(self) -> None:
        self.assertIn("names only", self.warning())


if __name__ == "__main__":
    unittest.main()

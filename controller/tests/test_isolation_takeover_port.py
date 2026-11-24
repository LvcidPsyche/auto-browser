"""The takeover URL an isolated session hands out can leave out, or replace, the published noVNC port.

Behind a reverse proxy that serves the takeover host on 443, the published host port is a port nothing listens
on: the link agents pass to the person times out. ISOLATED_TAKEOVER_PORT=none drops the port, a number replaces
it, and auto keeps the old behaviour.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer


class TakeoverPortTests(unittest.IsolatedAsyncioTestCase):
    async def takeover_url(self, **overrides: str) -> str:
        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "data"
            data_root.mkdir()
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(data_root / "artifacts"),
                ISOLATED_TAKEOVER_HOST="vnc.example.com",
                ISOLATED_TAKEOVER_SCHEME="https",
                **overrides,
            )
            controller = FakeDockerContainer(
                container_id="controller-id",
                name="controller",
                attrs={
                    "Mounts": [{"Destination": "/data", "Source": str(data_root)}],
                    "NetworkSettings": {"Networks": {"auto-browser_default": {}}},
                },
            )
            provisioner = DockerBrowserNodeProvisioner(settings, client=FakeDockerClient(controller))
            provisioner._controller_container_id = controller.id
            runtime = await provisioner.provision("session-port")
            return runtime.takeover_url

    async def test_auto_keeps_the_published_port(self) -> None:
        url = await self.takeover_url()
        self.assertRegex(url, r"^https://vnc\.example\.com:\d+/vnc\.html")
        self.assertTrue(url.endswith("/vnc.html?autoconnect=true&resize=scale"))

    async def test_none_drops_the_port_for_a_proxy_on_443(self) -> None:
        url = await self.takeover_url(ISOLATED_TAKEOVER_PORT="none")
        self.assertTrue(url.startswith("https://vnc.example.com/vnc.html"), url)
        self.assertNotIn(":443", url)
        self.assertNotRegex(url, r"vnc\.example\.com:\d")

    async def test_a_number_replaces_the_published_port(self) -> None:
        url = await self.takeover_url(ISOLATED_TAKEOVER_PORT="8443")
        self.assertTrue(url.startswith("https://vnc.example.com:8443/vnc.html"), url)

    def test_an_invalid_port_is_rejected_at_startup(self) -> None:
        with self.assertRaises(ValueError):
            Settings(_env_file=None, ISOLATED_TAKEOVER_PORT="http")


if __name__ == "__main__":
    unittest.main()

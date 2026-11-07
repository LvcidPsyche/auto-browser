"""An isolated browser's noVNC admits the tunnel's public host too (INFRA-26).

The takeover URL a session hands out is the tunnel's public URL once the tunnel is active, but the container's
NOVNC_ALLOWED_HOSTS only held ISOLATED_TAKEOVER_HOST, so noVNC refused its own page there (a DNS-rebinding guard).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer


class NovncAllowedHostsTests(unittest.IsolatedAsyncioTestCase):
    async def allowed_hosts(self, **overrides: str) -> str:
        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "data"
            data_root.mkdir()
            settings = Settings(_env_file=None, ARTIFACT_ROOT=str(data_root / "artifacts"), **overrides)
            controller = FakeDockerContainer(
                container_id="controller-id",
                name="controller",
                attrs={
                    "Mounts": [{"Destination": "/data", "Source": str(data_root)}],
                    "NetworkSettings": {"Networks": {"auto-browser_default": {}}},
                },
            )
            client = FakeDockerClient(controller)
            provisioner = DockerBrowserNodeProvisioner(settings, client=client)
            provisioner._controller_container_id = controller.id
            runtime = await provisioner.provision("session-hosts")
            return client.containers.browser_containers[runtime.container_name].run_kwargs["environment"][
                "NOVNC_ALLOWED_HOSTS"
            ]

    async def test_the_tunnel_public_host_is_allowed_beside_the_takeover_host(self) -> None:
        hosts = await self.allowed_hosts(
            ISOLATED_TAKEOVER_HOST="127.0.0.1",
            ISOLATED_TUNNEL_ENABLED="true",
            ISOLATED_TUNNEL_HOST="bastion.example.com",
            ISOLATED_TUNNEL_PUBLIC_HOST="takeover.example.org",
        )

        self.assertEqual(hosts.split(), ["127.0.0.1", "takeover.example.org"])

    async def test_without_a_public_host_the_tunnel_host_is_the_one(self) -> None:
        hosts = await self.allowed_hosts(
            ISOLATED_TUNNEL_ENABLED="true",
            ISOLATED_TUNNEL_HOST="bastion.example.com",
        )

        self.assertEqual(hosts.split(), ["127.0.0.1", "bastion.example.com"])

    async def test_a_host_already_listed_is_not_repeated(self) -> None:
        hosts = await self.allowed_hosts(
            ISOLATED_TAKEOVER_HOST="bastion.example.com",
            ISOLATED_TUNNEL_ENABLED="true",
            ISOLATED_TUNNEL_HOST="bastion.example.com",
        )

        self.assertEqual(hosts.split(), ["bastion.example.com"])

    async def test_with_the_tunnel_off_only_the_takeover_host_is_allowed(self) -> None:
        hosts = await self.allowed_hosts(ISOLATED_TUNNEL_HOST="bastion.example.com")

        self.assertEqual(hosts, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()

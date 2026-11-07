"""An unreachable Docker daemon is not a gone container: release fails and keeps the session's files (NAV-16)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import docker.errors
import requests

from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer


class DaemonErrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data_root = Path(self.tmp.name) / "data"
        data_root.mkdir()
        settings = Settings(_env_file=None, ARTIFACT_ROOT=str(data_root / "artifacts"))
        controller = FakeDockerContainer(
            container_id="controller-id",
            name="controller",
            attrs={
                "Mounts": [{"Destination": "/data", "Source": str(data_root)}],
                "NetworkSettings": {"Networks": {"auto-browser_default": {}}},
            },
        )
        self.client = FakeDockerClient(controller)
        self.provisioner = DockerBrowserNodeProvisioner(settings, client=self.client)
        self.provisioner._controller_container_id = controller.id
        self.runtime = await self.provisioner.provision("session-down")
        self.profile = self.provisioner._local_runtime_root("session-down")
        self.assertTrue(self.profile.is_dir())

    def daemon_down(self, collection) -> None:
        def get(identifier: str):
            raise requests.exceptions.ConnectionError("Error while fetching server API version")

        collection.get = get

    async def test_an_unreachable_daemon_fails_the_release_and_keeps_the_profile(self) -> None:
        self.daemon_down(self.client.containers)

        with self.assertRaises(requests.exceptions.ConnectionError):
            await self.provisioner.release(self.runtime)

        self.assertTrue(self.profile.exists(), "the daemon could not be asked, so nothing may be removed")

    async def test_an_unreachable_daemon_fails_the_network_release_too(self) -> None:
        self.daemon_down(self.client.networks)

        with self.assertRaises(requests.exceptions.ConnectionError):
            await self.provisioner.release(self.runtime)

        self.assertTrue(self.profile.exists())

    async def test_a_container_the_daemon_does_not_know_is_still_released_and_cleaned(self) -> None:
        def missing(identifier: str):
            raise docker.errors.NotFound(identifier)

        self.client.containers.get = missing

        await self.provisioner.release(self.runtime)

        self.assertFalse(self.profile.exists())


if __name__ == "__main__":
    unittest.main()

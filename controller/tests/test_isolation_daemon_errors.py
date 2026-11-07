"""An unreachable Docker daemon is not a gone container: release fails and keeps the session's files (NAV-16)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import docker.errors
import requests

from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner, IsolatedBrowserRuntime
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer


async def _provisioned(
    tmp: str, session_id: str, **settings: str
) -> tuple[DockerBrowserNodeProvisioner, FakeDockerClient, IsolatedBrowserRuntime]:
    data_root = Path(tmp) / "data"
    data_root.mkdir()
    controller = FakeDockerContainer(
        container_id="controller-id",
        name="controller",
        attrs={
            "Mounts": [{"Destination": "/data", "Source": str(data_root)}],
            "NetworkSettings": {"Networks": {"auto-browser_default": {}}},
        },
    )
    client = FakeDockerClient(controller)
    provisioner = DockerBrowserNodeProvisioner(
        Settings(_env_file=None, ARTIFACT_ROOT=str(data_root / "artifacts"), **settings), client=client
    )
    provisioner._controller_container_id = controller.id
    return provisioner, client, await provisioner.provision(session_id)


class DaemonErrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.provisioner, self.client, self.runtime = await _provisioned(self.tmp.name, "session-down")
        self.profile = self.provisioner._local_runtime_root("session-down")
        self.assertTrue(self.profile.is_dir())

    def daemon_down(self, collection) -> None:
        def get(identifier: str):
            raise requests.exceptions.ConnectionError("Error while fetching server API version")

        collection.get = get

    async def test_an_unreachable_daemon_fails_the_release_and_keeps_the_profile(self) -> None:
        self.daemon_down(self.client.containers)

        # R5C-10: the designed failure, with the daemon's error as its cause, where the lookup's error escaped bare
        # and the session network was not released at all.
        with self.assertRaisesRegex(RuntimeError, "could not be looked up") as raised:
            await self.provisioner.release(self.runtime)

        self.assertIsInstance(raised.exception.__cause__, requests.exceptions.ConnectionError)
        self.assertTrue(self.profile.exists(), "the daemon could not be asked, so nothing may be removed")
        network = self.client.networks.by_name[self.runtime.network_name]
        self.assertNotIn("controller-id", network.attrs["Containers"], "the controller stayed on the session network")

    async def test_a_failed_removal_stays_the_failure_reported_when_the_network_release_fails_too(self) -> None:
        container = self.client.containers.browser_containers[self.runtime.container_name]

        def refuse(force: bool = False) -> None:
            raise requests.exceptions.ConnectionError("Connection aborted while removing")

        container.remove = refuse
        self.daemon_down(self.client.networks)

        with self.assertLogs("app.session_isolation", level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "could not be removed") as raised:
                await self.provisioner.release(self.runtime)

        self.assertIn("Connection aborted while removing", str(raised.exception.__cause__))
        self.assertIn("Error while fetching server API version", "\n".join(logs.output))  # the network's, logged
        self.assertTrue(self.profile.exists())

    async def test_a_failed_lookup_stays_the_failure_reported_when_the_network_release_fails_too(self) -> None:
        self.daemon_down(self.client.containers)
        self.daemon_down(self.client.networks)

        with self.assertRaisesRegex(RuntimeError, "could not be looked up") as raised:
            await self.provisioner.release(self.runtime)

        self.assertIsInstance(raised.exception.__cause__, requests.exceptions.ConnectionError)
        self.assertTrue(self.profile.exists())

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

    async def test_a_container_that_did_not_stop_is_removed_all_the_same(self) -> None:
        container = self.client.containers.browser_containers[self.runtime.container_name]

        def refuse(timeout: int = 5) -> None:
            raise docker.errors.APIError("cannot stop container: permission denied")

        container.stop = refuse

        await self.provisioner.release(self.runtime)

        self.assertTrue(container.removed)
        self.assertFalse(self.profile.exists())


class KeptContainerTests(unittest.IsolatedAsyncioTestCase):
    """R5C-4: with ISOLATED_BROWSER_KEEP_CONTAINERS nothing removes the container, so one that did not stop may still
    run: its release failed (close then reports a partial teardown), where it returned as if it had worked."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.provisioner, self.client, self.runtime = await _provisioned(
            self.tmp.name, "session-kept", ISOLATED_BROWSER_KEEP_CONTAINERS="true"
        )
        self.container = self.client.containers.browser_containers[self.runtime.container_name]
        self.network = self.client.networks.by_name[self.runtime.network_name]
        self.assertIn("controller-id", self.network.attrs["Containers"])

    async def test_a_kept_container_that_did_not_stop_fails_the_release(self) -> None:
        def refuse(timeout: int = 5) -> None:
            raise docker.errors.APIError("cannot stop container: permission denied")

        self.container.stop = refuse

        with self.assertLogs("app.session_isolation", level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, f"browser container {self.runtime.container_name} could not be"):
                await self.provisioner.release(self.runtime)

        self.assertIn("cannot stop container: permission denied", "\n".join(logs.output))
        self.assertFalse(self.container.removed)  # kept, as configured
        self.assertNotIn("controller-id", self.network.attrs["Containers"])  # the controller still left its network

    async def test_a_kept_container_that_stopped_is_released(self) -> None:
        await self.provisioner.release(self.runtime)

        self.assertTrue(self.container.stopped)
        self.assertFalse(self.container.removed)
        self.assertNotIn("controller-id", self.network.attrs["Containers"])

    async def test_a_kept_container_already_gone_is_released(self) -> None:
        def gone(timeout: int = 5) -> None:
            raise docker.errors.NotFound(self.runtime.container_name)

        self.container.stop = gone

        await self.provisioner.release(self.runtime)


if __name__ == "__main__":
    unittest.main()

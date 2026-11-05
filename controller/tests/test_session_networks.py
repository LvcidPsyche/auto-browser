"""Per-session Docker networks for docker_ephemeral sessions.

Every isolated browser shared one network (the compose network), so a page in
one session could reach another session's Playwright server on :9223 (its
/json takes over that browser), its noVNC on :6080, and the shared
browser-node. Each session now gets its own bridge network, which the
controller joins before the container starts and leaves at release.
ISOLATED_BROWSER_NETWORK_MODE=shared keeps the old single network.

All Docker-free: the fakes in test_session_isolation record every container and
network call in one ordered log (client.events).
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path, PureWindowsPath

from pydantic import ValidationError

from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner, _host_path
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
SESSION_LABELS = {
    "auto-browser.managed": "true",
    "auto-browser.session_id": "s1",
    "auto-browser.mode": "docker_ephemeral",
}


class SessionNetworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.data_root = Path(self._tempdir.name) / "data"
        self.data_root.mkdir(parents=True)

    def provisioner(self, **extra) -> tuple[DockerBrowserNodeProvisioner, FakeDockerClient]:
        settings = Settings(
            _env_file=None,
            ARTIFACT_ROOT=str(self.data_root / "artifacts"),
            SESSION_ISOLATION_MODE="docker_ephemeral",
            ISOLATED_HOST_DATA_ROOT=str(self.data_root),
            **extra,
        )
        controller = FakeDockerContainer(
            container_id="controller-id",
            name="controller",
            attrs={"Mounts": [], "NetworkSettings": {"Networks": {"auto-browser_default": {}}}},
        )
        client = FakeDockerClient(controller)
        provisioner = DockerBrowserNodeProvisioner(settings, client=client)
        provisioner._controller_container_id = controller.id
        return provisioner, client

    def assert_nothing_left(self, provisioner, client, session_id: str = "s1") -> None:
        self.assertEqual(client.networks.by_name, {})
        live = [c.name for c in client.containers.browser_containers.values() if not c.removed]
        self.assertEqual(live, [])
        self.assertFalse(provisioner._local_runtime_root(session_id).exists())

    # --- provision -------------------------------------------------------

    async def test_two_sessions_get_distinct_networks(self) -> None:
        provisioner, client = self.provisioner()

        a = await provisioner.provision("a")
        b = await provisioner.provision("b")

        self.assertEqual(a.network_name, "browser-session-a-net")
        self.assertEqual(b.network_name, "browser-session-b-net")
        net_a = client.networks.by_name[a.network_name].attrs["Containers"]
        net_b = client.networks.by_name[b.network_name].attrs["Containers"]
        # Each holds the controller and its own browser, never the other's.
        self.assertEqual(set(net_a), {"controller-id", "browser-session-a-id"})
        self.assertEqual(set(net_b), {"controller-id", "browser-session-b-id"})

    async def test_a_stale_network_of_the_same_session_is_replaced(self) -> None:
        """A leaked network from an earlier run of this id (still holding the
        controller) would make networks.create refuse the duplicate name."""
        provisioner, client = self.provisioner()
        stale = client.networks.add("browser-session-s1-net", SESSION_LABELS)
        stale.attrs["Containers"]["controller-id"] = {}

        runtime = await provisioner.provision("s1")

        self.assertTrue(stale.removed)
        self.assertIsNot(client.networks.by_name[runtime.network_name], stale)
        self.assertEqual(
            client.events[:3],
            [
                ("network.disconnect", "browser-session-s1-net", "controller-id", True),
                ("network.remove", "browser-session-s1-net"),
                ("network.create", "browser-session-s1-net"),
            ],
        )

    async def test_a_foreign_network_with_the_session_name_is_left_alone(self) -> None:
        provisioner, client = self.provisioner()
        foreign = client.networks.add("browser-session-s1-net", {"com.example.owner": "someone"})

        with self.assertRaisesRegex(RuntimeError, "already exists") as raised:
            await provisioner.provision("s1")

        # A guided error, as pool exhaustion is: which network, why it is not
        # replaced, and what to do. The daemon's own text says only "already exists".
        message = str(raised.exception)
        self.assertIn("browser-session-s1-net", message)
        self.assertIn("not a session network of this controller", message)
        self.assertIn("docker network rm browser-session-s1-net", message)
        self.assertIn("ISOLATED_BROWSER_CONTAINER_PREFIX", message)
        self.assertIsInstance(raised.exception.__cause__, Exception)
        self.assertFalse(foreign.removed)
        self.assertIs(client.networks.by_name["browser-session-s1-net"], foreign)
        self.assertFalse(provisioner._local_runtime_root("s1").exists())

    async def test_isolated_browser_network_is_ignored_with_a_warning_in_per_session_mode(self) -> None:
        provisioner, client = self.provisioner(ISOLATED_BROWSER_NETWORK="auto-browser_default")

        with self.assertLogs("app.session_isolation", level=logging.WARNING) as logs:
            await provisioner.startup()
        runtime = await provisioner.provision("s1")

        self.assertIn("ISOLATED_BROWSER_NETWORK=auto-browser_default is ignored", "\n".join(logs.output))
        self.assertIn("ISOLATED_BROWSER_NETWORK_MODE=shared", "\n".join(logs.output))
        self.assertEqual(runtime.network_name, "browser-session-s1-net")
        self.assertEqual(
            client.containers.browser_containers["browser-session-s1"].run_kwargs["network"], runtime.network_name
        )

    async def test_a_missing_controller_container_fails_closed_and_creates_nothing(self) -> None:
        """A host-run controller, or an ISOLATED_DOCKER_HOST that is another
        daemon, cannot join session networks: refuse rather than start a
        browser the controller cannot reach."""
        for controller_id in (None, "not-on-this-daemon"):
            with self.subTest(controller_id=controller_id):
                provisioner, client = self.provisioner()
                provisioner._controller_container_id = controller_id

                with self.assertRaisesRegex(RuntimeError, "ISOLATED_BROWSER_NETWORK_MODE=shared"):
                    await provisioner.provision("s1")

                self.assertEqual(client.events, [])
                self.assert_nothing_left(provisioner, client)

    async def test_the_daemons_409_conflict_is_mapped_whatever_its_wording(self) -> None:
        """The real daemon answers 409 Conflict ("network with name ... already exists")."""
        provisioner, client = self.provisioner()

        class APIError(Exception):
            status_code = 409

        def conflict(name, **kwargs):
            raise APIError(
                f'409 Client Error for http+docker://localnpipe/v1.56/networks/create: Conflict ("{name} is taken")'
            )

        client.networks.create = conflict  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "not a session network of this controller"):
            await provisioner.provision("s1")

        self.assert_nothing_left(provisioner, client)

    async def test_address_pool_exhaustion_names_default_address_pools(self) -> None:
        provisioner, client = self.provisioner()

        def exhausted(name, **kwargs):
            raise RuntimeError(
                '500 Server Error: Internal Server Error ("could not find an available, non-overlapping IPv4 '
                'address pool among the defaults to assign to the network")'
            )

        client.networks.create = exhausted  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "default-address-pools") as raised:
            await provisioner.provision("s1")

        self.assertIn("daemon.json", str(raised.exception))
        self.assert_nothing_left(provisioner, client)

    # --- failure cleanup ---------------------------------------------------

    async def test_a_failed_network_create_leaves_nothing(self) -> None:
        provisioner, client = self.provisioner()

        def refuse(name, **kwargs):
            raise RuntimeError("daemon refused network create")

        client.networks.create = refuse  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "refused network create"):
            await provisioner.provision("s1")

        self.assert_nothing_left(provisioner, client)

    async def test_a_failed_controller_connect_leaves_nothing(self) -> None:
        provisioner, client = self.provisioner()
        client.api.connect_error = "endpoint join failed"

        with self.assertRaisesRegex(RuntimeError, "endpoint join failed"):
            await provisioner.provision("s1")

        self.assertNotIn(("container.run", "browser-session-s1", "browser-session-s1-net"), client.events)
        self.assert_nothing_left(provisioner, client)

    # --- the controller keeps its default gateway (and with it the published ports) ---------

    async def test_the_controller_joins_a_session_network_without_taking_the_gateway(self) -> None:
        """Docker moves a container's default gateway, and the ports published on it, to the network whose name
        sorts first; GwPriority -1 (API 1.48) keeps the controller's own networks (priority 0) as the gateway."""
        provisioner, client = self.provisioner()

        await provisioner.provision("s1")

        self.assertEqual(
            client.api.connect_bodies, [{"Container": "controller-id", "EndpointConfig": {"GwPriority": -1}}]
        )
        self.assertIn(("network.connect", "browser-session-s1-net", "controller-id"), client.events)

    async def test_an_old_daemon_warns_once_when_a_session_network_would_take_the_gateway(self) -> None:
        provisioner, client = self.provisioner(ISOLATED_BROWSER_CONTAINER_PREFIX="aaa-session")
        client.api_version = "1.47"

        with self.assertLogs("app.session_isolation", level=logging.WARNING) as logs:
            await provisioner.provision("a")
            await provisioner.provision("b")

        warnings = [line for line in logs.output if "GwPriority" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("aaa-session-a-net", warnings[0])
        self.assertIn("auto-browser_default", warnings[0])
        self.assertIn("Docker Engine 28", warnings[0])
        self.assertIn("ISOLATED_BROWSER_CONTAINER_PREFIX", warnings[0])
        self.assertEqual(len(client.api.connect_bodies), 2)

    async def test_an_old_daemon_still_warns_when_the_controller_is_on_a_leftover_session_network(self) -> None:
        # A restart-policy restart leaves the controller on the session networks it had joined, and the cached
        # container's attrs still list them: they sort lowest, but they are not the controller's own network.
        provisioner, client = self.provisioner(ISOLATED_BROWSER_CONTAINER_PREFIX="aaa-session")
        client.api_version = "1.47"
        networks = client.containers.get("controller-id").attrs["NetworkSettings"]["Networks"]
        networks["aaa-session-0000old-net"] = {}

        with self.assertLogs("app.session_isolation", level=logging.WARNING) as logs:
            await provisioner.provision("b")

        warnings = [line for line in logs.output if "GwPriority" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("auto-browser_default", warnings[0])
        self.assertNotIn("aaa-session-0000old-net", warnings[0])

    async def test_a_daemon_whose_version_cannot_be_read_still_gets_a_session_joined_with_the_priority(self) -> None:
        # The version only decides whether to warn: reading it must never fail the session.
        provisioner, client = self.provisioner()

        def unreadable() -> dict:
            raise RuntimeError("version endpoint unavailable")

        client.version = unreadable

        await provisioner.provision("s1")

        self.assertEqual(client.api.connect_bodies[-1]["EndpointConfig"], {"GwPriority": -1})

    async def test_an_old_daemon_does_not_warn_when_the_session_network_sorts_after_the_controllers(self) -> None:
        provisioner, client = self.provisioner()
        client.api_version = "1.47"

        with self.assertNoLogs("app.session_isolation", level=logging.WARNING):
            await provisioner.provision("s1")

    async def test_a_current_daemon_does_not_warn_whatever_the_names(self) -> None:
        provisioner, client = self.provisioner(ISOLATED_BROWSER_CONTAINER_PREFIX="aaa-session")

        with self.assertNoLogs("app.session_isolation", level=logging.WARNING):
            await provisioner.provision("s1")

    async def test_a_client_pinned_below_1_48_counts_as_an_old_daemon(self) -> None:
        provisioner, client = self.provisioner(ISOLATED_BROWSER_CONTAINER_PREFIX="aaa-session")
        client.api._version = "1.43"

        with self.assertLogs("app.session_isolation", level=logging.WARNING) as logs:
            await provisioner.provision("s1")

        self.assertIn("GwPriority", "\n".join(logs.output))

    async def test_a_failed_container_start_leaves_nothing(self) -> None:
        """containers.run is create + start: a refused start leaves a created
        container on the network, and its endpoint blocks the network removal."""
        provisioner, client = self.provisioner()
        run = client.containers.run

        def created_but_not_started(image, **kwargs):
            run(image, **kwargs)
            raise RuntimeError("driver failed programming external connectivity")

        client.containers.run = created_but_not_started  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "external connectivity"):
            await provisioner.provision("s1")

        self.assertTrue(client.containers.browser_containers["browser-session-s1"].removed)
        self.assert_nothing_left(provisioner, client)

    async def test_an_endpoint_timeout_leaves_nothing(self) -> None:
        provisioner, client = self.provisioner(ISOLATED_BROWSER_WAIT_TIMEOUT_SECONDS="1")
        run = client.containers.run

        def never_ready(image, **kwargs):
            container = run(image, **kwargs)
            (Path(next(iter(kwargs["volumes"]))) / "browser-ws-endpoint.txt").unlink()
            return container

        client.containers.run = never_ready  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "Timed out waiting"):
            await provisioner.provision("s1")

        self.assert_nothing_left(provisioner, client)

    # --- release -----------------------------------------------------------

    async def test_keep_containers_keeps_the_network_but_the_controller_leaves_it(self) -> None:
        provisioner, client = self.provisioner(ISOLATED_BROWSER_KEEP_CONTAINERS="true")
        runtime = await provisioner.provision("s1")

        await provisioner.release(runtime)

        network = client.networks.by_name[runtime.network_name]
        self.assertFalse(network.removed)
        self.assertEqual(set(network.attrs["Containers"]), {"browser-session-s1-id"})

    async def test_release_never_removes_a_network_without_this_sessions_labels(self) -> None:
        provisioner, client = self.provisioner()
        runtime = await provisioner.provision("s1")
        client.networks.by_name[runtime.network_name].attrs["Labels"]["auto-browser.session_id"] = "someone-else"

        with self.assertLogs("app.session_isolation", level=logging.WARNING):
            await provisioner.release(runtime)

        self.assertIn(runtime.network_name, client.networks.by_name)
        self.assertNotIn("controller-id", client.networks.by_name[runtime.network_name].attrs["Containers"])

    async def test_a_browser_container_that_cannot_be_removed_does_not_leak_its_network(self) -> None:
        """A failed container.remove left the container's endpoint on the network,
        so the network removal failed too and the network (and its address-pool
        slot, about 30 per daemon) leaked until the next controller start."""
        provisioner, client = self.provisioner()
        runtime = await provisioner.provision("s1")
        container = client.containers.browser_containers[runtime.container_name]

        def refuse(force: bool = False) -> None:
            client.events.append(("container.remove refused", container.name))
            raise RuntimeError("removal of container browser-session-s1 is already in progress")

        container.remove = refuse  # type: ignore[method-assign]
        client.events.clear()

        with self.assertLogs("app.session_isolation", level=logging.WARNING):
            await provisioner.release(runtime)

        self.assertNotIn(runtime.network_name, client.networks.by_name)
        self.assertEqual(
            [event for event in client.events if event[0] != "cleanup.run"],  # the network calls only
            [
                ("container.stop", "browser-session-s1"),
                ("container.remove refused", "browser-session-s1"),
                ("network.disconnect", runtime.network_name, "browser-session-s1-id", True),
                ("network.disconnect", runtime.network_name, "controller-id", True),
                ("network.remove", runtime.network_name),
            ],
        )

    async def test_a_removed_container_is_not_disconnected_again(self) -> None:
        provisioner, client = self.provisioner()
        runtime = await provisioner.provision("s1")
        client.events.clear()

        await provisioner.release(runtime)

        disconnected = [event[2] for event in client.events if event[0] == "network.disconnect"]
        self.assertEqual(disconnected, ["controller-id"])

    async def test_a_stranded_container_is_never_pulled_off_a_network_without_this_sessions_labels(self) -> None:
        provisioner, client = self.provisioner()
        runtime = await provisioner.provision("s1")
        container = client.containers.browser_containers[runtime.container_name]

        def refuse(force: bool = False) -> None:
            raise RuntimeError("container is busy")

        container.remove = refuse  # type: ignore[method-assign]
        client.networks.by_name[runtime.network_name].attrs["Labels"]["auto-browser.session_id"] = "someone-else"

        with self.assertLogs("app.session_isolation", level=logging.WARNING):
            await provisioner.release(runtime)

        network = client.networks.by_name[runtime.network_name]
        self.assertIn(container.id, network.attrs["Containers"])

    async def test_release_carries_on_when_the_network_is_already_gone(self) -> None:
        provisioner, client = self.provisioner()
        runtime = await provisioner.provision("s1")
        del client.networks.by_name[runtime.network_name]

        await provisioner.release(runtime)

        self.assertTrue(client.containers.browser_containers["browser-session-s1"].removed)
        self.assertFalse(provisioner._local_runtime_root("s1").exists())

    # --- shared mode ---------------------------------------------------------

    async def test_shared_mode_never_disconnects_or_removes_the_shared_network(self) -> None:
        """The key regression test: a release must not tear down the compose
        network every other service is on."""
        provisioner, client = self.provisioner(
            ISOLATED_BROWSER_NETWORK_MODE="shared", ISOLATED_BROWSER_NETWORK="auto-browser_default"
        )
        shared = client.networks.add("auto-browser_default", {"com.docker.compose.network": "default"})
        shared.attrs["Containers"]["controller-id"] = {}

        runtime = await provisioner.provision("s1")
        self.assertEqual(runtime.network_name, "auto-browser_default")
        self.assertFalse(runtime.network_owned)
        await provisioner.release(runtime)
        await provisioner.startup()

        self.assertFalse(shared.removed)
        self.assertIn("controller-id", shared.attrs["Containers"])
        network_calls = [event for event in client.events if event[0].startswith("network.")]
        self.assertEqual(network_calls, [])

    async def test_shared_mode_discovers_the_controllers_network_as_before(self) -> None:
        provisioner, client = self.provisioner(ISOLATED_BROWSER_NETWORK_MODE="shared")

        runtime = await provisioner.provision("s1")

        self.assertEqual(runtime.network_name, "auto-browser_default")
        self.assertEqual(client.networks.create_kwargs, {})

    # --- startup sweep ---------------------------------------------------------

    async def test_startup_sweep_removes_labeled_networks_only(self) -> None:
        """A controller restarted by its restart policy is still attached to
        its old session networks, so the sweep disconnects it first. A network
        with a stuck endpoint is kept (with a warning), not fatal."""
        provisioner, client = self.provisioner()
        leaked = client.networks.add("browser-session-old-net", {**SESSION_LABELS, "auto-browser.session_id": "old"})
        leaked.attrs["Containers"]["controller-id"] = {}
        stuck = client.networks.add("browser-session-stuck-net", {**SESSION_LABELS, "auto-browser.session_id": "stuck"})
        stuck.attrs["Containers"]["unremovable-container"] = {}
        unlabeled = client.networks.add("auto-browser_default")
        unlabeled.attrs["Containers"]["controller-id"] = {}

        with self.assertLogs("app.session_isolation", level=logging.WARNING) as logs:
            await provisioner.startup()

        self.assertTrue(leaked.removed)
        self.assertFalse(stuck.removed)
        self.assertIn("browser-session-stuck-net", "\n".join(logs.output))
        self.assertFalse(unlabeled.removed)
        self.assertIn("controller-id", unlabeled.attrs["Containers"])

    async def test_startup_sweep_leaves_a_managed_network_that_is_not_a_session_network(self) -> None:
        """Only a network with every label a session network carries is swept, as release checks."""
        provisioner, client = self.provisioner()
        foreign = client.networks.add("someone-else-net", {"auto-browser.managed": "true"})
        foreign.attrs["Containers"]["controller-id"] = {}

        await provisioner.startup()

        self.assertFalse(foreign.removed)
        self.assertIn("controller-id", foreign.attrs["Containers"])

    async def test_startup_sweep_runs_after_the_container_reap(self) -> None:
        provisioner, client = self.provisioner()
        provisioner_run = await provisioner.provision("orphan")
        client.events.clear()
        # A fresh controller process: nothing tracks the orphan any more.
        restarted = DockerBrowserNodeProvisioner(provisioner.settings, client=client)
        restarted._controller_container_id = "controller-id"

        await restarted.startup()

        self.assertEqual(
            [event for event in client.events if event[0] != "cleanup.run"],  # the network calls only
            [
                ("container.remove", "browser-session-orphan"),
                ("network.disconnect", provisioner_run.network_name, "controller-id", True),
                ("network.remove", provisioner_run.network_name),
            ],
        )

    async def test_startup_sweep_is_skipped_with_keep_containers(self) -> None:
        provisioner, client = self.provisioner(ISOLATED_BROWSER_KEEP_CONTAINERS="true")
        kept = client.networks.add("browser-session-kept-net", SESSION_LABELS)

        await provisioner.startup()

        self.assertFalse(kept.removed)


class NetworkModeSettingTests(unittest.TestCase):
    def test_the_default_is_per_session(self) -> None:
        self.assertEqual(Settings(_env_file=None).isolated_browser_network_mode, "per_session")

    def test_an_invalid_mode_fails_at_startup(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, ISOLATED_BROWSER_NETWORK_MODE="per-session")

    @unittest.skipUnless(ENV_EXAMPLE.is_file(), ".env.example is not in the controller image")
    def test_env_example_lists_the_mode(self) -> None:
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^ISOLATED_BROWSER_NETWORK_MODE=per_session$")


if __name__ == "__main__":
    unittest.main()


class HostDataRootTests(unittest.IsolatedAsyncioTestCase):
    """Docker Desktop on Windows reports the controller's /data source as
    C:\...\data. Resolved inside the Linux controller it became
    /app/C:\...\data and every session create failed ("too many colons")."""

    async def provision_with_data_source(self, source: str, **extra) -> dict:
        with tempfile.TemporaryDirectory() as tempdir:
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(Path(tempdir) / "data" / "artifacts"),
                ISOLATED_BROWSER_WAIT_TIMEOUT_SECONDS="1",
                **extra,
            )
            controller = FakeDockerContainer(
                container_id="controller-id",
                name="controller",
                attrs={"Mounts": [{"Destination": "/data", "Source": source}]},
            )
            client = FakeDockerClient(controller)
            provisioner = DockerBrowserNodeProvisioner(settings, client=client)
            provisioner._controller_container_id = controller.id
            run = client.containers.run

            def run_on_the_local_dirs(image, **kwargs):
                # The fake writes the endpoint file into the bind source, which
                # here names a host the test is not on: the daemon would bind
                # it to the local dir the controller watches. Record the real kwargs.
                local = provisioner._local_runtime_root("s1")
                container = run(
                    image,
                    **{
                        **kwargs,
                        "volumes": {str(local / "profile"): {"bind": "/data/profile", "mode": "rw"}},
                    },
                )
                container.run_kwargs = kwargs
                return container

            client.containers.run = run_on_the_local_dirs  # type: ignore[method-assign]
            runtime = await provisioner.provision("s1")
            self.assertTrue(runtime.ws_endpoint_file.exists())
            return client.containers.browser_containers[runtime.container_name].run_kwargs["volumes"]

    async def test_a_windows_data_source_is_kept_verbatim(self) -> None:
        volumes = await self.provision_with_data_source(r"C:\Users\x\auto-browser\data")

        self.assertEqual(
            volumes,
            {
                r"C:\Users\x\auto-browser\data\browser-sessions\s1\profile": {"bind": "/data/profile", "mode": "rw"},
                r"C:\Users\x\auto-browser\data\browser-sessions\s1\downloads": {
                    "bind": "/data/downloads",
                    "mode": "rw",
                },
            },
        )

    async def test_a_unc_data_source_is_kept_verbatim(self) -> None:
        unc = "\\\\fileserver\\share\\data"  # \\fileserver\share\data

        volumes = await self.provision_with_data_source(unc)

        self.assertIn(unc + "\\browser-sessions\\s1\\profile", volumes)

    async def test_a_windows_isolated_host_data_root_is_kept_verbatim(self) -> None:
        volumes = await self.provision_with_data_source("/ignored", ISOLATED_HOST_DATA_ROOT="D:/auto-browser/data")

        self.assertIn(r"D:\auto-browser\data\browser-sessions\s1\profile", volumes)

    def test_a_posix_data_source_is_resolved_as_before(self) -> None:
        # Checked on the helper: provisioning would mkdir the host path, and
        # on a Windows test host /srv/... resolves to C:\srv\....
        for source in ("/srv/auto-browser/data", "/home/u/auto-browser/./data"):
            self.assertEqual(_host_path(source), Path(source).resolve())
            self.assertIsInstance(_host_path(source), Path)
        self.assertIsInstance(_host_path(r"C:\data"), PureWindowsPath)
        self.assertNotIsInstance(_host_path(r"C:\data"), Path)

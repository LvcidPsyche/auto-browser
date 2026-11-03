"""Requests from a per-session network are refused (403) before auth.

The controller joins every docker_ephemeral session's network, so each
session's browser could reach the controller API at the controller's address
on that network. Browser containers never call the controller (browser-node
has no callback), so a request from a session network is a page or code in a
session trying the API: it is refused before the token check, the rate
limiter and every route. The provisioner tracks each live session network's
subnet (from the created network's IPAM config) and drops it once the
controller has left the network. Only the direct peer counts; a forwarded-for
header is never trusted. Shared mode tracks nothing: the shared network can
hold legitimate callers.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.middleware.http import install_controller_http_middleware
from app.session_isolation import DockerBrowserNodeProvisioner
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer

TOKEN = "t" * 40
SESSION_LABELS = {
    "auto-browser.managed": "true",
    "auto-browser.session_id": "old",
    "auto-browser.mode": "docker_ephemeral",
}


def _subnet_host(client: FakeDockerClient, network_name: str, last: int = 5) -> str:
    subnet = client.networks.by_name[network_name].attrs["IPAM"]["Config"][0]["Subnet"]
    return subnet.rsplit(".", 1)[0] + f".{last}"


class SessionSubnetTrackingTests(unittest.IsolatedAsyncioTestCase):
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
        controller = FakeDockerContainer(container_id="controller-id", name="controller", attrs={})
        client = FakeDockerClient(controller)
        provisioner = DockerBrowserNodeProvisioner(settings, client=client)
        provisioner._controller_container_id = controller.id
        return provisioner, client

    async def test_a_live_session_networks_peers_are_refused_until_release(self) -> None:
        provisioner, client = self.provisioner()
        runtime = await provisioner.provision("s1")
        peer = _subnet_host(client, runtime.network_name)

        self.assertTrue(provisioner.peer_is_in_session_network(peer))
        self.assertTrue(provisioner.peer_is_in_session_network(_subnet_host(client, runtime.network_name, 254)))

        await provisioner.release(runtime)

        self.assertFalse(provisioner.peer_is_in_session_network(peer))

    async def test_each_session_is_tracked_on_its_own(self) -> None:
        provisioner, client = self.provisioner()
        a = await provisioner.provision("a")
        b = await provisioner.provision("b")
        peer_a, peer_b = _subnet_host(client, a.network_name), _subnet_host(client, b.network_name)

        await provisioner.release(a)

        self.assertFalse(provisioner.peer_is_in_session_network(peer_a))
        self.assertTrue(provisioner.peer_is_in_session_network(peer_b))

    async def test_other_peers_are_never_refused(self) -> None:
        provisioner, _ = self.provisioner()
        await provisioner.provision("s1")

        for peer in ("127.0.0.1", "::1", "172.18.0.1", "192.168.65.254", "testclient", "", "not-an-ip"):
            with self.subTest(peer=peer):
                self.assertFalse(provisioner.peer_is_in_session_network(peer))

    async def test_an_ipv4_mapped_ipv6_peer_is_matched(self) -> None:
        provisioner, client = self.provisioner()
        runtime = await provisioner.provision("s1")

        self.assertTrue(provisioner.peer_is_in_session_network("::ffff:" + _subnet_host(client, runtime.network_name)))

    async def test_a_failed_provision_drops_its_subnet(self) -> None:
        provisioner, client = self.provisioner()
        created = []
        create = client.networks.create

        def remember(name, **kwargs):
            network = create(name, **kwargs)
            created.append(network.attrs["IPAM"]["Config"][0]["Subnet"])
            return network

        def refuse(image, **kwargs):
            raise RuntimeError("image not found")

        client.networks.create = remember  # type: ignore[method-assign]
        client.containers.run = refuse  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "image not found"):
            await provisioner.provision("s1")

        self.assertEqual(len(created), 1)
        self.assertFalse(provisioner.peer_is_in_session_network(created[0].rsplit(".", 1)[0] + ".5"))

    async def test_keep_containers_drops_the_subnet_once_the_controller_leaves(self) -> None:
        provisioner, client = self.provisioner(ISOLATED_BROWSER_KEEP_CONTAINERS="true")
        runtime = await provisioner.provision("s1")
        peer = _subnet_host(client, runtime.network_name)

        await provisioner.release(runtime)

        self.assertIn(runtime.network_name, client.networks.by_name)
        self.assertFalse(provisioner.peer_is_in_session_network(peer))

    async def test_a_network_the_controller_could_not_leave_stays_refused(self) -> None:
        provisioner, client = self.provisioner()
        runtime = await provisioner.provision("s1")
        network = client.networks.by_name[runtime.network_name]
        peer = _subnet_host(client, runtime.network_name)

        def refuse(*args, **kwargs):
            raise RuntimeError("daemon is busy")

        network.disconnect = refuse  # type: ignore[method-assign]
        network.remove = refuse  # type: ignore[method-assign]

        with self.assertLogs("app.session_isolation", level=logging.WARNING):
            await provisioner.release(runtime)

        self.assertTrue(provisioner.peer_is_in_session_network(peer))

    async def test_the_startup_sweep_refuses_a_leftover_it_cannot_clear(self) -> None:
        """A restarted controller is still attached to its old session networks;
        one it cannot leave keeps being refused, one it clears is forgotten."""
        provisioner, client = self.provisioner()
        stuck = client.networks.add("browser-session-old-net", SESSION_LABELS, subnet="172.31.7.0/24")
        stuck.attrs["Containers"]["controller-id"] = {}
        cleared = client.networks.add(
            "browser-session-gone-net", {**SESSION_LABELS, "auto-browser.session_id": "gone"}, subnet="172.31.8.0/24"
        )
        cleared.attrs["Containers"]["controller-id"] = {}

        def refuse(*args, **kwargs):
            raise RuntimeError("daemon is busy")

        stuck.disconnect = refuse  # type: ignore[method-assign]
        stuck.remove = refuse  # type: ignore[method-assign]

        with self.assertLogs("app.session_isolation", level=logging.WARNING):
            await provisioner.startup()

        self.assertTrue(cleared.removed)
        self.assertTrue(provisioner.peer_is_in_session_network("172.31.7.9"))
        self.assertFalse(provisioner.peer_is_in_session_network("172.31.8.9"))

    async def test_shared_mode_tracks_nothing(self) -> None:
        provisioner, client = self.provisioner(
            ISOLATED_BROWSER_NETWORK_MODE="shared", ISOLATED_BROWSER_NETWORK="auto-browser_default"
        )
        client.networks.add("auto-browser_default", {"com.docker.compose.network": "default"}, subnet="172.18.0.0/16")

        await provisioner.provision("s1")

        self.assertFalse(provisioner.peer_is_in_session_network("172.18.0.5"))

    async def test_a_network_without_an_ipam_subnet_refuses_the_session(self) -> None:
        # Fail closed: without its subnet the controller could not refuse that network's peers,
        # so the session is not started, and nothing is left on the daemon.
        provisioner, client = self.provisioner()
        create = client.networks.create
        created = []

        def without_ipam(name, **kwargs):
            network = create(name, **kwargs)
            network.attrs.pop("IPAM", None)
            created.append(network)
            return network

        client.networks.create = without_ipam  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "no subnet"):
            await provisioner.provision("s1")

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].removed)
        self.assertNotIn("container.run", [event[0] for event in client.events])

    async def test_an_ipv6_session_network_is_refused_by_address(self) -> None:
        provisioner, client = self.provisioner()
        network = client.networks.add("ipv6-net", SESSION_LABELS)
        network.attrs["IPAM"] = {"Driver": "default", "Config": [{"Subnet": "fd00:ab:cd::/64"}]}

        self.assertTrue(provisioner._track_session_subnets(network))

        self.assertTrue(provisioner.peer_is_in_session_network("fd00:ab:cd::5"))
        self.assertFalse(provisioner.peer_is_in_session_network("fd00:ab:ce::5"))
        self.assertFalse(provisioner.peer_is_in_session_network("10.0.0.5"))

    async def test_a_reused_subnet_replaces_a_stale_entry(self) -> None:
        # Docker reuses a subnet only once its old network is gone, so an entry left by a release
        # that could neither leave nor remove its network is stale once the range is handed out again.
        provisioner, client = self.provisioner()
        stale = client.networks.add("stale-net", SESSION_LABELS, subnet="10.0.0.0/24")
        provisioner._track_session_subnets(stale)
        fresh = client.networks.add("fresh-net", SESSION_LABELS, subnet="10.0.0.0/24")

        provisioner._track_session_subnets(fresh)

        self.assertNotIn("stale-net", provisioner._session_subnets)
        self.assertIn("fresh-net", provisioner._session_subnets)
        self.assertTrue(provisioner.peer_is_in_session_network("10.0.0.5"))


def _app(refuses_peer, **overrides) -> FastAPI:
    application = FastAPI()

    @application.get("/sessions")
    def sessions() -> dict:
        return {"ok": True}

    @application.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @application.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    settings = Settings(_env_file=None, API_BEARER_TOKEN=TOKEN, **overrides)
    install_controller_http_middleware(
        application,
        settings=settings,
        rate_limiter=None,
        metrics=SimpleNamespace(enabled=False),
        refuses_peer=refuses_peer,
    )
    return application


SESSION_PEER = "172.30.1.5"


def _refuses(host: str) -> bool:
    return host == SESSION_PEER


class SessionPeerMiddlewareTests(unittest.TestCase):
    def client(self, peer: str, **overrides) -> TestClient:
        return TestClient(_app(_refuses, **overrides), client=(peer, 40000))

    def test_a_session_network_peer_gets_403_before_auth(self) -> None:
        response = self.client(SESSION_PEER).get("/sessions", headers={"Authorization": f"Bearer {TOKEN}"})

        self.assertEqual(response.status_code, 403)
        self.assertIn("session network", response.json()["detail"])
        # The outer layers still apply to the refusal.
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")

    def test_it_is_refused_before_the_token_check(self) -> None:
        """No token: 403, not 401, so a session cannot probe the auth layer at all."""
        self.assertEqual(self.client(SESSION_PEER).get("/sessions").status_code, 403)

    def test_even_exempt_paths_are_refused(self) -> None:
        self.assertEqual(self.client(SESSION_PEER).get("/healthz").status_code, 403)

    def test_before_the_host_check(self) -> None:
        client = self.client(SESSION_PEER, CONTROLLER_ALLOWED_HOSTS="controller.example")
        self.assertEqual(client.get("/sessions", headers={"Host": "elsewhere.example"}).status_code, 403)

    def test_other_peers_are_untouched(self) -> None:
        client = self.client("172.18.0.1")
        self.assertEqual(client.get("/sessions").status_code, 401)
        self.assertEqual(client.get("/sessions", headers={"Authorization": f"Bearer {TOKEN}"}).status_code, 200)
        self.assertEqual(client.get("/healthz").status_code, 200)

    def test_forwarded_headers_are_never_trusted(self) -> None:
        spoofing_in = self.client("172.18.0.1").get(
            "/healthz", headers={"X-Forwarded-For": SESSION_PEER, "X-Real-IP": SESSION_PEER}
        )
        spoofing_out = self.client(SESSION_PEER).get(
            "/healthz", headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}
        )
        self.assertEqual(spoofing_in.status_code, 200)
        self.assertEqual(spoofing_out.status_code, 403)

    def test_a_websocket_from_a_session_network_is_refused(self) -> None:
        with self.assertRaises(WebSocketDisconnect) as closed:
            with self.client(SESSION_PEER).websocket_connect("/ws") as ws:
                ws.receive_text()
        self.assertEqual(closed.exception.code, 1008)

        with self.client("172.18.0.1").websocket_connect("/ws") as ws:
            self.assertEqual(ws.receive_text(), "hello")

    def test_without_a_refusal_hook_nothing_changes(self) -> None:
        client = TestClient(_app(None), client=(SESSION_PEER, 40000))
        self.assertEqual(client.get("/healthz").status_code, 200)


class MainWiringTests(unittest.TestCase):
    def test_the_controller_app_is_wired_to_the_provisioner(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("refuses_peer=manager.runtime_provisioner.peer_is_in_session_network", source)


if __name__ == "__main__":
    unittest.main()

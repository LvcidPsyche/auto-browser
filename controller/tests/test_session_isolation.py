from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner, IsolatedBrowserRuntime
from app.session_tunnel import IsolatedSessionTunnel


class FakeDockerContainer:
    def __init__(self, *, container_id: str, name: str, attrs: dict, status: str = "running") -> None:
        self.id = container_id
        self.name = name
        self.attrs = attrs
        self.status = status
        self.removed = False
        self.stopped = False

    def reload(self) -> None:
        return None

    def stop(self, timeout: int = 5) -> None:
        self.stopped = True
        self.status = "exited"

    def remove(self, force: bool = False) -> None:
        self.removed = True

    def logs(self, tail: int = 20) -> bytes:
        return b""


class FakeDockerContainers:
    def __init__(self, controller_container: FakeDockerContainer) -> None:
        self.controller_container = controller_container
        self.browser_containers: dict[str, FakeDockerContainer] = {}

    def get(self, identifier: str) -> FakeDockerContainer:
        if identifier in {self.controller_container.id, self.controller_container.name}:
            return self.controller_container
        for container in self.browser_containers.values():
            if identifier in {container.id, container.name}:
                return container
        raise KeyError(identifier)

    def run(self, image: str, **kwargs) -> FakeDockerContainer:
        name = kwargs["name"]
        volumes = kwargs["volumes"]
        profile_dir = Path(next(path for path, mount in volumes.items() if mount["bind"] == "/data/profile"))
        profile_dir.mkdir(parents=True, exist_ok=True)
        endpoint = f"ws://{name}:9223/playwright"
        (profile_dir / "browser-ws-endpoint.txt").write_text(endpoint, encoding="utf-8")
        container = FakeDockerContainer(
            container_id=f"{name}-id",
            name=name,
            attrs={
                "Config": {"Image": image},
                "NetworkSettings": {
                    "Ports": {
                        "6080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "16080"}],
                        "5900/tcp": [{"HostIp": "127.0.0.1", "HostPort": "15900"}],
                    }
                },
            },
        )
        self.browser_containers[name] = container
        return container


class FakeDockerClient:
    def __init__(self, controller_container: FakeDockerContainer) -> None:
        self.containers = FakeDockerContainers(controller_container)


class FakeTracing:
    async def start(self, screenshots: bool = True, snapshots: bool = True, sources: bool = False) -> None:
        return None

    async def stop(self, path: str | None = None) -> None:
        return None


class FakeContext:
    def __init__(self) -> None:
        self.tracing = FakeTracing()

    async def new_page(self):
        return FakePage()

    async def close(self) -> None:
        return None


class FakePage:
    def __init__(self, url: str = "https://example.com") -> None:
        self.url = url

    def set_default_timeout(self, timeout_ms: int) -> None:
        return None

    async def title(self) -> str:
        return "Example Domain"


class FakeTunnelProcess:
    def __init__(self, returncode: int | None = None, pid: int = 4321) -> None:
        self.returncode = returncode
        self.pid = pid


class FailingCloseContext(FakeContext):
    async def close(self) -> None:
        raise RuntimeError("context close failed")


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.closed = False

    async def new_context(self, **kwargs):
        return self.context

    async def close(self) -> None:
        self.closed = True


class DockerBrowserNodeProvisionerTests(unittest.IsolatedAsyncioTestCase):
    async def test_provisioner_discovers_mounts_and_network_and_releases_container(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_root = root / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(data_root / "artifacts"),
                UPLOAD_ROOT=str(data_root / "uploads"),
                AUTH_ROOT=str(data_root / "auth"),
                APPROVAL_ROOT=str(data_root / "approvals"),
                SESSION_STORE_ROOT=str(data_root / "sessions"),
                ISOLATED_TAKEOVER_HOST="127.0.0.1",
            )
            controller_container = FakeDockerContainer(
                container_id="controller-id",
                name="controller",
                attrs={
                    "Mounts": [{"Destination": "/data", "Source": str(data_root)}],
                    "NetworkSettings": {"Networks": {"auto-browser_default": {}}},
                },
            )
            client = FakeDockerClient(controller_container)
            provisioner = DockerBrowserNodeProvisioner(settings, client=client)
            provisioner._controller_container_id = controller_container.id

            runtime = await provisioner.provision("session-1")

            self.assertEqual(runtime.container_name, "browser-session-session-1")
            self.assertEqual(runtime.network_name, "auto-browser_default")
            self.assertEqual(runtime.ws_endpoint, "ws://browser-session-session-1:9223/playwright")
            self.assertEqual(runtime.takeover_url, "http://127.0.0.1:16080/vnc.html?autoconnect=true&resize=scale")
            self.assertTrue(runtime.ws_endpoint_file.exists())

            await provisioner.release(runtime)

            container = client.containers.browser_containers[runtime.container_name]
            self.assertTrue(container.stopped)
            self.assertTrue(container.removed)


class BrowserIsolationSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_isolated_session_summary_uses_session_takeover_url_and_runtime_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                UPLOAD_ROOT=str(root / "uploads"),
                AUTH_ROOT=str(root / "auth"),
                APPROVAL_ROOT=str(root / "approvals"),
                SESSION_STORE_ROOT=str(root / "sessions"),
                REMOTE_ACCESS_INFO_PATH=str(root / "tunnels/reverse-ssh.json"),
            )
            manager = BrowserManager(settings)
            runtime = IsolatedBrowserRuntime(
                session_id="session-1",
                container_id="container-1",
                container_name="browser-session-session-1",
                network_name="auto-browser_default",
                browser_node_name="browser-session-session-1",
                profile_dir=root / "browser-sessions" / "session-1" / "profile",
                downloads_dir=root / "browser-sessions" / "session-1" / "downloads",
                ws_endpoint_file=root / "browser-sessions" / "session-1" / "profile" / "browser-ws-endpoint.txt",
                ws_endpoint="ws://browser-session-session-1:9223/playwright",
                takeover_url="http://127.0.0.1:16080/vnc.html?autoconnect=true&resize=scale",
                novnc_port=16080,
                vnc_port=15900,
            )
            artifact_dir = Path(settings.artifact_root) / "session-1"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            session = BrowserSession(
                id="session-1",
                name="session-1",
                created_at=datetime.now(UTC),
                context=FakeContext(),  # type: ignore[arg-type]
                page=FakePage("https://example.com/isolated"),  # type: ignore[arg-type]
                artifact_dir=artifact_dir,
                auth_dir=Path(settings.auth_root) / "session-1",
                upload_dir=Path(settings.upload_root) / "session-1",
                takeover_url=runtime.takeover_url,
                trace_path=artifact_dir / "trace.zip",
                browser_node_name=runtime.browser_node_name,
                isolation_mode="docker_ephemeral",
                runtime=runtime,
                shared_takeover_surface=False,
                shared_browser_process=False,
            )

            summary = await manager._session_summary(session)

            self.assertEqual(summary["takeover_url"], runtime.takeover_url)
            self.assertEqual(summary["isolation"]["mode"], "docker_ephemeral")
            self.assertFalse(summary["isolation"]["shared_takeover_surface"])
            self.assertFalse(summary["isolation"]["shared_browser_process"])
            self.assertEqual(summary["remote_access"]["status"], "local_only")
            self.assertFalse(summary["remote_access"]["active"])
            self.assertTrue(summary["remote_access"]["local_only"])
            self.assertEqual(
                summary["isolation"]["runtime"]["container_name"],
                "browser-session-session-1",
            )

    async def test_isolated_session_remote_access_marks_public_host_as_active(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                UPLOAD_ROOT=str(root / "uploads"),
                AUTH_ROOT=str(root / "auth"),
                APPROVAL_ROOT=str(root / "approvals"),
                SESSION_STORE_ROOT=str(root / "sessions"),
                REMOTE_ACCESS_INFO_PATH=str(root / "tunnels/reverse-ssh.json"),
            )
            manager = BrowserManager(settings)
            runtime = IsolatedBrowserRuntime(
                session_id="session-2",
                container_id="container-2",
                container_name="browser-session-session-2",
                network_name="auto-browser_default",
                browser_node_name="browser-session-session-2",
                profile_dir=root / "browser-sessions" / "session-2" / "profile",
                downloads_dir=root / "browser-sessions" / "session-2" / "downloads",
                ws_endpoint_file=root / "browser-sessions" / "session-2" / "profile" / "browser-ws-endpoint.txt",
                ws_endpoint="ws://browser-session-session-2:9223/playwright",
                takeover_url="https://tailscale-box.example.ts.net:16081/vnc.html?autoconnect=true&resize=scale",
                novnc_port=16081,
                vnc_port=15901,
            )
            artifact_dir = Path(settings.artifact_root) / "session-2"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            session = BrowserSession(
                id="session-2",
                name="session-2",
                created_at=datetime.now(UTC),
                context=FakeContext(),  # type: ignore[arg-type]
                page=FakePage("https://example.com/public"),  # type: ignore[arg-type]
                artifact_dir=artifact_dir,
                auth_dir=Path(settings.auth_root) / "session-2",
                upload_dir=Path(settings.upload_root) / "session-2",
                takeover_url=runtime.takeover_url,
                trace_path=artifact_dir / "trace.zip",
                browser_node_name=runtime.browser_node_name,
                isolation_mode="docker_ephemeral",
                runtime=runtime,
                shared_takeover_surface=False,
                shared_browser_process=False,
            )

            manager.sessions[session.id] = session
            remote_access = manager.get_remote_access_info(session.id)

            self.assertTrue(remote_access["active"])
            self.assertEqual(remote_access["status"], "active")
            self.assertFalse(remote_access["local_only"])
            self.assertEqual(remote_access["takeover_url"], runtime.takeover_url)

    async def test_isolated_session_prefers_dynamic_session_tunnel_takeover_url(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                UPLOAD_ROOT=str(root / "uploads"),
                AUTH_ROOT=str(root / "auth"),
                APPROVAL_ROOT=str(root / "approvals"),
                SESSION_STORE_ROOT=str(root / "sessions"),
                REMOTE_ACCESS_INFO_PATH=str(root / "tunnels/reverse-ssh.json"),
                ISOLATED_TUNNEL_ENABLED="true",
                ISOLATED_TUNNEL_HOST="bastion.example.com",
                ISOLATED_TUNNEL_USER="tunnel",
                ISOLATED_TUNNEL_KEY_PATH=str(root / "ssh" / "id_ed25519"),
                ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=str(root / "ssh" / "known_hosts"),
            )
            ssh_root = root / "ssh"
            ssh_root.mkdir(parents=True, exist_ok=True)
            (ssh_root / "id_ed25519").write_text("dummy", encoding="utf-8")
            (ssh_root / "known_hosts").write_text("dummy", encoding="utf-8")
            manager = BrowserManager(settings)
            runtime = IsolatedBrowserRuntime(
                session_id="session-3",
                container_id="container-3",
                container_name="browser-session-session-3",
                network_name="auto-browser_default",
                browser_node_name="browser-session-session-3",
                profile_dir=root / "browser-sessions" / "session-3" / "profile",
                downloads_dir=root / "browser-sessions" / "session-3" / "downloads",
                ws_endpoint_file=root / "browser-sessions" / "session-3" / "profile" / "browser-ws-endpoint.txt",
                ws_endpoint="ws://browser-session-session-3:9223/playwright",
                takeover_url="http://127.0.0.1:16082/vnc.html?autoconnect=true&resize=scale",
                novnc_port=16082,
                vnc_port=15902,
            )
            tunnel = IsolatedSessionTunnel(
                session_id="session-3",
                remote_port=16181,
                local_host="host.docker.internal",
                local_port=16082,
                public_takeover_url="http://bastion.example.com:16181/vnc.html?autoconnect=true&resize=scale",
                info_path=root / "tunnels" / "sessions" / "session-3.json",
                log_path=root / "tunnels" / "sessions" / "session-3.log",
                ssh_host="bastion.example.com",
                ssh_port=22,
                ssh_user="tunnel",
                access_mode="private",
                remote_bind_address="127.0.0.1",
                info_interval_seconds=10.0,
                process=FakeTunnelProcess(),
                status="active",
            )
            artifact_dir = Path(settings.artifact_root) / "session-3"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            session = BrowserSession(
                id="session-3",
                name="session-3",
                created_at=datetime.now(UTC),
                context=FakeContext(),  # type: ignore[arg-type]
                page=FakePage("https://example.com/tunnel"),  # type: ignore[arg-type]
                artifact_dir=artifact_dir,
                auth_dir=Path(settings.auth_root) / "session-3",
                upload_dir=Path(settings.upload_root) / "session-3",
                takeover_url=runtime.takeover_url,
                trace_path=artifact_dir / "trace.zip",
                browser_node_name=runtime.browser_node_name,
                isolation_mode="docker_ephemeral",
                runtime=runtime,
                tunnel=tunnel,
                shared_takeover_surface=False,
                shared_browser_process=False,
            )

            manager.sessions[session.id] = session
            remote_access = manager.get_remote_access_info(session.id)
            summary = await manager._session_summary(session)

            self.assertTrue(remote_access["active"])
            self.assertEqual(remote_access["status"], "active")
            self.assertEqual(
                remote_access["takeover_url"],
                "http://bastion.example.com:16181/vnc.html?autoconnect=true&resize=scale",
            )
            self.assertFalse(remote_access["requires_direct_host_access"])
            self.assertEqual(summary["takeover_url"], remote_access["takeover_url"])

    async def test_isolated_session_tunnel_targets_runtime_container_network(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                UPLOAD_ROOT=str(root / "uploads"),
                AUTH_ROOT=str(root / "auth"),
                APPROVAL_ROOT=str(root / "approvals"),
                SESSION_STORE_ROOT=str(root / "sessions"),
                ISOLATED_TUNNEL_ENABLED="true",
                ISOLATED_TUNNEL_HOST="bastion.example.com",
                ISOLATED_TUNNEL_USER="tunnel",
                ISOLATED_TUNNEL_KEY_PATH=str(root / "ssh" / "id_ed25519"),
                ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=str(root / "ssh" / "known_hosts"),
            )
            ssh_root = root / "ssh"
            ssh_root.mkdir(parents=True, exist_ok=True)
            (ssh_root / "id_ed25519").write_text("dummy", encoding="utf-8")
            (ssh_root / "known_hosts").write_text("dummy", encoding="utf-8")
            manager = BrowserManager(settings)
            runtime = IsolatedBrowserRuntime(
                session_id="session-4",
                container_id="container-4",
                container_name="browser-session-session-4",
                network_name="auto-browser_default",
                browser_node_name="browser-session-session-4",
                profile_dir=root / "browser-sessions" / "session-4" / "profile",
                downloads_dir=root / "browser-sessions" / "session-4" / "downloads",
                ws_endpoint_file=root / "browser-sessions" / "session-4" / "profile" / "browser-ws-endpoint.txt",
                ws_endpoint="ws://browser-session-session-4:9223/playwright",
                takeover_url="http://127.0.0.1:16083/vnc.html?autoconnect=true&resize=scale",
                novnc_port=16083,
                vnc_port=15903,
                tunnel_local_host="browser-session-session-4",
                tunnel_local_port=6080,
            )
            artifact_dir = Path(settings.artifact_root) / "session-4"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            session = BrowserSession(
                id="session-4",
                name="session-4",
                created_at=datetime.now(UTC),
                context=FakeContext(),  # type: ignore[arg-type]
                page=FakePage("https://example.com/runtime-network"),  # type: ignore[arg-type]
                artifact_dir=artifact_dir,
                auth_dir=Path(settings.auth_root) / "session-4",
                upload_dir=Path(settings.upload_root) / "session-4",
                takeover_url=runtime.takeover_url,
                trace_path=artifact_dir / "trace.zip",
                browser_node_name=runtime.browser_node_name,
                isolation_mode="docker_ephemeral",
                runtime=runtime,
                shared_takeover_surface=False,
                shared_browser_process=False,
            )

            class RecordingTunnelBroker:
                enabled = True

                def __init__(self) -> None:
                    self.calls: list[dict[str, object]] = []

                async def provision(self, session_id: str, *, local_host: str | None = None, local_port: int):
                    self.calls.append(
                        {
                            "session_id": session_id,
                            "local_host": local_host,
                            "local_port": local_port,
                        }
                    )
                    return None

            broker = RecordingTunnelBroker()
            manager.tunnel_broker = broker  # type: ignore[assignment]

            await manager._maybe_provision_session_tunnel(session)

            self.assertEqual(
                broker.calls,
                [
                    {
                        "session_id": "session-4",
                        "local_host": "browser-session-session-4",
                        "local_port": 6080,
                    }
                ],
            )


class BrowserSessionRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_session_rolls_back_runtime_even_if_context_close_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                UPLOAD_ROOT=str(root / "uploads"),
                AUTH_ROOT=str(root / "auth"),
                APPROVAL_ROOT=str(root / "approvals"),
                SESSION_STORE_ROOT=str(root / "sessions"),
            )
            manager = BrowserManager(settings)
            runtime = IsolatedBrowserRuntime(
                session_id="session-rollback",
                container_id="container-rollback",
                container_name="browser-session-session-rollback",
                network_name="auto-browser_default",
                browser_node_name="browser-session-session-rollback",
                profile_dir=root / "browser-sessions" / "session-rollback" / "profile",
                downloads_dir=root / "browser-sessions" / "session-rollback" / "downloads",
                ws_endpoint_file=root / "browser-sessions" / "session-rollback" / "profile" / "browser-ws-endpoint.txt",
                ws_endpoint="ws://browser-session-session-rollback:9223/playwright",
                takeover_url="http://127.0.0.1:16084/vnc.html?autoconnect=true&resize=scale",
                novnc_port=16084,
                vnc_port=15904,
                tunnel_local_host="browser-session-session-rollback",
                tunnel_local_port=6080,
            )
            browser = FakeBrowser(FailingCloseContext())
            tunnel = IsolatedSessionTunnel(
                session_id="session-rollback",
                remote_port=16181,
                local_host="browser-session-session-rollback",
                local_port=6080,
                public_takeover_url="http://bastion.example.com:16181/vnc.html?autoconnect=true&resize=scale",
                info_path=root / "tunnels" / "sessions" / "session-rollback.json",
                log_path=root / "tunnels" / "sessions" / "session-rollback.log",
                ssh_host="bastion.example.com",
                ssh_port=22,
                ssh_user="tunnel",
                access_mode="private",
                remote_bind_address="127.0.0.1",
                info_interval_seconds=10.0,
                process=FakeTunnelProcess(),
                status="active",
            )

            manager._acquire_session_browser = AsyncMock(return_value=(browser, runtime))  # type: ignore[method-assign]
            manager._attach_page_listeners = lambda page, session: None  # type: ignore[method-assign]

            async def fake_provision(session: BrowserSession) -> None:
                session.tunnel = tunnel

            manager._maybe_provision_session_tunnel = fake_provision  # type: ignore[method-assign]
            manager._persist_session = AsyncMock(side_effect=RuntimeError("persist failed"))  # type: ignore[method-assign]
            manager.tunnel_broker.release = AsyncMock()  # type: ignore[method-assign]
            manager.runtime_provisioner.release = AsyncMock()  # type: ignore[method-assign]

            with self.assertRaisesRegex(RuntimeError, "persist failed"):
                await manager.create_session(name="rollback-session")

            manager.tunnel_broker.release.assert_awaited_once_with(tunnel)
            manager.runtime_provisioner.release.assert_awaited_once_with(runtime)
            self.assertTrue(browser.closed)


if __name__ == "__main__":
    unittest.main()

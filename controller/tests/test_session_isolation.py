from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.browser_manager import BrowserManager, BrowserSession
from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner, IsolatedBrowserRuntime
from app.session_tunnel import IsolatedSessionTunnel
from app.utils import UTC
from tests._fake_cdp import FakeCDPSession, new_context_id, page_session_for


def _link_dir(link: Path, target: Path) -> None:
    """A directory symlink, or a junction where Windows refuses symlinks without privileges."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


class FakeDockerContainer:
    def __init__(
        self,
        *,
        container_id: str,
        name: str,
        attrs: dict,
        status: str = "running",
        labels: dict | None = None,
    ) -> None:
        self.id = container_id
        self.name = name
        self.attrs = attrs
        self.status = status
        self.labels = labels or {}
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
                    }
                },
            },
            labels=kwargs.get("labels") or {},
        )
        container.run_kwargs = kwargs
        self.browser_containers[name] = container
        return container

    def list(self, all: bool = False, filters: dict | None = None) -> list[FakeDockerContainer]:
        containers = list(self.browser_containers.values())
        label_filter = (filters or {}).get("label")
        if not label_filter:
            return containers
        key, _, value = label_filter.partition("=")
        return [c for c in containers if c.labels.get(key) == value]


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

    def set_default_timeout(self, timeout_ms: int) -> None:
        return None

    async def close(self) -> None:
        return None

    async def new_cdp_session(self, _page):
        # What the pre-request gate reads to own the context (app/browser/request_gate.py).
        return page_session_for(new_context_id())


class FakePage:
    def __init__(self, url: str = "https://example.com") -> None:
        self.url = url

    def set_default_timeout(self, timeout_ms: int) -> None:
        return None

    def on(self, event: str, handler) -> None:
        return None

    def remove_listener(self, event: str, handler) -> None:
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

    async def new_browser_cdp_session(self):
        return FakeCDPSession()

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
                AUDIT_ROOT=str(data_root / "audit"),
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

    async def test_provision_applies_default_resource_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(data_root / "artifacts"),
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

            runtime = await provisioner.provision("session-limits")

            kwargs = client.containers.browser_containers[runtime.container_name].run_kwargs
            self.assertEqual(kwargs["mem_limit"], "4g")
            self.assertEqual(kwargs["pids_limit"], 2048)
            self.assertNotIn("nano_cpus", kwargs)

    async def test_provision_applies_cpu_limit_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(data_root / "artifacts"),
                ISOLATED_BROWSER_CPUS="1.5",
                ISOLATED_BROWSER_MEM_LIMIT="2g",
                ISOLATED_BROWSER_PIDS_LIMIT="512",
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

            runtime = await provisioner.provision("session-cpus")

            kwargs = client.containers.browser_containers[runtime.container_name].run_kwargs
            self.assertEqual(kwargs["mem_limit"], "2g")
            self.assertEqual(kwargs["pids_limit"], 512)
            self.assertEqual(kwargs["nano_cpus"], 1_500_000_000)

    async def test_vnc_password_reaches_isolated_containers_only_when_set(self) -> None:
        for password in ("s3cret", None):
            with self.subTest(password=password), tempfile.TemporaryDirectory() as tempdir:
                data_root = Path(tempdir) / "data"
                data_root.mkdir(parents=True, exist_ok=True)
                overrides = {"VNC_PASSWORD": password} if password else {}
                settings = Settings(_env_file=None, ARTIFACT_ROOT=str(data_root / "artifacts"), **overrides)
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

                runtime = await provisioner.provision("session-vnc")

                environment = client.containers.browser_containers[runtime.container_name].run_kwargs["environment"]
                self.assertEqual(environment.get("VNC_PASSWORD"), password)
                self.assertNotIn("s3cret", repr(settings))
                self.assertEqual(environment["NOVNC_ALLOWED_HOSTS"], settings.isolated_takeover_host)

    async def test_startup_reaps_orphaned_containers(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(data_root / "artifacts"),
                SESSION_ISOLATION_MODE="docker_ephemeral",
            )
            controller_container = FakeDockerContainer(
                container_id="controller-id",
                name="controller",
                attrs={"Mounts": [], "NetworkSettings": {"Networks": {}}},
            )
            client = FakeDockerClient(controller_container)
            orphan = FakeDockerContainer(
                container_id="orphan-id",
                name="browser-session-stale",
                attrs={},
                labels={"auto-browser.managed": "true"},
            )
            unmanaged = FakeDockerContainer(
                container_id="other-id",
                name="someone-elses-container",
                attrs={},
            )
            client.containers.browser_containers[orphan.name] = orphan
            client.containers.browser_containers[unmanaged.name] = unmanaged
            provisioner = DockerBrowserNodeProvisioner(settings, client=client)

            await provisioner.startup()

            self.assertTrue(orphan.removed)
            self.assertFalse(unmanaged.removed)

    async def test_startup_keeps_orphans_when_keep_containers_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(data_root / "artifacts"),
                SESSION_ISOLATION_MODE="docker_ephemeral",
                ISOLATED_BROWSER_KEEP_CONTAINERS="true",
            )
            controller_container = FakeDockerContainer(
                container_id="controller-id",
                name="controller",
                attrs={"Mounts": [], "NetworkSettings": {"Networks": {}}},
            )
            client = FakeDockerClient(controller_container)
            orphan = FakeDockerContainer(
                container_id="orphan-id",
                name="browser-session-stale",
                attrs={},
                labels={"auto-browser.managed": "true"},
            )
            client.containers.browser_containers[orphan.name] = orphan
            provisioner = DockerBrowserNodeProvisioner(settings, client=client)

            await provisioner.startup()

            self.assertFalse(orphan.removed)

    def _provisioner_with_runtime_root(
        self, tempdir: str, **extra
    ) -> tuple[DockerBrowserNodeProvisioner, FakeDockerClient]:
        data_root = Path(tempdir) / "data"
        data_root.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            _env_file=None,
            ARTIFACT_ROOT=str(data_root / "artifacts"),
            SESSION_ISOLATION_MODE="docker_ephemeral",
            ISOLATED_HOST_DATA_ROOT=str(data_root),
            ISOLATED_BROWSER_NETWORK="auto-browser_default",
            **extra,
        )
        controller_container = FakeDockerContainer(
            container_id="controller-id",
            name="controller",
            attrs={"Mounts": [], "NetworkSettings": {"Networks": {}}},
        )
        client = FakeDockerClient(controller_container)
        return DockerBrowserNodeProvisioner(settings, client=client), client

    @staticmethod
    def _write_profile(provisioner: DockerBrowserNodeProvisioner, session_id: str) -> Path:
        runtime_root = provisioner._local_runtime_root(session_id)
        (runtime_root / "profile").mkdir(parents=True)
        (runtime_root / "profile" / "Cookies").write_text("session-cookie", encoding="utf-8")
        return runtime_root

    async def test_release_removes_the_profile_even_when_the_container_is_already_gone(self) -> None:
        """release() returned at once when docker no longer knew the container
        (removed by hand, a restarted daemon), leaving the Chromium profile and
        its cookies on disk."""
        with tempfile.TemporaryDirectory() as tempdir:
            provisioner, client = self._provisioner_with_runtime_root(tempdir)
            runtime_root = self._write_profile(provisioner, "gone")
            runtime = IsolatedBrowserRuntime(
                session_id="gone",
                container_id="gone-id",
                container_name="browser-session-gone",
                network_name="n",
                browser_node_name="browser-session-gone",
                profile_dir=runtime_root / "profile",
                downloads_dir=runtime_root / "downloads",
                ws_endpoint_file=runtime_root / "profile" / "ws.txt",
                ws_endpoint="",
                takeover_url="",
                novnc_port=None,
            )
            self.assertEqual(client.containers.browser_containers, {})

            await provisioner.release(runtime)

            self.assertFalse(runtime_root.exists())

    async def test_release_keeps_the_profile_when_keep_containers_is_on(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provisioner, _ = self._provisioner_with_runtime_root(tempdir, ISOLATED_BROWSER_KEEP_CONTAINERS="true")
            runtime_root = self._write_profile(provisioner, "kept")
            runtime = IsolatedBrowserRuntime(
                session_id="kept",
                container_id="x",
                container_name="x",
                network_name="n",
                browser_node_name="x",
                profile_dir=runtime_root / "profile",
                downloads_dir=runtime_root / "downloads",
                ws_endpoint_file=runtime_root / "ws.txt",
                ws_endpoint="",
                takeover_url="",
                novnc_port=None,
            )

            await provisioner.release(runtime)

            self.assertTrue(runtime_root.exists())

    async def test_startup_removes_leftover_profiles_but_only_inside_the_runtime_root(self) -> None:
        """Reaping an orphan removed its container and left the profile behind, and
        a profile whose container was removed by hand before the crash had nothing
        to reap at all."""
        with tempfile.TemporaryDirectory() as tempdir:
            provisioner, client = self._provisioner_with_runtime_root(tempdir)
            reaped_root = self._write_profile(provisioner, "orphan-1")
            containerless_root = self._write_profile(provisioner, "container-removed-by-hand")
            outside = Path(tempdir) / "data" / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("not a session dir", encoding="utf-8")
            name = "browser-session-orphan-1"
            client.containers.browser_containers[name] = FakeDockerContainer(
                container_id=f"{name}-id",
                name=name,
                attrs={},
                labels={"auto-browser.managed": "true", "auto-browser.session_id": "orphan-1"},
            )

            await provisioner.startup()

            self.assertFalse(reaped_root.exists())
            self.assertFalse(containerless_root.exists())
            self.assertTrue((outside / "keep.txt").exists())

    async def test_startup_sweep_does_not_follow_a_link_out_of_the_runtime_root(self) -> None:
        """The sweep removes every entry under the runtime root, so a symlink or
        junction planted there must not point it at a directory outside."""
        with tempfile.TemporaryDirectory() as tempdir:
            provisioner, _ = self._provisioner_with_runtime_root(tempdir)
            self._write_profile(provisioner, "orphan-1")
            outside = Path(tempdir) / "data" / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("not a session dir", encoding="utf-8")
            _link_dir(provisioner._local_runtime_root("") / "planted", outside)

            await provisioner.startup()

            self.assertTrue((outside / "keep.txt").exists())

    async def test_startup_keeps_leftover_profiles_when_keep_containers_is_on(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provisioner, _ = self._provisioner_with_runtime_root(tempdir, ISOLATED_BROWSER_KEEP_CONTAINERS="true")
            runtime_root = self._write_profile(provisioner, "kept")

            await provisioner.startup()

            self.assertTrue(runtime_root.exists())

    async def test_startup_reap_keeps_the_profile_when_the_container_could_not_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provisioner, client = self._provisioner_with_runtime_root(tempdir)
            runtime_root = self._write_profile(provisioner, "stuck")
            stuck = FakeDockerContainer(
                container_id="stuck-id",
                name="browser-session-stuck",
                attrs={},
                labels={"auto-browser.managed": "true", "auto-browser.session_id": "stuck"},
            )

            def refuse(force: bool = False) -> None:
                raise RuntimeError("container is busy")

            stuck.remove = refuse  # type: ignore[method-assign]
            client.containers.browser_containers[stuck.name] = stuck

            await provisioner.startup()

            self.assertTrue(runtime_root.exists())

    async def test_failed_provision_removes_the_directories_it_made(self) -> None:
        """If docker refuses to start the container there is nothing to release()
        later, so the profile directories made first would stay."""
        with tempfile.TemporaryDirectory() as tempdir:
            provisioner, client = self._provisioner_with_runtime_root(tempdir)

            def refuse(image: str, **kwargs):
                raise RuntimeError("image not found")

            client.containers.run = refuse  # type: ignore[method-assign]

            with self.assertRaisesRegex(RuntimeError, "image not found"):
                await provisioner.provision("never-started")

            self.assertFalse(provisioner._local_runtime_root("never-started").exists())


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
                AUDIT_ROOT=str(root / "audit"),
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
            # x11vnc has no TCP port (INFRA-01), so neither runtime view reports a raw VNC port.
            for view in (summary["isolation"]["runtime"], summary["remote_access"]["runtime"]):
                self.assertEqual(view["novnc_port"], 16080)
                self.assertNotIn("vnc_port", view)

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
                AUDIT_ROOT=str(root / "audit"),
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
                AUDIT_ROOT=str(root / "audit"),
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
                AUDIT_ROOT=str(root / "audit"),
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
                AUDIT_ROOT=str(root / "audit"),
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

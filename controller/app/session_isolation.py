from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pragma: no cover - optional dependency for local host-only unit runs
    import docker
except Exception:  # pragma: no cover - optional dependency for local host-only unit runs
    docker = None


@dataclass
class IsolatedBrowserRuntime:
    session_id: str
    container_id: str
    container_name: str
    network_name: str
    browser_node_name: str
    profile_dir: Path
    downloads_dir: Path
    ws_endpoint_file: Path
    ws_endpoint: str
    takeover_url: str
    novnc_port: int | None
    vnc_port: int | None
    tunnel_local_host: str = "host.docker.internal"
    tunnel_local_port: int = 6080


@dataclass
class PendingBrowserProvision:
    session_id: str
    container: Any
    container_name: str
    network_name: str
    browser_node_name: str
    local_profile_dir: Path
    local_downloads_dir: Path
    ws_endpoint_file: Path


class DockerBrowserNodeProvisioner:
    def __init__(self, settings, *, client: Any | None = None):
        self.settings = settings
        self._client = client
        self._host_data_root: Path | None = None
        self._network_name: str | None = None
        self._controller_container_id = os.environ.get("HOSTNAME")

    async def startup(self) -> None:
        if self.settings.session_isolation_mode != "docker_ephemeral":
            return
        await asyncio.to_thread(self._ensure_context)

    async def provision(self, session_id: str) -> IsolatedBrowserRuntime:
        pending = await asyncio.to_thread(self._provision_container_sync, session_id)
        try:
            ws_endpoint = await self._wait_for_ws_endpoint(pending.container, pending.ws_endpoint_file)
            await asyncio.to_thread(pending.container.reload)
            ports = pending.container.attrs.get("NetworkSettings", {}).get("Ports", {})
            novnc_port = self._extract_host_port(ports, "6080/tcp")
            vnc_port = self._extract_host_port(ports, "5900/tcp")
            takeover_url = self._build_takeover_url(novnc_port)
            return IsolatedBrowserRuntime(
                session_id=pending.session_id,
                container_id=str(pending.container.id),
                container_name=pending.container_name,
                network_name=pending.network_name,
                browser_node_name=pending.browser_node_name,
                profile_dir=pending.local_profile_dir,
                downloads_dir=pending.local_downloads_dir,
                ws_endpoint_file=pending.ws_endpoint_file,
                ws_endpoint=ws_endpoint,
                takeover_url=takeover_url,
                novnc_port=novnc_port,
                vnc_port=vnc_port,
                tunnel_local_host=pending.container_name,
                tunnel_local_port=6080,
            )
        except Exception:
            await asyncio.to_thread(self._cleanup_pending_container, pending)
            raise

    async def release(self, runtime: IsolatedBrowserRuntime) -> None:
        await asyncio.to_thread(self._release_sync, runtime)

    def _provision_container_sync(self, session_id: str) -> PendingBrowserProvision:
        client = self._ensure_context()
        host_data_root = self._host_data_root or self._discover_host_data_root(client)
        network_name = self._network_name or self._discover_network_name(client)
        self._host_data_root = host_data_root
        self._network_name = network_name

        local_runtime_root = self._local_runtime_root(session_id)
        local_profile_dir = local_runtime_root / "profile"
        local_downloads_dir = local_runtime_root / "downloads"
        host_runtime_root = host_data_root / "browser-sessions" / session_id
        host_profile_dir = host_runtime_root / "profile"
        host_downloads_dir = host_runtime_root / "downloads"

        local_profile_dir.mkdir(parents=True, exist_ok=True)
        local_downloads_dir.mkdir(parents=True, exist_ok=True)
        host_profile_dir.mkdir(parents=True, exist_ok=True)
        host_downloads_dir.mkdir(parents=True, exist_ok=True)

        container_name = f"{self.settings.isolated_browser_container_prefix}-{session_id}"
        existing = self._safe_get_container(client, container_name)
        if existing is not None:
            try:
                existing.remove(force=True)
            except Exception:
                pass

        container = client.containers.run(
            self.settings.isolated_browser_image,
            name=container_name,
            detach=True,
            network=network_name,
            shm_size="2g",
            environment={
                "BROWSER_WIDTH": str(self.settings.default_viewport_width),
                "BROWSER_HEIGHT": str(self.settings.default_viewport_height),
                "BROWSER_WS_ENDPOINT_FILE": "/data/profile/browser-ws-endpoint.txt",
                "PLAYWRIGHT_SERVER_HOST": "0.0.0.0",
                "PLAYWRIGHT_SERVER_PORT": "9223",
                "PLAYWRIGHT_SERVER_ADVERTISED_HOST": container_name,
            },
            volumes={
                str(host_profile_dir): {"bind": "/data/profile", "mode": "rw"},
                str(host_downloads_dir): {"bind": "/data/downloads", "mode": "rw"},
            },
            ports={
                "6080/tcp": (self.settings.isolated_browser_bind_host, None),
                "5900/tcp": (self.settings.isolated_browser_bind_host, None),
            },
            labels={
                "auto-browser.managed": "true",
                "auto-browser.session_id": session_id,
                "auto-browser.mode": "docker_ephemeral",
            },
        )

        ws_endpoint_file = local_profile_dir / "browser-ws-endpoint.txt"
        return PendingBrowserProvision(
            session_id=session_id,
            container_name=container_name,
            network_name=network_name,
            browser_node_name=container_name,
            local_profile_dir=local_profile_dir,
            local_downloads_dir=local_downloads_dir,
            ws_endpoint_file=ws_endpoint_file,
            container=container,
        )

    def _release_sync(self, runtime: IsolatedBrowserRuntime) -> None:
        client = self._ensure_context()
        container = self._safe_get_container(client, runtime.container_id) or self._safe_get_container(
            client, runtime.container_name
        )
        if container is None:
            return
        try:
            container.stop(timeout=5)
        except Exception:
            pass
        if not self.settings.isolated_browser_keep_containers:
            try:
                container.remove(force=True)
            except Exception:
                pass

    def _ensure_context(self):
        if self._client is None:
            if docker is None:
                raise RuntimeError(
                    "docker Python SDK is not installed. Install controller requirements or switch back to shared_browser_node."
                )
            if self.settings.isolated_docker_host:
                self._client = docker.DockerClient(base_url=self.settings.isolated_docker_host)
            else:
                self._client = docker.from_env()
        return self._client

    def _discover_host_data_root(self, client) -> Path:
        if self.settings.isolated_host_data_root:
            return Path(self.settings.isolated_host_data_root).resolve()
        container = self._get_controller_container(client)
        for mount in container.attrs.get("Mounts", []):
            if mount.get("Destination") == "/data" and mount.get("Source"):
                return Path(str(mount["Source"])).resolve()
        raise RuntimeError(
            "Could not discover the host path that backs /data for isolated browser containers. "
            "Set ISOLATED_HOST_DATA_ROOT explicitly."
        )

    def _discover_network_name(self, client) -> str:
        if self.settings.isolated_browser_network:
            return self.settings.isolated_browser_network
        container = self._get_controller_container(client)
        networks = list((container.attrs.get("NetworkSettings") or {}).get("Networks", {}).keys())
        preferred = [name for name in networks if name not in {"bridge", "host", "none"}]
        if preferred:
            return preferred[0]
        if networks:
            return networks[0]
        raise RuntimeError(
            "Could not discover a Docker network for isolated browser containers. "
            "Set ISOLATED_BROWSER_NETWORK explicitly."
        )

    def _get_controller_container(self, client):
        if not self._controller_container_id:
            raise RuntimeError(
                "Not running inside Docker and no controller container ID is available. "
                "Set ISOLATED_HOST_DATA_ROOT and ISOLATED_BROWSER_NETWORK explicitly."
            )
        return client.containers.get(self._controller_container_id)

    async def _wait_for_ws_endpoint(self, container, endpoint_file: Path) -> str:
        for _ in range(max(1, self.settings.isolated_browser_wait_timeout_seconds * 4)):
            await asyncio.to_thread(container.reload)
            status = str(getattr(container, "status", "") or container.attrs.get("State", {}).get("Status", ""))
            if status in {"exited", "dead"}:
                logs = self._container_logs(container)
                raise RuntimeError(
                    f"Isolated browser container {container.name} exited before Playwright endpoint was ready. {logs}"
                )
            if endpoint_file.exists():
                endpoint = endpoint_file.read_text(encoding="utf-8").strip()
                if endpoint:
                    return endpoint
            await asyncio.sleep(0.25)
        logs = self._container_logs(container)
        raise RuntimeError(
            f"Timed out waiting for isolated browser endpoint file {endpoint_file}. {logs}"
        )

    def _cleanup_pending_container(self, pending: PendingBrowserProvision) -> None:
        runtime = IsolatedBrowserRuntime(
            session_id=pending.session_id,
            container_id=str(pending.container.id),
            container_name=pending.container_name,
            network_name=pending.network_name,
            browser_node_name=pending.browser_node_name,
            profile_dir=pending.local_profile_dir,
            downloads_dir=pending.local_downloads_dir,
            ws_endpoint_file=pending.ws_endpoint_file,
            ws_endpoint="",
            takeover_url="",
            novnc_port=None,
            vnc_port=None,
            tunnel_local_host=pending.container_name,
            tunnel_local_port=6080,
        )
        self._release_sync(runtime)

    @staticmethod
    def _container_logs(container) -> str:
        try:
            output = container.logs(tail=20)
        except Exception:
            return "No container logs available."
        if isinstance(output, bytes):
            return output.decode("utf-8", errors="replace").strip() or "No container logs available."
        return str(output).strip() or "No container logs available."

    @staticmethod
    def _extract_host_port(ports: dict[str, Any], key: str) -> int | None:
        binding = (ports.get(key) or [None])[0]
        if not binding or not binding.get("HostPort"):
            return None
        return int(binding["HostPort"])

    def _build_takeover_url(self, novnc_port: int | None) -> str:
        if novnc_port is None:
            raise RuntimeError("Isolated browser container did not publish a noVNC port")
        return (
            f"{self.settings.isolated_takeover_scheme}://"
            f"{self.settings.isolated_takeover_host}:{novnc_port}"
            f"{self.settings.isolated_takeover_path}"
        )

    def _local_runtime_root(self, session_id: str) -> Path:
        return Path(self.settings.artifact_root).resolve().parent / "browser-sessions" / session_id

    @staticmethod
    def _safe_get_container(client, identifier: str):
        try:
            return client.containers.get(identifier)
        except Exception:
            return None

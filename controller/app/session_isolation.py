from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import string
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

try:  # pragma: no cover - optional dependency for local host-only unit runs
    import docker
except Exception:  # pragma: no cover - optional dependency for local host-only unit runs
    docker = None

logger = logging.getLogger(__name__)

_MANAGED_LABEL = "auto-browser.managed"
_SESSION_ID_LABEL = "auto-browser.session_id"
_MODE_LABEL = "auto-browser.mode"

# The capabilities browser-node's entrypoint needs as root before setpriv
# drops to the browser user (whose processes then hold none): CHOWN for the
# profile and runtime dirs, DAC_OVERRIDE to remove the endpoint file and write
# the VNC password in browser-owned dirs, KILL to stop node gracefully,
# SETGID/SETUID for setpriv. Everything else is dropped. docker-compose.yml's
# browser-node service lists the same set (test_container_hardening checks).
_BROWSER_NODE_CAP_ADD = ("CHOWN", "DAC_OVERRIDE", "KILL", "SETGID", "SETUID")
_NO_NEW_PRIVILEGES = "no-new-privileges:true"

# Each isolated session's noVNC gets its own random VNC password. Its 6080 is
# reachable from other sessions' containers (Docker routes to published ports
# across bridges), so one password shared by every container would let code in
# one session, which can read its own copy, open any other's desktop. VNC
# authentication uses only the first 8 characters: 62**8, about 47 bits.
_VNC_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_VNC_PASSWORD_LENGTH = 8


# A drive-letter (C:\ or C:/) or UNC (\\server\share) path: what Docker Desktop on
# Windows reports as a bind mount's Source.
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _host_path(value: str) -> PurePath:
    r"""A host path as the daemon will read it.

    Docker Desktop on Windows reports the controller's /data source as
    C:\...\data. Resolved inside the Linux controller it became
    /app/C:\...\data, which the daemon refuses ("too many colons"). Such a
    path is kept verbatim and joined with Windows separators; a POSIX path is
    resolved as before.
    """
    if _WINDOWS_PATH.match(value):
        return PureWindowsPath(value)
    return Path(value).resolve()


def _new_vnc_password() -> str:
    return "".join(secrets.choice(_VNC_PASSWORD_ALPHABET) for _ in range(_VNC_PASSWORD_LENGTH))


def takeover_url_with_password(url: str, password: str) -> str:
    """The takeover URL a human opens, with the noVNC password in the fragment.

    noVNC reads `password` from the fragment first (webutil.js getConfigVar),
    and a browser never sends the fragment to a server, so it stays out of
    access logs. The result is a secret: hand it only to the person taking over.
    """
    if not url or not password:
        return url
    separator = "&" if "#" in url else "#"
    return f"{url}{separator}password={password}"


# The daemon's error when its default address pools are used up. Docker's
# defaults give about 31 bridge networks, two of them already taken by the
# default bridge and the compose network.
_ADDRESS_POOL_EXHAUSTED = "could not find an available, non-overlapping ipv4 address pool"


def _session_labels(session_id: str) -> dict[str, str]:
    return {
        _MANAGED_LABEL: "true",
        _SESSION_ID_LABEL: session_id,
        _MODE_LABEL: "docker_ephemeral",
    }


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
    tunnel_local_host: str = "host.docker.internal"
    tunnel_local_port: int = 6080
    # True when network_name is this session's own network (per_session mode),
    # which release removes. A shared network is never removed.
    network_owned: bool = False
    # This session's noVNC password. Never logged, stored or put in takeover_url
    # itself: only the human-handoff responses carry it (takeover_url_with_password).
    vnc_password: str = field(default="", repr=False)


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
    network_owned: bool = False
    vnc_password: str = field(default="", repr=False)


class DockerBrowserNodeProvisioner:
    def __init__(self, settings, *, client: Any | None = None):
        self.settings = settings
        self._client = client
        self._host_data_root: PurePath | None = None
        # Shared mode only: the one network every browser container joins.
        self._network_name: str | None = None
        self._controller_container_id = os.environ.get("HOSTNAME")
        # The controller's own container, looked up once. per_session joins it
        # to each session network, and _discover_host_data_root reads its mounts.
        self._controller_container: Any | None = None

    @property
    def _per_session_networks(self) -> bool:
        return self.settings.isolated_browser_network_mode == "per_session"

    async def startup(self) -> None:
        if self.settings.session_isolation_mode != "docker_ephemeral":
            return
        if self._per_session_networks and self.settings.isolated_browser_network:
            logger.warning(
                "ISOLATED_BROWSER_NETWORK=%s is ignored: ISOLATED_BROWSER_NETWORK_MODE=per_session gives each "
                "session its own network. Set ISOLATED_BROWSER_NETWORK_MODE=shared to keep one shared network.",
                self.settings.isolated_browser_network,
            )
        await asyncio.to_thread(self._ensure_context)
        await asyncio.to_thread(self._reap_orphaned_containers)
        # After the container reap, so the orphans' endpoints are already gone.
        await asyncio.to_thread(self._reap_orphaned_networks)

    def _reap_orphaned_containers(self) -> None:
        """Remove session containers left behind by a previous controller run.

        Runs at startup before any session exists, so every container carrying
        the managed label is an orphan (a crashed or killed controller never got
        to release it). Assumes one controller per Docker host, matching the
        supported single-controller deployment.
        """
        if self.settings.isolated_browser_keep_containers:
            logger.debug("Skipping orphan container sweep: ISOLATED_BROWSER_KEEP_CONTAINERS is enabled")
            return
        client = self._ensure_context()
        try:
            orphans = client.containers.list(all=True, filters={"label": f"{_MANAGED_LABEL}=true"})
        except Exception as exc:
            logger.warning("Could not list orphaned browser containers: %s", exc)
            return
        still_running: set[str] = set()
        for container in orphans:
            name = getattr(container, "name", "<unknown>")
            try:
                container.remove(force=True)
            except Exception as exc:
                logger.warning("Failed to remove orphaned browser container %s: %s", name, exc)
                # It may still have its profile mounted: leave that directory.
                still_running.add((getattr(container, "labels", None) or {}).get(_SESSION_ID_LABEL, ""))
                continue
            logger.info("Reaped orphaned browser container %s", name)
        self._remove_orphaned_runtime_dirs(keep=still_running)

    def _remove_orphaned_runtime_dirs(self, *, keep: set[str]) -> None:
        """Delete every per-session profile left from a previous run.

        No session survives a controller restart, so each directory under the
        runtime root is an orphan, including the ones whose container was
        already removed by hand before the crash (nothing to reap, profile and
        cookies still on disk).
        """
        root = self._local_runtime_root("")
        try:
            leftovers = [entry.name for entry in root.iterdir() if entry.is_dir()]
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Could not list leftover session runtime dirs in %s: %s", root, exc)
            return
        for session_id in leftovers:
            if session_id not in keep:
                self._remove_runtime_dirs(session_id)

    def _reap_orphaned_networks(self) -> None:
        """Remove per-session networks left behind by a previous controller run.

        A controller restarted by its restart policy is the same container and
        is still attached to every session network it had joined (attachments
        survive a restart), so each network refuses removal until the controller
        disconnects itself. Runs after the container reap, so the orphaned
        browsers' endpoints are already gone. Inherits the one-controller-per-
        Docker-host assumption of _reap_orphaned_containers.
        """
        if self.settings.isolated_browser_keep_containers:
            logger.debug("Skipping orphan network sweep: ISOLATED_BROWSER_KEEP_CONTAINERS is enabled")
            return
        client = self._ensure_context()
        try:
            orphans = client.networks.list(filters={"label": f"{_MANAGED_LABEL}=true"})
        except Exception as exc:
            logger.warning("Could not list orphaned browser session networks: %s", exc)
            return
        controller_id = self._controller_id_or_none(client)
        for network in orphans:
            name = getattr(network, "name", "<unknown>")
            # Every label a session network carries, as release checks: a network with the
            # managed label alone is not one of ours to remove.
            labels = (getattr(network, "attrs", None) or {}).get("Labels") or {}
            if labels.get(_MODE_LABEL) != "docker_ephemeral" or not labels.get(_SESSION_ID_LABEL):
                continue
            if controller_id:
                try:
                    network.disconnect(controller_id, force=True)
                except Exception as exc:
                    # Usually "is not connected": nothing to undo.
                    logger.debug("Controller was not attached to orphaned network %s: %s", name, exc)
            try:
                network.remove()
            except Exception as exc:
                logger.warning("Failed to remove orphaned browser session network %s: %s", name, exc)
                continue
            logger.info("Reaped orphaned browser session network %s", name)

    async def provision(self, session_id: str) -> IsolatedBrowserRuntime:
        pending = await asyncio.to_thread(self._provision_container_sync, session_id)
        try:
            ws_endpoint = await self._wait_for_ws_endpoint(pending.container, pending.ws_endpoint_file)
            await asyncio.to_thread(pending.container.reload)
            ports = pending.container.attrs.get("NetworkSettings", {}).get("Ports", {})
            novnc_port = self._extract_host_port(ports, "6080/tcp")
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
                tunnel_local_host=pending.container_name,
                tunnel_local_port=6080,
                network_owned=pending.network_owned,
                vnc_password=pending.vnc_password,
            )
        except BaseException:
            # BaseException: a cancelled create (client gone, shutdown) left the
            # container running with nothing tracking it until the next restart.
            await asyncio.to_thread(self._cleanup_pending_container, pending)
            raise

    async def release(self, runtime: IsolatedBrowserRuntime) -> None:
        await asyncio.to_thread(self._release_sync, runtime)

    def _provision_container_sync(self, session_id: str) -> PendingBrowserProvision:
        client = self._ensure_context()
        host_data_root = self._host_data_root or self._discover_host_data_root(client)
        self._host_data_root = host_data_root
        container_name = self._container_name(session_id)
        if self._per_session_networks:
            network_name = self._session_network_name(container_name)
        else:
            network_name = self._network_name or self._discover_network_name(client)
            self._network_name = network_name

        local_runtime_root = self._local_runtime_root(session_id)
        local_profile_dir = local_runtime_root / "profile"
        local_downloads_dir = local_runtime_root / "downloads"
        host_runtime_root = host_data_root / "browser-sessions" / session_id
        host_profile_dir = host_runtime_root / "profile"
        host_downloads_dir = host_runtime_root / "downloads"

        local_profile_dir.mkdir(parents=True, exist_ok=True)
        local_downloads_dir.mkdir(parents=True, exist_ok=True)
        # The host path is usually the same directory as the local one (through
        # the /data mount). A Windows host path cannot be made from in here; the
        # local dirs above already are it.
        if isinstance(host_data_root, Path):
            host_profile_dir.mkdir(parents=True, exist_ok=True)
            host_downloads_dir.mkdir(parents=True, exist_ok=True)

        network = None
        controller_id: str | None = None
        try:
            if self._per_session_networks:
                # Fail closed before anything exists on the daemon: without the
                # controller on the session network it cannot reach the browser.
                controller_id = str(self._get_controller_container(client).id)
                self._remove_stale_container(client, container_name)
                self._remove_stale_session_network(client, network_name, session_id, controller_id)
                network = self._create_session_network(client, network_name, session_id)
                # Join before the container starts: the controller reaches it by
                # name (ws://<name>:9223, autossh to <name>:6080) through Docker
                # DNS on this network.
                network.connect(controller_id)
            return self._run_container_sync(
                client,
                session_id,
                network_name=network_name,
                network_owned=network is not None,
                host_profile_dir=host_profile_dir,
                host_downloads_dir=host_downloads_dir,
                local_profile_dir=local_profile_dir,
                local_downloads_dir=local_downloads_dir,
            )
        except BaseException:
            # No runtime exists to release, so release() will never run for
            # this id. containers.run is create + start: a failed start leaves a
            # created container behind, and it holds the network's endpoint.
            self._remove_stale_container(client, container_name)
            if network is not None:
                self._remove_session_network(network, session_id, controller_id)
            self._remove_runtime_dirs(session_id)
            raise

    def _container_name(self, session_id: str) -> str:
        return f"{self.settings.isolated_browser_container_prefix}-{session_id}"

    @staticmethod
    def _session_network_name(container_name: str) -> str:
        return f"{container_name}-net"

    def _remove_stale_container(self, client, container_name: str) -> None:
        existing = self._safe_get_container(client, container_name)
        if existing is None:
            return
        try:
            existing.remove(force=True)
        except Exception as exc:
            logger.debug("Could not remove stale container %s: %s", container_name, exc)

    def _create_session_network(self, client, network_name: str, session_id: str):
        try:
            # Not internal: the browser needs egress, and Docker does not publish
            # ports (noVNC 6080) from an internal network. No check_duplicate:
            # deprecated in API 1.44, and the daemon now always refuses a
            # duplicate name.
            return client.networks.create(
                network_name,
                driver="bridge",
                labels=_session_labels(session_id),
                internal=False,
            )
        except Exception as exc:
            if _ADDRESS_POOL_EXHAUSTED in str(exc).lower():
                raise RuntimeError(
                    f"Docker has no free address pool for the session network {network_name}: every isolated "
                    "session gets its own bridge network, and the daemon's default pools allow about 30. Close "
                    "sessions, remove leftover networks (label auto-browser.managed=true), or widen "
                    '"default-address-pools" in the daemon\'s daemon.json.'
                ) from exc
            raise

    def _remove_stale_session_network(self, client, network_name: str, session_id: str, controller_id: str) -> None:
        """Remove a same-name network a previous run of this session id left.

        Only one that carries this session's labels: a foreign network with the
        same name is left alone, and networks.create then refuses the duplicate.
        """
        network = self._safe_get_network(client, network_name)
        if network is None or not self._is_session_network(network, session_id):
            return
        self._remove_session_network(network, session_id, controller_id)

    def _remove_session_network(self, network, session_id: str, controller_id: str | None) -> None:
        """Disconnect the controller from this session's network, then remove it.

        Never raises: like the rest of release, every step logs and continues.
        """
        name = getattr(network, "name", "<unknown>")
        if controller_id:
            try:
                network.disconnect(controller_id, force=True)
            except Exception as exc:
                logger.warning("Could not disconnect the controller from session network %s: %s", name, exc)
        if not self._is_session_network(network, session_id):
            logger.warning("Not removing network %s: it does not carry session %s's labels", name, session_id)
            return
        try:
            network.remove()
        except Exception as exc:
            logger.warning("Could not remove session network %s: %s", name, exc)

    @staticmethod
    def _is_session_network(network, session_id: str) -> bool:
        labels = (getattr(network, "attrs", None) or {}).get("Labels") or {}
        return labels.get(_MANAGED_LABEL) == "true" and labels.get(_SESSION_ID_LABEL) == session_id

    def _run_container_sync(
        self,
        client,
        session_id: str,
        *,
        network_name: str,
        network_owned: bool,
        host_profile_dir: PurePath,
        host_downloads_dir: PurePath,
        local_profile_dir: Path,
        local_downloads_dir: Path,
    ) -> PendingBrowserProvision:
        container_name = self._container_name(session_id)
        self._remove_stale_container(client, container_name)

        resource_kwargs: dict[str, Any] = {}
        if self.settings.isolated_browser_mem_limit:
            resource_kwargs["mem_limit"] = self.settings.isolated_browser_mem_limit
        if self.settings.isolated_browser_pids_limit > 0:
            resource_kwargs["pids_limit"] = self.settings.isolated_browser_pids_limit
        if self.settings.isolated_browser_cpus > 0:
            resource_kwargs["nano_cpus"] = int(self.settings.isolated_browser_cpus * 1_000_000_000)

        environment = {
            "BROWSER_WIDTH": str(self.settings.default_viewport_width),
            "BROWSER_HEIGHT": str(self.settings.default_viewport_height),
            "BROWSER_WS_ENDPOINT_FILE": "/data/profile/browser-ws-endpoint.txt",
            "PLAYWRIGHT_SERVER_HOST": "0.0.0.0",
            "PLAYWRIGHT_SERVER_PORT": "9223",
            "PLAYWRIGHT_SERVER_ADVERTISED_HOST": container_name,
        }
        # Always a fresh password of its own, never the operator's VNC_PASSWORD:
        # the container can read whatever it is given, and that one guards the
        # shared browser-node.
        vnc_password = _new_vnc_password()
        environment["VNC_PASSWORD"] = vnc_password
        # noVNC in the container only accepts its own page on a loopback name or
        # a listed host (a DNS-rebinding guard), and the takeover URL for this
        # container uses ISOLATED_TAKEOVER_HOST.
        environment["NOVNC_ALLOWED_HOSTS"] = self.settings.isolated_takeover_host

        container = client.containers.run(
            self.settings.isolated_browser_image,
            name=container_name,
            detach=True,
            network=network_name,
            shm_size="2g",
            cap_drop=["ALL"],
            cap_add=list(_BROWSER_NODE_CAP_ADD),
            # setpriv drops privileges by syscall, so no-new-privileges (which
            # only blocks gains across execve) leaves it working.
            security_opt=[_NO_NEW_PRIVILEGES],
            environment=environment,
            volumes={
                str(host_profile_dir): {"bind": "/data/profile", "mode": "rw"},
                str(host_downloads_dir): {"bind": "/data/downloads", "mode": "rw"},
            },
            ports={
                "6080/tcp": (self.settings.isolated_browser_bind_host, None),
            },
            labels=_session_labels(session_id),
            **resource_kwargs,
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
            network_owned=network_owned,
            vnc_password=vnc_password,
        )

    def _release_sync(self, runtime: IsolatedBrowserRuntime) -> None:
        client = self._ensure_context()
        container = self._safe_get_container(client, runtime.container_id) or self._safe_get_container(
            client, runtime.container_name
        )
        if container is not None:
            try:
                container.stop(timeout=5)
            except Exception as exc:
                logger.debug("Could not stop browser container %s: %s", runtime.container_name, exc)
        stranded = None
        if not self.settings.isolated_browser_keep_containers:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception as exc:
                    logger.warning("Could not remove browser container %s: %s", runtime.container_name, exc)
                    # It still holds an endpoint on the session network.
                    stranded = container
        if runtime.network_owned:
            self._release_session_network(client, runtime, stranded=stranded)
        if not self.settings.isolated_browser_keep_containers:
            # Also when the container is already gone (docker rm, a crashed
            # daemon): its profile directory, cookies included, is still here.
            self._remove_runtime_dirs(runtime.session_id)

    def _release_session_network(self, client, runtime: IsolatedBrowserRuntime, *, stranded: Any = None) -> None:
        """Leave this session's network, and remove it unless containers are kept.

        The tunnel and the Playwright connection are already closed by now
        (sessions.py tears them down before the runtime). With keep_containers
        the network stays for the kept container, but the controller still
        leaves it, or it would stay attached to every session ever opened.

        `stranded` is the session's browser container when removing it failed:
        its endpoint would block the network removal, and the network (and its
        address-pool slot) would leak until the next startup sweep, so it is
        force-disconnected first.
        """
        network = self._safe_get_network(client, runtime.network_name)
        if network is None:
            return
        controller_id = self._controller_id_or_none(client)
        if self.settings.isolated_browser_keep_containers:
            if controller_id:
                try:
                    network.disconnect(controller_id, force=True)
                except Exception as exc:
                    logger.warning(
                        "Could not disconnect the controller from session network %s: %s", runtime.network_name, exc
                    )
            return
        if stranded is not None and self._is_session_network(network, runtime.session_id):
            try:
                network.disconnect(getattr(stranded, "id", stranded), force=True)
            except Exception as exc:
                logger.warning(
                    "Could not disconnect browser container %s from session network %s: %s",
                    runtime.container_name,
                    runtime.network_name,
                    exc,
                )
        self._remove_session_network(network, runtime.session_id, controller_id)

    def _remove_runtime_dirs(self, session_id: str) -> None:
        """Delete the per-session profile and downloads tree.

        Only the container was ever removed on release. Each isolated session
        leaves behind a full Chromium profile — tens to hundreds of megabytes —
        and nothing cleaned it: MaintenanceService sweeps only the artifact,
        upload and auth roots. On a long-lived host that is unbounded growth
        with no signal until the volume fills mid-session.
        """
        runtime_root = self._local_runtime_root(session_id)
        try:
            resolved = runtime_root.resolve()
            base = self._local_runtime_root("").resolve()
            # Never delete outside the managed runtime root, whatever a
            # malformed session id might resolve to.
            if base not in resolved.parents:
                logger.warning("Refusing to remove %s: outside the session runtime root", resolved)
                return
            shutil.rmtree(resolved, ignore_errors=True)
            logger.debug("Removed isolated session runtime dir %s", resolved)
        except Exception as exc:
            logger.warning("Could not remove runtime dir for session %s: %s", session_id, exc)

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

    def _discover_host_data_root(self, client) -> PurePath:
        if self.settings.isolated_host_data_root:
            return _host_path(self.settings.isolated_host_data_root)
        container = self._get_controller_container(client)
        for mount in container.attrs.get("Mounts", []):
            if mount.get("Destination") == "/data" and mount.get("Source"):
                return _host_path(str(mount["Source"]))
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
            "Could not discover a Docker network for isolated browser containers "
            "(ISOLATED_BROWSER_NETWORK_MODE=shared). Set ISOLATED_BROWSER_NETWORK explicitly."
        )

    def _get_controller_container(self, client):
        """The controller's own container on the isolation daemon (cached).

        Raises RuntimeError when there is none: a controller run on the host,
        or an ISOLATED_DOCKER_HOST that is a different daemon.
        """
        if self._controller_container is not None:
            return self._controller_container
        hint = (
            "Per-session networks (ISOLATED_BROWSER_NETWORK_MODE=per_session, the default) need the controller "
            "to run as a container on that Docker daemon, so it can join each session's network. Otherwise set "
            "ISOLATED_BROWSER_NETWORK_MODE=shared with ISOLATED_BROWSER_NETWORK, and ISOLATED_HOST_DATA_ROOT."
        )
        if not self._controller_container_id:
            raise RuntimeError(f"Not running inside Docker and no controller container ID is available. {hint}")
        try:
            container = client.containers.get(self._controller_container_id)
        except Exception as exc:
            raise RuntimeError(
                f"Could not find the controller container {self._controller_container_id!r} on the isolation "
                f"Docker daemon ({exc}). {hint}"
            ) from exc
        self._controller_container = container
        return container

    def _controller_id_or_none(self, client) -> str | None:
        """The controller's id for disconnects during cleanup, which never raise."""
        try:
            return str(self._get_controller_container(client).id)
        except Exception as exc:
            logger.debug("Controller container lookup failed during cleanup: %s", exc)
            return self._controller_container_id or None

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
        raise RuntimeError(f"Timed out waiting for isolated browser endpoint file {endpoint_file}. {logs}")

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
            tunnel_local_host=pending.container_name,
            tunnel_local_port=6080,
            network_owned=pending.network_owned,
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

    @staticmethod
    def _safe_get_network(client, identifier: str):
        try:
            return client.networks.get(identifier)
        except Exception:
            return None

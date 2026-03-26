from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .utils import utc_now


@dataclass
class IsolatedSessionTunnel:
    session_id: str
    remote_port: int
    local_host: str
    local_port: int
    public_takeover_url: str
    info_path: Path
    log_path: Path
    ssh_host: str
    ssh_port: int
    ssh_user: str
    access_mode: str
    remote_bind_address: str
    info_interval_seconds: float
    process: asyncio.subprocess.Process | None = None
    stderr_handle: BinaryIO | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    status: str = "starting"
    error: str | None = None
    released: bool = False
    last_updated: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.process is not None and self.process.returncode is None and self.status == "active")


class IsolatedSessionTunnelBroker:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(self.settings.isolated_tunnel_info_root)
        self._tunnels: dict[str, IsolatedSessionTunnel] = {}
        self._allocated_ports: set[int] = set()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.settings.isolated_tunnel_enabled

    async def startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._mark_existing_metadata_inactive)
        if self.enabled:
            self._validate_settings()

    async def shutdown(self) -> None:
        for tunnel in list(self._tunnels.values()):
            await self.release(tunnel)

    async def provision(
        self,
        session_id: str,
        *,
        local_port: int,
        local_host: str | None = None,
    ) -> IsolatedSessionTunnel | None:
        if not self.enabled:
            return None

        self._validate_settings()
        async with self._lock:
            existing = self._tunnels.get(session_id)
            if existing is not None:
                return existing

            last_error: str | None = None
            for remote_port in range(
                self.settings.isolated_tunnel_remote_port_start,
                self.settings.isolated_tunnel_remote_port_end + 1,
            ):
                if remote_port in self._allocated_ports:
                    continue
                tunnel = self._build_tunnel(
                    session_id,
                    remote_port=remote_port,
                    local_host=local_host or self.settings.isolated_tunnel_local_host,
                    local_port=local_port,
                )
                tunnel.stderr_handle = tunnel.log_path.open("ab", buffering=0)
                self._write_metadata(tunnel, status="starting")
                try:
                    tunnel.process = await asyncio.create_subprocess_exec(
                        "autossh",
                        *self._autossh_args(tunnel),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=tunnel.stderr_handle,
                    )
                except Exception as exc:
                    tunnel.error = str(exc)
                    self._write_metadata(tunnel, status="error")
                    tunnel.stderr_handle.close()
                    raise

                await asyncio.sleep(self.settings.isolated_tunnel_startup_grace_seconds)
                if tunnel.process.returncode is not None:
                    tunnel.error = self._tail_file(tunnel.log_path) or (
                        f"autossh exited early with code {tunnel.process.returncode}"
                    )
                    self._write_metadata(tunnel, status="error")
                    if tunnel.stderr_handle is not None:
                        tunnel.stderr_handle.close()
                        tunnel.stderr_handle = None
                    last_error = tunnel.error
                    continue

                self._allocated_ports.add(remote_port)
                self._tunnels[session_id] = tunnel
                self._write_metadata(tunnel, status="active")
                tunnel.heartbeat_task = asyncio.create_task(self._heartbeat(tunnel))
                return tunnel

            raise RuntimeError(
                last_error
                or (
                    "No isolated tunnel ports were available in the configured range "
                    f"{self.settings.isolated_tunnel_remote_port_start}-"
                    f"{self.settings.isolated_tunnel_remote_port_end}."
                )
            )

    async def release(self, tunnel: IsolatedSessionTunnel) -> None:
        async with self._lock:
            tunnel.released = True
            if tunnel.heartbeat_task is not None:
                tunnel.heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await tunnel.heartbeat_task
                tunnel.heartbeat_task = None

            process = tunnel.process
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

            tunnel.status = "inactive"
            tunnel.error = None
            self._write_metadata(tunnel, status="inactive")
            self._allocated_ports.discard(tunnel.remote_port)
            self._tunnels.pop(tunnel.session_id, None)
            if tunnel.stderr_handle is not None:
                tunnel.stderr_handle.close()
                tunnel.stderr_handle = None

    def describe(self, tunnel: IsolatedSessionTunnel | None) -> dict[str, Any] | None:
        if tunnel is None:
            return None
        self._sync_status_from_process(tunnel)
        return {
            "status": tunnel.status,
            "active": tunnel.active,
            "session_id": tunnel.session_id,
            "remote_port": tunnel.remote_port,
            "remote_bind_address": tunnel.remote_bind_address,
            "local_host": tunnel.local_host,
            "local_port": tunnel.local_port,
            "public_takeover_url": tunnel.public_takeover_url,
            "ssh_host": tunnel.ssh_host,
            "ssh_port": tunnel.ssh_port,
            "ssh_user": tunnel.ssh_user,
            "access_mode": tunnel.access_mode,
            "info_path": str(tunnel.info_path),
            "log_path": str(tunnel.log_path),
            "error": tunnel.error,
            "last_updated": tunnel.last_updated,
        }

    def _build_tunnel(
        self,
        session_id: str,
        *,
        remote_port: int,
        local_host: str,
        local_port: int,
    ) -> IsolatedSessionTunnel:
        public_host = self.settings.isolated_tunnel_public_host or self.settings.isolated_tunnel_host
        public_path = self.settings.isolated_takeover_path
        if not public_path.startswith("/"):
            public_path = f"/{public_path}"
        return IsolatedSessionTunnel(
            session_id=session_id,
            remote_port=remote_port,
            local_host=local_host,
            local_port=local_port,
            public_takeover_url=(
                f"{self.settings.isolated_tunnel_public_scheme}://{public_host}:{remote_port}{public_path}"
            ),
            info_path=self.root / f"{session_id}.json",
            log_path=self.root / f"{session_id}.log",
            ssh_host=self.settings.isolated_tunnel_host or "",
            ssh_port=self.settings.isolated_tunnel_port,
            ssh_user=self.settings.isolated_tunnel_user or "",
            access_mode=self.settings.isolated_tunnel_access_mode,
            remote_bind_address=self.settings.isolated_tunnel_remote_bind_address,
            info_interval_seconds=self.settings.isolated_tunnel_info_interval_seconds,
        )

    def _autossh_args(self, tunnel: IsolatedSessionTunnel) -> list[str]:
        return [
            "-M",
            "0",
            "-N",
            "-T",
            "-p",
            str(tunnel.ssh_port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            f"ServerAliveInterval={self.settings.isolated_tunnel_server_alive_interval}",
            "-o",
            f"ServerAliveCountMax={self.settings.isolated_tunnel_server_alive_count_max}",
            "-o",
            f"StrictHostKeyChecking={self.settings.isolated_tunnel_strict_host_key_checking}",
            "-o",
            f"UserKnownHostsFile={self.settings.isolated_tunnel_known_hosts_path}",
            "-i",
            self.settings.isolated_tunnel_key_path,
            "-R",
            (
                f"{tunnel.remote_bind_address}:{tunnel.remote_port}:"
                f"{tunnel.local_host}:{tunnel.local_port}"
            ),
            f"{tunnel.ssh_user}@{tunnel.ssh_host}",
        ]

    async def _heartbeat(self, tunnel: IsolatedSessionTunnel) -> None:
        try:
            while tunnel.process is not None and tunnel.process.returncode is None:
                await asyncio.sleep(tunnel.info_interval_seconds)
                if tunnel.process.returncode is None:
                    self._write_metadata(tunnel, status="active")
        except asyncio.CancelledError:
            raise
        finally:
            if not tunnel.released:
                self._sync_status_from_process(tunnel)

    def _sync_status_from_process(self, tunnel: IsolatedSessionTunnel) -> None:
        if tunnel.process is None or tunnel.process.returncode is None or tunnel.released:
            return
        if tunnel.status in {"inactive", "error", "degraded"}:
            return
        tunnel.error = self._tail_file(tunnel.log_path) or (
            f"autossh exited with code {tunnel.process.returncode}"
        )
        self._write_metadata(tunnel, status="degraded")

    def _write_metadata(self, tunnel: IsolatedSessionTunnel, *, status: str) -> None:
        tunnel.status = status
        tunnel.last_updated = utc_now()
        payload = {
            "status": status,
            "updated_at": tunnel.last_updated,
            "session_id": tunnel.session_id,
            "info_interval_seconds": tunnel.info_interval_seconds,
            "ssh_host": tunnel.ssh_host,
            "ssh_port": tunnel.ssh_port,
            "ssh_user": tunnel.ssh_user,
            "access_mode": tunnel.access_mode,
            "remote_bind_address": tunnel.remote_bind_address,
            "remote_port": tunnel.remote_port,
            "local_host": tunnel.local_host,
            "local_port": tunnel.local_port,
            "public_takeover_url": tunnel.public_takeover_url,
            "pid": tunnel.process.pid if tunnel.process is not None and tunnel.process.returncode is None else None,
            "info_path": str(tunnel.info_path),
            "log_path": str(tunnel.log_path),
            "error": tunnel.error,
        }
        tunnel.info_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _mark_existing_metadata_inactive(self) -> None:
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(payload.get("status") or "") not in {"starting", "active", "degraded"}:
                continue
            payload["status"] = "inactive"
            payload["updated_at"] = utc_now()
            payload["error"] = "controller restarted; session tunnel no longer active"
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _validate_settings(self) -> None:
        required = {
            "ISOLATED_TUNNEL_HOST": self.settings.isolated_tunnel_host,
            "ISOLATED_TUNNEL_USER": self.settings.isolated_tunnel_user,
            "ISOLATED_TUNNEL_KEY_PATH": self.settings.isolated_tunnel_key_path,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"isolated tunnel support is enabled but missing required settings: {', '.join(missing)}")

        key_path = Path(self.settings.isolated_tunnel_key_path)
        if not key_path.is_file():
            raise RuntimeError(f"isolated tunnel key is missing or unreadable: {key_path}")

        strict = self.settings.isolated_tunnel_strict_host_key_checking
        known_hosts = Path(self.settings.isolated_tunnel_known_hosts_path)
        if strict in {"yes", "accept-new"} and not known_hosts.is_file():
            raise RuntimeError(
                "isolated tunnel known_hosts file is required when strict host key checking is enabled: "
                f"{known_hosts}"
            )

        allowed_modes = {"private", "tailscale", "cloudflare-access", "unsafe-public"}
        if self.settings.isolated_tunnel_access_mode not in allowed_modes:
            raise RuntimeError(
                "invalid ISOLATED_TUNNEL_ACCESS_MODE="
                f"{self.settings.isolated_tunnel_access_mode}; expected one of: {', '.join(sorted(allowed_modes))}"
            )

        bind_address = self.settings.isolated_tunnel_remote_bind_address
        if bind_address not in {"127.0.0.1", "localhost", "::1"} and (
            self.settings.isolated_tunnel_access_mode != "unsafe-public"
        ):
            raise RuntimeError(
                "isolated session tunnels refuse non-local remote bind addresses unless "
                "ISOLATED_TUNNEL_ACCESS_MODE=unsafe-public."
            )

        if (
            self.settings.isolated_tunnel_access_mode == "cloudflare-access"
            and self.settings.isolated_tunnel_public_scheme != "https"
        ):
            raise RuntimeError("Cloudflare Access mode expects ISOLATED_TUNNEL_PUBLIC_SCHEME=https")

        if (
            self.settings.isolated_tunnel_remote_port_start
            > self.settings.isolated_tunnel_remote_port_end
        ):
            raise RuntimeError("ISOLATED_TUNNEL_REMOTE_PORT_START must be <= ISOLATED_TUNNEL_REMOTE_PORT_END")

    @staticmethod
    def _tail_file(path: Path, *, max_chars: int = 800) -> str | None:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return None
        return text[-max_chars:]


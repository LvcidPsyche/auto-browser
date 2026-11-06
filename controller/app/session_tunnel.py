from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .providers.base import allowlisted_environment
from .utils import utc_now


def _is_bare_host(value: str) -> bool:
    """A host name or IP address on its own: no surrounding space, brackets or port (an IPv6 literal has colons)."""
    if not value or value != value.strip() or any(char.isspace() or char in "[]/@" for char in value):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return ":" not in value  # a name with a colon is host:port


def _bare_bind_address(value: str) -> str | None:
    """The tunnel's remote bind address as a bare host or IP, or None when it is not one.

    Any bare host in brackets with no port (`[::1]`, `[127.0.0.1]`, `[localhost]`: ssh parses each) is unbracketed
    and then judged as its bare spelling (W2-5); a port, spaces and an empty value are refused."""
    if len(value) > 2 and value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value if _is_bare_host(value) else None


def _ssh_address(value: str) -> str:
    """An address as it goes into an ssh forwarding spec: an IPv6 literal in brackets (its colons would otherwise
    split the spec), anything else as it is."""
    try:
        return f"[{value}]" if ipaddress.ip_address(value).version == 6 else value
    except ValueError:
        return value


def _is_bare_loopback(value: str) -> bool:
    """Strictly loopback, for the tunnel's remote bind address: `localhost`, or an address ipaddress reads as
    loopback. is_loopback_host stays lenient (host:port, brackets) for its other callers."""
    if not _is_bare_host(value):
        return False
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


logger = logging.getLogger(__name__)

# What autossh prints when the remote port it was asked to bind is taken (ExitOnForwardFailure=yes): the only early
# exits that another port can cure. Anything else (a refused key, an unreachable bastion) fails the next port too.
_REMOTE_PORT_UNAVAILABLE = ("remote port forwarding failed", "address already in use")
# The error session users see when the cause is autossh's own output (which can name the bastion, its user, key paths).
TUNNEL_EXITED_CODE = "autossh_exited"
# How long a release waits for autossh to exit after terminate(), and again after kill().
_PROCESS_EXIT_WAIT_SECONDS = 5.0
# Errors set by the broker itself: short codes, safe to show as they are.
_TUNNEL_ERROR_CODES = frozenset({"autossh_start_failed", "provision_cancelled", "provision_failed"})


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
        last_error: str | None = None
        for remote_port in range(
            self.settings.isolated_tunnel_remote_port_start,
            self.settings.isolated_tunnel_remote_port_end + 1,
        ):
            # The lock covers choosing a port and starting autossh, not the
            # startup grace period: held across that sleep, concurrent
            # provisions queued behind each other one grace period apiece.
            async with self._lock:
                # Also a tunnel still starting: create_session provisions once per
                # fresh session id, so a duplicate call is not a real path.
                existing = self._tunnels.get(session_id)
                if existing is not None:
                    return existing
                if remote_port in self._allocated_ports:
                    continue
                tunnel = self._build_tunnel(
                    session_id,
                    remote_port=remote_port,
                    local_host=local_host or self.settings.isolated_tunnel_local_host,
                    local_port=local_port,
                )
                try:
                    # Inside the cleanup (B3-3): a metadata write or a log open that raises (PermissionError, a
                    # full disk) used to leave <session>.json, which names the bastion and its user, behind.
                    self._write_metadata(tunnel, status="starting")
                    tunnel.stderr_handle = tunnel.log_path.open("ab", buffering=0)
                    tunnel.process = await asyncio.create_subprocess_exec(
                        "autossh",
                        *self._autossh_args(tunnel),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=tunnel.stderr_handle,
                        # The base names, not the controller's tokens and keys (W2-27), plus the operator's own
                        # AUTOSSH_ tuning (AUTOSSH_POLL, AUTOSSH_GATETIME, ...): configuration, not secrets.
                        env=allowlisted_environment(extra_prefixes=("AUTOSSH_",)),
                    )
                except BaseException:
                    # BaseException: a create cancelled while autossh is starting
                    # must not leave the log handle open or the metadata "starting".
                    tunnel.error = "autossh_start_failed"
                    self._give_up_files(tunnel)  # given up: B-fix-3
                    raise
                # Reserved and visible while it starts, so a concurrent provision
                # picks another port and release()/shutdown() can still find it.
                self._allocated_ports.add(remote_port)
                self._tunnels[session_id] = tunnel

            try:
                await asyncio.sleep(self.settings.isolated_tunnel_startup_grace_seconds)
            except BaseException:
                # A create cancelled during the grace period (the caller
                # gave up, shutdown) left autossh running with nothing
                # tracking it, holding its remote port on the bastion.
                await self._abort_starting(tunnel, "provision_cancelled")
                raise

            try:
                async with self._lock:
                    if tunnel.released:
                        # release() or shutdown() tore it down while it was starting;
                        # it already stopped the process and freed the port.
                        raise RuntimeError(f"session tunnel for {session_id} was released while it was starting")
                    if tunnel.process.returncode is not None:
                        self._forget_starting(tunnel)
                        tunnel.error = self._tail_file(tunnel.log_path) or (
                            f"autossh exited early with code {tunnel.process.returncode}"
                        )
                        try:
                            self._write_metadata(tunnel, status="error")
                        except Exception as exc:
                            # Diagnostic only (W2-7): raised, it reached the handler below, which wrote
                            # provision_failed over autossh's tail and deleted the log, and the tail was lost.
                            logger.warning(
                                "could not record the error status of session %s's tunnel: %s", session_id, exc
                            )
                        self._close_stderr(tunnel)
                        last_error = tunnel.error
                        if not any(text in last_error.lower() for text in _REMOTE_PORT_UNAVAILABLE):
                            # Not the port: the next one would fail the same way after another grace period.
                            # Out of the loop, which raises last_error: raised here, the handler below wrote
                            # provision_failed over the tail.
                            break
                        continue

                    self._write_metadata(tunnel, status="active")
                    tunnel.heartbeat_task = asyncio.create_task(self._heartbeat(tunnel))
                    return tunnel
            except BaseException:
                # Cancelled while waiting for the lock, or a failed metadata write:
                # autossh is running and tracked, and nothing else will stop it.
                await self._abort_starting(tunnel, "provision_failed")
                raise

        # Given up: no port left, or autossh failed for a reason another port would not change. The last attempt's
        # metadata and log go (a released tunnel's do too); the error is raised and logged by the caller.
        if session_id not in self._tunnels:
            self._remove_files(session_id)
        raise RuntimeError(
            last_error
            or (
                "No isolated tunnel ports were available in the configured range "
                f"{self.settings.isolated_tunnel_remote_port_start}-"
                f"{self.settings.isolated_tunnel_remote_port_end}."
            )
        )

    async def _abort_starting(self, tunnel: IsolatedSessionTunnel, reason: str) -> None:
        """Stop a tunnel provision() gave up on, unless release() already did."""
        if tunnel.released:
            return
        # Bookkeeping first and synchronously, so a second cancel during the
        # wait below cannot leave the port reserved.
        self._forget_starting(tunnel)
        process = tunnel.process
        if process.returncode is None:
            process.kill()
            await asyncio.shield(process.wait())
        tunnel.error = reason
        # Given up on: the files (bastion, user, key path; autossh's stderr) go, as on release.
        self._give_up_files(tunnel)

    def _give_up_files(self, tunnel: IsolatedSessionTunnel) -> None:
        """Record the error status, then close the log handle and remove both files, whatever that write does.

        The status write is diagnostic only: when it fails (a full disk), the handle is still closed, the files
        still removed, and the caller's own error, not this one, is what it raises (B3-3)."""
        try:
            self._write_metadata(tunnel, status="error")
        except Exception as exc:
            logger.warning("could not record the error status of session %s's tunnel: %s", tunnel.session_id, exc)
        finally:
            try:
                self._close_stderr(tunnel)
            finally:
                self._remove_files(tunnel.session_id, tunnel.info_path, tunnel.log_path)

    def _forget_starting(self, tunnel: IsolatedSessionTunnel) -> None:
        self._allocated_ports.discard(tunnel.remote_port)
        if self._tunnels.get(tunnel.session_id) is tunnel:
            del self._tunnels[tunnel.session_id]

    @staticmethod
    def _close_stderr(tunnel: IsolatedSessionTunnel) -> None:
        if tunnel.stderr_handle is not None:
            tunnel.stderr_handle.close()
            tunnel.stderr_handle = None

    async def release(self, tunnel: IsolatedSessionTunnel) -> None:
        async with self._lock:
            tunnel.released = True
            # Once `released` is set nothing else stops this tunnel, so a cancel
            # aimed at release() (a close that gave up, shutdown) must not end the
            # teardown halfway: autossh would keep running and hold its remote
            # port. Finish it under the lock, then let the cancel through.
            teardown = asyncio.create_task(self._teardown(tunnel))
            cancelled: asyncio.CancelledError | None = None
            while not teardown.done():
                try:
                    await asyncio.shield(teardown)
                except asyncio.CancelledError as exc:
                    cancelled = exc
            teardown.result()
            if cancelled is not None:
                raise cancelled

    async def _teardown(self, tunnel: IsolatedSessionTunnel) -> None:
        if tunnel.heartbeat_task is not None:
            tunnel.heartbeat_task.cancel()
            # wait(), not await: awaiting the task and suppressing CancelledError
            # would also swallow a cancel aimed at this task.
            await asyncio.wait({tunnel.heartbeat_task})
            tunnel.heartbeat_task = None

        process = tunnel.process
        if process is not None and process.returncode is None:
            # ProcessLookupError from terminate() or kill() means autossh is gone (its transport closed, as when it
            # exits while the first wait's timeout is handled): go on to the bookkeeping, which nothing retries once
            # `released` is set (W11-2).
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            else:
                try:
                    await asyncio.wait_for(process.wait(), timeout=_PROCESS_EXIT_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    else:
                        # Bounded too (W10-7): an autossh stuck in uninterruptible I/O never exits after SIGKILL,
                        # and release() finishes this teardown under a cancel, so an unbounded wait held its
                        # caller for ever.
                        try:
                            await asyncio.wait_for(process.wait(), timeout=_PROCESS_EXIT_WAIT_SECONDS)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "autossh (pid %s) of session %s's tunnel did not exit after SIGKILL;"
                                " tearing down without it",
                                process.pid,
                                tunnel.session_id,
                            )

        tunnel.status = "inactive"
        tunnel.error = None
        self._allocated_ports.discard(tunnel.remote_port)
        self._tunnels.pop(tunnel.session_id, None)
        if tunnel.stderr_handle is not None:
            tunnel.stderr_handle.close()
            tunnel.stderr_handle = None
        self._remove_files(tunnel.session_id, tunnel.info_path, tunnel.log_path)

    def _remove_files(self, session_id: str, *paths: Path) -> None:
        """Delete a tunnel's metadata and log: released, or given up on by provision().

        The metadata names the bastion, its user and key paths, and the log holds autossh's stderr: neither outlives
        the tunnel (an inactive tunnel's file has no reader, and the controller restarts without them).
        """
        for path in paths or (self.root / f"{session_id}.json", self.root / f"{session_id}.log"):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("could not remove %s for session %s: %s", path.name, session_id, exc)

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
            "access_mode": tunnel.access_mode,
            # A short code: autossh's own text (the bastion, its user, key paths) stays in the controller log.
            "error": self._error_code(tunnel),
            "last_updated": tunnel.last_updated,
        }

    @staticmethod
    def _error_code(tunnel: IsolatedSessionTunnel) -> str | None:
        if tunnel.error is None:
            return None
        return tunnel.error if tunnel.error in _TUNNEL_ERROR_CODES else TUNNEL_EXITED_CODE

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
            remote_bind_address=(
                _bare_bind_address(self.settings.isolated_tunnel_remote_bind_address)
                or self.settings.isolated_tunnel_remote_bind_address
            ),
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
                f"{_ssh_address(tunnel.remote_bind_address)}:{tunnel.remote_port}:"
                f"{_ssh_address(tunnel.local_host)}:{tunnel.local_port}"
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
        tunnel.error = self._tail_file(tunnel.log_path) or (f"autossh exited with code {tunnel.process.returncode}")
        logger.warning("session tunnel for %s degraded: %s", tunnel.session_id, tunnel.error)
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
            raise RuntimeError(
                f"isolated tunnel support is enabled but missing required settings: {', '.join(missing)}"
            )

        key_path = Path(self.settings.isolated_tunnel_key_path)
        if not key_path.is_file():
            raise RuntimeError(f"isolated tunnel key is missing or unreadable: {key_path}")

        strict = self.settings.isolated_tunnel_strict_host_key_checking
        known_hosts = Path(self.settings.isolated_tunnel_known_hosts_path)
        if strict in {"yes", "accept-new"} and not known_hosts.is_file():
            raise RuntimeError(
                f"isolated tunnel known_hosts file is required when strict host key checking is enabled: {known_hosts}"
            )

        allowed_modes = {"private", "tailscale", "cloudflare-access", "unsafe-public"}
        if self.settings.isolated_tunnel_access_mode not in allowed_modes:
            raise RuntimeError(
                "invalid ISOLATED_TUNNEL_ACCESS_MODE="
                f"{self.settings.isolated_tunnel_access_mode}; expected one of: {', '.join(sorted(allowed_modes))}"
            )

        configured = self.settings.isolated_tunnel_remote_bind_address
        bind_address = _bare_bind_address(configured)
        if bind_address is None:
            # It goes into the -R spec as host:port:local_host:local_port: a port or spaces there make a spec ssh
            # refuses or reads as another bind, and an empty one binds every interface. An IPv6 literal is
            # bracketed in the spec (_ssh_address).
            raise RuntimeError(
                "ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS must be a bare host name or IP address, with no port or spaces "
                "(for example 127.0.0.1, ::1 or [::1]; `*` for every interface (0.0.0.0 for IPv4 only), with "
                f"ISOLATED_TUNNEL_ACCESS_MODE=unsafe-public); got {configured!r}"
            )
        if not _is_bare_loopback(bind_address) and (self.settings.isolated_tunnel_access_mode != "unsafe-public"):
            raise RuntimeError(
                "isolated session tunnels refuse non-local remote bind addresses unless "
                "ISOLATED_TUNNEL_ACCESS_MODE=unsafe-public."
            )

        if (
            self.settings.isolated_tunnel_access_mode == "cloudflare-access"
            and self.settings.isolated_tunnel_public_scheme != "https"
        ):
            raise RuntimeError("Cloudflare Access mode expects ISOLATED_TUNNEL_PUBLIC_SCHEME=https")

        if self.settings.isolated_tunnel_remote_port_start > self.settings.isolated_tunnel_remote_port_end:
            raise RuntimeError("ISOLATED_TUNNEL_REMOTE_PORT_START must be <= ISOLATED_TUNNEL_REMOTE_PORT_END")

    @staticmethod
    def _tail_file(path: Path, *, max_chars: int = 800) -> str | None:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return None
        return text[-max_chars:]

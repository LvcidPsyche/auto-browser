from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.session_tunnel import IsolatedSessionTunnelBroker

# A grace period no other sleep in the code under test uses, so a test can tell it apart.
_GRACE = 0.123


class FakeProcess:
    _next_pid = 9000

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class SessionTunnelBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_broker_provisions_and_releases_tunnel(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            ssh_root = root / "ssh"
            ssh_root.mkdir(parents=True, exist_ok=True)
            (ssh_root / "id_ed25519").write_text("dummy", encoding="utf-8")
            (ssh_root / "known_hosts").write_text("dummy", encoding="utf-8")

            settings = Settings(
                _env_file=None,
                ISOLATED_TUNNEL_ENABLED="true",
                ISOLATED_TUNNEL_HOST="bastion.example.com",
                ISOLATED_TUNNEL_USER="tunnel",
                ISOLATED_TUNNEL_KEY_PATH=str(ssh_root / "id_ed25519"),
                ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=str(ssh_root / "known_hosts"),
                ISOLATED_TUNNEL_INFO_ROOT=str(root / "tunnels" / "sessions"),
                ISOLATED_TUNNEL_REMOTE_PORT_START=16181,
                ISOLATED_TUNNEL_REMOTE_PORT_END=16182,
                ISOLATED_TUNNEL_STARTUP_GRACE_SECONDS=0,
                ISOLATED_TUNNEL_INFO_INTERVAL_SECONDS=30,
            )
            broker = IsolatedSessionTunnelBroker(settings)
            await broker.startup()

            calls: list[tuple[str, ...]] = []

            async def fake_exec(*args, **kwargs):
                calls.append(tuple(str(arg) for arg in args))
                return FakeProcess()

            with patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec):
                tunnel = await broker.provision(
                    "session-1",
                    local_host="browser-session-session-1",
                    local_port=6080,
                )

            assert tunnel is not None
            self.assertTrue(tunnel.active)
            self.assertEqual(tunnel.remote_port, 16181)
            self.assertIn("autossh", calls[0][0])
            self.assertIn("127.0.0.1:16181:browser-session-session-1:6080", calls[0])

            metadata = json.loads((root / "tunnels" / "sessions" / "session-1.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "active")
            self.assertEqual(metadata["remote_port"], 16181)
            self.assertEqual(
                metadata["public_takeover_url"],
                "http://bastion.example.com:16181/vnc.html?autoconnect=true&resize=scale",
            )

            await broker.release(tunnel)

            metadata = json.loads((root / "tunnels" / "sessions" / "session-1.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "inactive")
            self.assertTrue(tunnel.released)

    async def test_cancelled_provision_stops_the_autossh_process(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            ssh_root = root / "ssh"
            ssh_root.mkdir(parents=True, exist_ok=True)
            (ssh_root / "id_ed25519").write_text("dummy", encoding="utf-8")
            (ssh_root / "known_hosts").write_text("dummy", encoding="utf-8")
            settings = Settings(
                _env_file=None,
                ISOLATED_TUNNEL_ENABLED="true",
                ISOLATED_TUNNEL_HOST="bastion.example.com",
                ISOLATED_TUNNEL_USER="tunnel",
                ISOLATED_TUNNEL_KEY_PATH=str(ssh_root / "id_ed25519"),
                ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=str(ssh_root / "known_hosts"),
                ISOLATED_TUNNEL_INFO_ROOT=str(root / "tunnels" / "sessions"),
                ISOLATED_TUNNEL_REMOTE_PORT_START=16181,
                ISOLATED_TUNNEL_REMOTE_PORT_END=16182,
                ISOLATED_TUNNEL_STARTUP_GRACE_SECONDS=30,
            )
            broker = IsolatedSessionTunnelBroker(settings)
            await broker.startup()
            processes: list[FakeProcess] = []

            async def fake_exec(*args, **kwargs):
                processes.append(FakeProcess())
                return processes[-1]

            with patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec):
                task = asyncio.create_task(broker.provision("session-1", local_port=6080))
                while not processes:
                    await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            self.assertTrue(processes[0].killed)
            metadata = json.loads((root / "tunnels" / "sessions" / "session-1.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "error")

    async def test_broker_tries_next_port_when_first_process_exits_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            ssh_root = root / "ssh"
            ssh_root.mkdir(parents=True, exist_ok=True)
            (ssh_root / "id_ed25519").write_text("dummy", encoding="utf-8")
            (ssh_root / "known_hosts").write_text("dummy", encoding="utf-8")

            settings = Settings(
                _env_file=None,
                ISOLATED_TUNNEL_ENABLED="true",
                ISOLATED_TUNNEL_HOST="bastion.example.com",
                ISOLATED_TUNNEL_USER="tunnel",
                ISOLATED_TUNNEL_KEY_PATH=str(ssh_root / "id_ed25519"),
                ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=str(ssh_root / "known_hosts"),
                ISOLATED_TUNNEL_INFO_ROOT=str(root / "tunnels" / "sessions"),
                ISOLATED_TUNNEL_REMOTE_PORT_START=16181,
                ISOLATED_TUNNEL_REMOTE_PORT_END=16182,
                ISOLATED_TUNNEL_STARTUP_GRACE_SECONDS=0,
                ISOLATED_TUNNEL_INFO_INTERVAL_SECONDS=30,
            )
            broker = IsolatedSessionTunnelBroker(settings)
            await broker.startup()

            processes = [FakeProcess(returncode=255), FakeProcess()]

            async def fake_exec(*args, **kwargs):
                process = processes.pop(0)
                stderr = kwargs["stderr"]
                if process.returncode is not None:
                    stderr.write(b"remote port forwarding failed\n")
                    stderr.flush()
                return process

            with patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec):
                tunnel = await broker.provision(
                    "session-2",
                    local_host="browser-session-session-2",
                    local_port=6080,
                )

            assert tunnel is not None
            self.assertEqual(tunnel.remote_port, 16182)
            metadata = json.loads((root / "tunnels" / "sessions" / "session-2.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "active")
            await broker.release(tunnel)

    async def _started_broker(self, root: Path, *, ports: int = 8) -> IsolatedSessionTunnelBroker:
        ssh_root = root / "ssh"
        ssh_root.mkdir(parents=True, exist_ok=True)
        (ssh_root / "id_ed25519").write_text("dummy", encoding="utf-8")
        (ssh_root / "known_hosts").write_text("dummy", encoding="utf-8")
        settings = Settings(
            _env_file=None,
            ISOLATED_TUNNEL_ENABLED="true",
            ISOLATED_TUNNEL_HOST="bastion.example.com",
            ISOLATED_TUNNEL_USER="tunnel",
            ISOLATED_TUNNEL_KEY_PATH=str(ssh_root / "id_ed25519"),
            ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=str(ssh_root / "known_hosts"),
            ISOLATED_TUNNEL_INFO_ROOT=str(root / "tunnels" / "sessions"),
            ISOLATED_TUNNEL_REMOTE_PORT_START=16181,
            ISOLATED_TUNNEL_REMOTE_PORT_END=16181 + ports - 1,
            ISOLATED_TUNNEL_STARTUP_GRACE_SECONDS=_GRACE,
            ISOLATED_TUNNEL_INFO_INTERVAL_SECONDS=30,
        )
        broker = IsolatedSessionTunnelBroker(settings)
        await broker.startup()
        return broker

    async def test_concurrent_provisions_wait_out_the_startup_grace_together(self) -> None:
        """The lock was held across the grace-period sleep, so N provisions took N
        grace periods. Counts how many are inside the grace sleep at once instead
        of timing it: all four must be there together before any is released."""
        real_sleep = asyncio.sleep
        in_grace = 0
        all_in_grace = asyncio.Event()

        async def gated_sleep(delay, *args):
            nonlocal in_grace
            if delay != _GRACE:
                return await real_sleep(delay, *args)
            in_grace += 1
            if in_grace == 4:
                all_in_grace.set()
            await all_in_grace.wait()

        async def fake_exec(*args, **kwargs):
            return FakeProcess()

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            with (
                patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec),
                patch("app.session_tunnel.asyncio.sleep", new=gated_sleep),
            ):
                provisions = [asyncio.create_task(broker.provision(f"session-{i}", local_port=6080)) for i in range(4)]
                try:
                    tunnels = await asyncio.wait_for(asyncio.gather(*provisions), timeout=10)
                finally:
                    for task in provisions:
                        task.cancel()
            self.assertEqual(len({t.remote_port for t in tunnels}), 4)
            self.assertTrue(all(t.active for t in tunnels))
            for tunnel in tunnels:
                await broker.release(tunnel)

    async def test_release_while_a_tunnel_is_starting_stops_it_and_fails_the_provision(self) -> None:
        """With the lock no longer held through the grace period, release() and
        shutdown() can reach a tunnel that is still starting."""
        real_sleep = asyncio.sleep
        in_grace = asyncio.Event()
        end_grace = asyncio.Event()

        async def gated_sleep(delay, *args):
            if delay != _GRACE:
                return await real_sleep(delay, *args)
            in_grace.set()
            await end_grace.wait()

        processes: list[FakeProcess] = []

        async def fake_exec(*args, **kwargs):
            processes.append(FakeProcess())
            return processes[-1]

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            with (
                patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec),
                patch("app.session_tunnel.asyncio.sleep", new=gated_sleep),
            ):
                provision = asyncio.create_task(broker.provision("session-1", local_port=6080))
                await asyncio.wait_for(in_grace.wait(), timeout=10)
                starting = broker._tunnels["session-1"]
                self.assertEqual(starting.status, "starting")

                await broker.shutdown()
                end_grace.set()
                with self.assertRaisesRegex(RuntimeError, "released while it was starting"):
                    await asyncio.wait_for(provision, timeout=10)

            self.assertTrue(processes[0].terminated)
            self.assertEqual(broker._tunnels, {})
            self.assertEqual(broker._allocated_ports, set())
            self.assertEqual(starting.status, "inactive")

    async def test_failed_metadata_write_does_not_leak_the_log_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            built = []
            real_build = broker._build_tunnel

            def build(*args, **kwargs):
                built.append(real_build(*args, **kwargs))
                return built[-1]

            def failing_write(tunnel, *, status):
                raise OSError("disk full")

            broker._build_tunnel = build  # type: ignore[method-assign]
            broker._write_metadata = failing_write  # type: ignore[method-assign]

            with self.assertRaisesRegex(OSError, "disk full"):
                await broker.provision("session-1", local_port=6080)

            self.assertIsNone(built[0].stderr_handle)
            self.assertEqual(broker._allocated_ports, set())


if __name__ == "__main__":
    unittest.main()

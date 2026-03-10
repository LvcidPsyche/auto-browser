from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.session_tunnel import IsolatedSessionTunnelBroker


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


if __name__ == "__main__":
    unittest.main()

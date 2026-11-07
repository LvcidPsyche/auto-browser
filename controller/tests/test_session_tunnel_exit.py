"""An autossh that exits early stops the port search unless the port was the problem (NAV-21), and a released
tunnel leaves no files and shows session users no host, user, path or stderr (NAV-22)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.session_tunnel import IsolatedSessionTunnelBroker
from tests.test_session_tunnel import FakeProcess

STDERR_TAIL = "Permission denied (publickey) for tunnel@bastion.example.com using /srv/keys/id_ed25519"
HIDDEN = ("bastion.example.com", "tunnel", "ssh_host", "ssh_user", "ssh_port", "info_path", "log_path")


class TunnelExitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ssh_root = self.root / "ssh"
        ssh_root.mkdir()
        (ssh_root / "id_ed25519").write_text("dummy", encoding="utf-8")
        (ssh_root / "known_hosts").write_text("dummy", encoding="utf-8")
        settings = Settings(
            _env_file=None,
            ISOLATED_TUNNEL_ENABLED="true",
            ISOLATED_TUNNEL_HOST="bastion.example.com",
            ISOLATED_TUNNEL_PUBLIC_HOST="takeover.example.org",
            ISOLATED_TUNNEL_USER="tunnel",
            ISOLATED_TUNNEL_KEY_PATH=str(ssh_root / "id_ed25519"),
            ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=str(ssh_root / "known_hosts"),
            ISOLATED_TUNNEL_INFO_ROOT=str(self.root / "tunnels"),
            ISOLATED_TUNNEL_REMOTE_PORT_START=16181,
            ISOLATED_TUNNEL_REMOTE_PORT_END=16185,
            ISOLATED_TUNNEL_STARTUP_GRACE_SECONDS=0,
        )
        self.broker = IsolatedSessionTunnelBroker(settings)
        await self.broker.startup()
        self.spawned: list[FakeProcess] = []

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def exiting_with(self, message: bytes, *, then_succeed: bool = False):
        async def fake_exec(*args, **kwargs):
            exits = not (then_succeed and self.spawned)
            process = FakeProcess(returncode=255 if exits else None)
            self.spawned.append(process)
            if exits:
                kwargs["stderr"].write(message)
                kwargs["stderr"].flush()
            return process

        return patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec)

    async def test_nav21_an_exit_that_is_not_a_port_problem_stops_at_the_first_port(self) -> None:
        with self.exiting_with(STDERR_TAIL.encode() + b"\n"), self.assertRaises(RuntimeError) as caught:
            await self.broker.provision("s1", local_port=6080)

        self.assertEqual(len(self.spawned), 1, "five ports in the range, one autossh started")
        self.assertIn("Permission denied", str(caught.exception))

    async def test_w2_7_a_failed_error_status_write_on_an_early_exit_keeps_autosshs_message(self) -> None:
        # The write's OSError fell into the outer handler: _abort_starting wrote provision_failed over the tail,
        # deleted the log, and provision raised the write's error, so autossh's message was lost.
        real_write = self.broker._write_metadata

        def write(tunnel, *, status):
            if status == "error":
                raise OSError(28, "sentinel-disk-full")
            return real_write(tunnel, status=status)

        self.broker._write_metadata = write  # type: ignore[method-assign]
        with (
            self.exiting_with(STDERR_TAIL.encode() + b"\n"),
            self.assertLogs("app.session_tunnel", level="WARNING") as logs,
            self.assertRaises(RuntimeError) as caught,
        ):
            await self.broker.provision("s1", local_port=6080)

        self.assertIn("Permission denied", str(caught.exception))
        self.assertEqual(len(self.spawned), 1)
        self.assertTrue(any("sentinel-disk-full" in line for line in logs.output))
        self.assertEqual(sorted(path.name for path in (self.root / "tunnels").iterdir()), [])
        self.assertEqual(self.broker._tunnels, {})
        self.assertEqual(self.broker._allocated_ports, set())

    async def test_nav21_an_address_already_in_use_moves_to_the_next_port(self) -> None:
        with self.exiting_with(b"bind [127.0.0.1]:16181: Address already in use\n", then_succeed=True):
            tunnel = await self.broker.provision("s1", local_port=6080)

        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(tunnel.remote_port, 16182)
        await self.broker.release(tunnel)

    async def test_nav22_release_deletes_the_metadata_and_log_files(self) -> None:
        async def fake_exec(*args, **kwargs):
            return FakeProcess()

        with patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec):
            tunnel = await self.broker.provision("s1", local_port=6080)
        self.assertTrue(tunnel.info_path.exists() and tunnel.log_path.exists())

        await self.broker.release(tunnel)

        self.assertFalse(tunnel.info_path.exists())
        self.assertFalse(tunnel.log_path.exists())

    async def test_nav22_describe_shows_session_users_no_host_user_path_or_stderr(self) -> None:
        async def fake_exec(*args, **kwargs):
            return FakeProcess()

        with patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec):
            tunnel = await self.broker.provision("s1", local_port=6080)
        tunnel.process.returncode = 255  # autossh died after it was active
        tunnel.log_path.write_text(STDERR_TAIL, encoding="utf-8")

        with self.assertLogs("app.session_tunnel", level="WARNING") as logs:
            described = self.broker.describe(tunnel)

        self.assertEqual(described["status"], "degraded")
        self.assertEqual(described["error"], "autossh_exited")
        shown = repr(described)
        for secret in HIDDEN + ("Permission denied", "id_ed25519"):
            self.assertNotIn(secret, shown)
        for key in ("ssh_host", "ssh_port", "ssh_user", "info_path", "log_path"):
            self.assertNotIn(key, described)
        self.assertIn("Permission denied", "\n".join(logs.output), "the operator's log keeps the stderr tail")
        self.assertEqual(described["public_takeover_url"], tunnel.public_takeover_url)
        self.assertIn("active", described)
        await self.broker.release(tunnel)

    async def test_nav22_describe_keeps_the_short_codes_it_already_had(self) -> None:
        async def fake_exec(*args, **kwargs):
            return FakeProcess()

        with patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec):
            tunnel = await self.broker.provision("s1", local_port=6080)
        tunnel.error = "provision_cancelled"

        self.assertEqual(self.broker.describe(tunnel)["error"], "provision_cancelled")
        await self.broker.release(tunnel)


if __name__ == "__main__":
    unittest.main()

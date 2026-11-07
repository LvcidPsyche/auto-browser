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

            # NAV-22: nothing of the tunnel's metadata or stderr log outlives it.
            self.assertFalse((root / "tunnels" / "sessions" / "session-1.json").exists())
            self.assertFalse((root / "tunnels" / "sessions" / "session-1.log").exists())
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
            # B-fix-3 (NAV-22): a provision that gives up leaves neither its metadata nor its log behind.
            self.assertFalse((root / "tunnels" / "sessions" / "session-1.json").exists())
            self.assertFalse((root / "tunnels" / "sessions" / "session-1.log").exists())

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

    async def _started_broker(self, root: Path, *, ports: int = 8, **extra: str) -> IsolatedSessionTunnelBroker:
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
            **extra,
        )
        broker = IsolatedSessionTunnelBroker(settings)
        await broker.startup()
        return broker

    async def test_an_autossh_that_exits_for_another_reason_keeps_its_tail_as_the_error(self) -> None:
        # B-fix-2 (NAV-21): the early exit raised inside the try whose handler wrote provision_failed over the tail.
        async def failing_exec(*args, **kwargs):
            kwargs["stderr"].write(b"Permission denied (publickey).\n")
            kwargs["stderr"].flush()
            return FakeProcess(returncode=255)

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            written: list[tuple[str, str | None]] = []
            real_write = broker._write_metadata

            def record(tunnel, *, status):
                written.append((status, tunnel.error))
                return real_write(tunnel, status=status)

            broker._write_metadata = record  # type: ignore[method-assign]
            with patch("app.session_tunnel.asyncio.create_subprocess_exec", new=failing_exec):
                with self.assertRaisesRegex(RuntimeError, "Permission denied"):
                    await broker.provision("session-1", local_port=6080)

            self.assertEqual(written[-1][0], "error")
            self.assertIn("Permission denied (publickey)", written[-1][1])
            self.assertNotIn(("error", "provision_failed"), written)
            # B-fix-3 (NAV-22): once provision gives up, neither file is left under ISOLATED_TUNNEL_INFO_ROOT.
            self.assertEqual(sorted(path.name for path in broker.root.iterdir()), [])

    async def test_b3_3_a_log_that_cannot_be_opened_leaves_no_metadata(self) -> None:
        real_open = Path.open

        def refusing_open(path, *args, **kwargs):
            if path.suffix == ".log":
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            with patch.object(Path, "open", refusing_open):
                with self.assertRaises(PermissionError):
                    await broker.provision("session-1", local_port=6080)
            self.assertEqual(sorted(path.name for path in broker.root.iterdir()), [])
            self.assertEqual(broker._tunnels, {})

    async def test_b3_3_a_failed_error_metadata_write_still_cleans_up_and_keeps_the_original_error(self) -> None:
        opened: list = []

        async def failing_exec(*args, **kwargs):
            opened.append(kwargs["stderr"])
            raise FileNotFoundError(2, "No such file or directory", "autossh")

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            real_write = broker._write_metadata

            def write(tunnel, *, status):
                if status == "error":
                    raise OSError(28, "No space left on device")
                return real_write(tunnel, status=status)

            broker._write_metadata = write  # type: ignore[method-assign]
            with patch("app.session_tunnel.asyncio.create_subprocess_exec", new=failing_exec):
                with self.assertRaises(FileNotFoundError):
                    await broker.provision("session-1", local_port=6080)
            self.assertTrue(opened and opened[0].closed)
            self.assertEqual(sorted(path.name for path in broker.root.iterdir()), [])

    async def test_b3_3_a_failed_error_metadata_write_in_the_abort_still_cleans_up(self) -> None:
        processes: list[FakeProcess] = []

        async def fake_exec(*args, **kwargs):
            processes.append(FakeProcess())
            return processes[-1]

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            real_write = broker._write_metadata

            def write(tunnel, *, status):
                # W2-8: distinct messages, so the test can tell which write provision raised: the active one's.
                if status == "active":
                    raise OSError(28, "sentinel-active-write-failed")
                if status == "error":
                    raise OSError(28, "sentinel-error-write-failed")
                return real_write(tunnel, status=status)

            broker._write_metadata = write  # type: ignore[method-assign]
            with patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec):
                with self.assertRaisesRegex(OSError, "^\\[Errno 28\\] sentinel-active-write-failed$"):
                    await broker.provision("session-1", local_port=6080)
            self.assertTrue(processes[0].killed)
            self.assertEqual(sorted(path.name for path in broker.root.iterdir()), [])
            self.assertEqual(broker._tunnels, {})
            self.assertEqual(broker._allocated_ports, set())

    async def test_b_fix_11_the_remote_bind_address_must_be_a_bare_host_or_ip(self) -> None:
        # NAV-29: the lenient loopback check accepted these, and the -R spec built from them was malformed.
        for bind, mode in (
            ("localhost:22", "private"),
            ("[::1]:22", "private"),
            (" 127.0.0.1 ", "private"),
            ("0.0.0.0:22", "unsafe-public"),
            # B3-2: empty (ssh reads it as all interfaces) stays refused; 0.0.0.0 says that on purpose.
            ("", "unsafe-public"),
            ("[::1", "private"),
            ("[]", "unsafe-public"),
            ("[127.0.0.1:22]", "unsafe-public"),
            ("[[::1]]", "private"),
            ("[ 127.0.0.1]", "private"),
        ):
            with self.subTest(bind=bind, mode=mode), tempfile.TemporaryDirectory() as tempdir:
                with self.assertRaisesRegex(RuntimeError, "ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS"):
                    await self._started_broker(
                        Path(tempdir),
                        ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS=bind,
                        ISOLATED_TUNNEL_ACCESS_MODE=mode,
                    )

    async def test_b_fix_11_a_bare_loopback_bind_address_is_accepted(self) -> None:
        for bind in ("localhost", "127.0.0.1", "127.0.0.2", "::1"):
            with self.subTest(bind=bind), tempfile.TemporaryDirectory() as tempdir:
                broker = await self._started_broker(Path(tempdir), ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS=bind)
                self.assertTrue(broker.enabled)

    async def test_b3_2_the_forward_spec_brackets_an_ipv6_bind_and_keeps_an_ipv4_one_bare(self) -> None:
        # `::1:16181:127.0.0.1:6080` is a spec ssh refuses ("Bad remote forwarding specification"); `[::1]` is ssh's
        # own spelling, accepted before B-fix-11 and now again, normalised to the bare literal.
        for bind, mode, spec, shown in (
            ("::1", "private", "[::1]:16181:127.0.0.1:6080", "::1"),
            ("[::1]", "private", "[::1]:16181:127.0.0.1:6080", "::1"),
            ("::", "unsafe-public", "[::]:16181:127.0.0.1:6080", "::"),
            ("127.0.0.1", "private", "127.0.0.1:16181:127.0.0.1:6080", "127.0.0.1"),
            ("localhost", "private", "localhost:16181:127.0.0.1:6080", "localhost"),
            ("0.0.0.0", "unsafe-public", "0.0.0.0:16181:127.0.0.1:6080", "0.0.0.0"),
        ):
            with self.subTest(bind=bind), tempfile.TemporaryDirectory() as tempdir:
                broker = await self._started_broker(
                    Path(tempdir), ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS=bind, ISOLATED_TUNNEL_ACCESS_MODE=mode
                )
                tunnel = broker._build_tunnel("session-1", remote_port=16181, local_host="127.0.0.1", local_port=6080)
                self.assertEqual(tunnel.remote_bind_address, shown)
                args = broker._autossh_args(tunnel)
                self.assertEqual(args[args.index("-R") + 1], spec)

    async def test_w2_5_a_bracketed_ipv4_or_name_is_unbracketed_and_judged_as_its_bare_spelling(self) -> None:
        # B3-2 refused these, which 1.15.0 passed to ssh (it parses them: `ssh -G`) under unsafe-public.
        for bind, mode, spec, shown in (
            ("[127.0.0.1]", "private", "127.0.0.1:16181:127.0.0.1:6080", "127.0.0.1"),
            ("[localhost]", "private", "localhost:16181:127.0.0.1:6080", "localhost"),
            ("[203.0.113.5]", "unsafe-public", "203.0.113.5:16181:127.0.0.1:6080", "203.0.113.5"),
            (
                "[bastion.example.com]",
                "unsafe-public",
                "bastion.example.com:16181:127.0.0.1:6080",
                "bastion.example.com",
            ),
        ):
            with self.subTest(bind=bind, mode=mode), tempfile.TemporaryDirectory() as tempdir:
                broker = await self._started_broker(
                    Path(tempdir), ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS=bind, ISOLATED_TUNNEL_ACCESS_MODE=mode
                )
                tunnel = broker._build_tunnel("session-1", remote_port=16181, local_host="127.0.0.1", local_port=6080)
                self.assertEqual(tunnel.remote_bind_address, shown)
                args = broker._autossh_args(tunnel)
                self.assertEqual(args[args.index("-R") + 1], spec)
        # As its bare spelling would be: a public address in brackets still needs unsafe-public.
        for bind in ("[203.0.113.5]", "[bastion.example.com]"):
            with self.subTest(bind=bind, mode="private"), tempfile.TemporaryDirectory() as tempdir:
                with self.assertRaisesRegex(RuntimeError, "non-local remote bind"):
                    await self._started_broker(Path(tempdir), ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS=bind)

    async def test_b3_2_an_ipv6_local_host_is_bracketed_in_the_spec_too(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            tunnel = broker._build_tunnel("session-1", remote_port=16181, local_host="::1", local_port=6080)
            args = broker._autossh_args(tunnel)
            self.assertEqual(args[args.index("-R") + 1], "127.0.0.1:16181:[::1]:6080")

    async def test_w4_20_star_binds_every_interface_under_unsafe_public_only(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(
                Path(tempdir), ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS="*", ISOLATED_TUNNEL_ACCESS_MODE="unsafe-public"
            )
            tunnel = broker._build_tunnel("session-1", remote_port=16181, local_host="127.0.0.1", local_port=6080)
            args = broker._autossh_args(tunnel)
            self.assertEqual(args[args.index("-R") + 1], "*:16181:127.0.0.1:6080")
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertRaisesRegex(RuntimeError, "non-local remote bind"):
                await self._started_broker(Path(tempdir), ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS="*")

    async def test_w4_20_the_bad_bind_address_error_recommends_star_for_every_interface(self) -> None:
        # It recommended 0.0.0.0 "for every interface", which binds IPv4 only.
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertRaises(RuntimeError) as raised:
                await self._started_broker(
                    Path(tempdir), ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS="", ISOLATED_TUNNEL_ACCESS_MODE="unsafe-public"
                )
        self.assertIn(
            "`*` for every interface (0.0.0.0 for IPv4 only), with ISOLATED_TUNNEL_ACCESS_MODE=unsafe-public",
            str(raised.exception),
        )

    async def test_b_fix_11_a_bare_public_bind_address_still_needs_unsafe_public(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertRaisesRegex(RuntimeError, "non-local remote bind"):
                await self._started_broker(Path(tempdir), ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS="0.0.0.0")

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

                # Bounded: a shutdown that queues behind the starting tunnel would hang here.
                await asyncio.wait_for(broker.shutdown(), timeout=10)
                end_grace.set()
                with self.assertRaisesRegex(RuntimeError, "released while it was starting"):
                    await asyncio.wait_for(provision, timeout=10)

            self.assertTrue(processes[0].terminated)
            self.assertEqual(broker._tunnels, {})
            self.assertEqual(broker._allocated_ports, set())
            self.assertEqual(starting.status, "inactive")

    async def test_provision_cancelled_while_autossh_is_starting_closes_the_log_and_marks_error(self) -> None:
        started = asyncio.Event()
        handles: list = []

        async def hanging_exec(*args, **kwargs):
            handles.append(kwargs["stderr"])
            started.set()
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            built = []
            real_build = broker._build_tunnel

            def build(*args, **kwargs):
                built.append(real_build(*args, **kwargs))
                return built[-1]

            broker._build_tunnel = build  # type: ignore[method-assign]
            with patch("app.session_tunnel.asyncio.create_subprocess_exec", new=hanging_exec):
                provision = asyncio.create_task(broker.provision("session-1", local_port=6080))
                await asyncio.wait_for(started.wait(), timeout=10)
                provision.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await provision

            tunnel = built[0]
            self.assertTrue(handles[0].closed)
            self.assertIsNone(tunnel.stderr_handle)
            # B-fix-3: given up, so the error metadata and the log are deleted (an open handle blocks that on Windows).
            self.assertEqual(tunnel.error, "autossh_start_failed")
            self.assertFalse(tunnel.info_path.exists())
            self.assertFalse(tunnel.log_path.exists())
            self.assertEqual(broker._tunnels, {})
            self.assertEqual(broker._allocated_ports, set())

    async def test_provision_cancelled_waiting_for_the_lock_after_the_grace_period_stops_autossh(self) -> None:
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

                await broker._lock.acquire()
                try:
                    end_grace.set()
                    for _ in range(5):  # let provision run up to the lock wait
                        await real_sleep(0)
                    provision.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await provision
                finally:
                    broker._lock.release()

            self.assertTrue(processes[0].killed)
            self.assertEqual(broker._tunnels, {})
            self.assertEqual(broker._allocated_ports, set())
            self.assertEqual(starting.status, "error")
            self.assertIsNone(starting.stderr_handle)

    async def test_release_cancelled_during_a_slow_heartbeat_is_cancelled(self) -> None:
        """release() absorbed any CancelledError while it waited for the heartbeat
        task, its own cancellation included, and carried on as if not cancelled."""
        heartbeat_cancelled = asyncio.Event()
        let_heartbeat_die = asyncio.Event()

        async def slow_to_die() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                heartbeat_cancelled.set()
                await let_heartbeat_die.wait()
                raise

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            tunnel = broker._build_tunnel("session-1", remote_port=16181, local_host="h", local_port=6080)
            tunnel.process = FakeProcess()
            heartbeat = asyncio.create_task(slow_to_die())
            await asyncio.sleep(0)
            tunnel.heartbeat_task = heartbeat

            release = asyncio.create_task(broker.release(tunnel))
            await asyncio.wait_for(heartbeat_cancelled.wait(), timeout=10)
            release.cancel()
            let_heartbeat_die.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(release, timeout=10)
            self.assertTrue(release.cancelled())
            await asyncio.wait({heartbeat})

    async def test_release_cancelled_while_waiting_for_the_heartbeat_still_tears_down(self) -> None:
        """release() sets `released` first, so nothing else will stop the tunnel. A
        cancel that lands while it waits for the heartbeat, even one that dies at
        once, must not leave autossh running, its port reserved or its log open."""
        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            tunnel = broker._build_tunnel("session-1", remote_port=16181, local_host="h", local_port=6080)
            tunnel.process = FakeProcess()
            tunnel.stderr_handle = tunnel.log_path.open("ab")
            broker._tunnels["session-1"] = tunnel
            broker._allocated_ports.add(16181)
            tunnel.heartbeat_task = asyncio.create_task(asyncio.sleep(3600))

            release = asyncio.create_task(broker.release(tunnel))
            await asyncio.sleep(0)  # release() takes the lock and waits for the heartbeat
            release.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await release

            self.assertTrue(tunnel.process.terminated)
            self.assertEqual(broker._allocated_ports, set())
            self.assertEqual(broker._tunnels, {})
            self.assertIsNone(tunnel.stderr_handle)
            self.assertFalse(tunnel.info_path.exists())
            self.assertFalse(tunnel.log_path.exists())

    async def test_w10_7_release_ends_when_autossh_never_exits_after_the_kill(self) -> None:
        """The wait after kill() was unbounded: an autossh stuck in uninterruptible I/O held a failed create's
        rollback or a close for ever, and release() finishes its teardown under a cancel by design."""

        class StuckProcess(FakeProcess):
            def __init__(self) -> None:
                super().__init__()
                self.exit = asyncio.Event()

            async def wait(self) -> int:
                await self.exit.wait()
                return 0

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            tunnel = broker._build_tunnel("session-1", remote_port=16181, local_host="h", local_port=6080)
            process = StuckProcess()
            tunnel.process = process
            tunnel.stderr_handle = tunnel.log_path.open("ab")
            broker._tunnels["session-1"] = tunnel
            broker._allocated_ports.add(16181)

            # W11-4: a task waited on with asyncio.wait, not wait_for: release() shields its teardown and keeps
            # awaiting it under a cancel, so with the bound reverted wait_for's cancel hung instead of failing.
            with patch("app.session_tunnel._PROCESS_EXIT_WAIT_SECONDS", 0.05):
                with self.assertLogs("app.session_tunnel", "WARNING") as logs:
                    release = asyncio.create_task(broker.release(tunnel))
                    done, _ = await asyncio.wait({release}, timeout=10)
                    finished = release in done
                    process.exit.set()  # whatever happened, nothing is left waiting on the fake
                    # W12-6: awaited before the assertion, so a failure leaves no release pending and no log handle
                    # open (on Windows the temporary directory's removal failed on it, a second error).
                    await release
                    self.assertTrue(finished, "release() did not end while autossh never exited after the kill")

            self.assertTrue(tunnel.process.killed)
            self.assertTrue(
                any("session-1" in line and str(tunnel.process.pid) in line for line in logs.output), logs.output
            )
            self.assertEqual(tunnel.status, "inactive")
            self.assertEqual(broker._allocated_ports, set())
            self.assertEqual(broker._tunnels, {})
            self.assertIsNone(tunnel.stderr_handle)
            self.assertFalse(tunnel.info_path.exists())
            self.assertFalse(tunnel.log_path.exists())

    async def test_r5c_18_a_provision_given_up_on_ends_when_autossh_never_exits_after_the_kill(self) -> None:
        """_abort_starting's wait after kill() was unbounded, as release()'s was before W10-7: a create cancelled while
        autossh started never ended while an autossh stuck in uninterruptible I/O never exited."""

        class StuckProcess(FakeProcess):
            def __init__(self) -> None:
                super().__init__()
                self.exit = asyncio.Event()

            async def wait(self) -> int:
                await self.exit.wait()
                return 0

            def kill(self) -> None:
                self.killed = True

        real_sleep = asyncio.sleep
        in_grace = asyncio.Event()

        async def gated_sleep(delay, *args):
            if delay != _GRACE:
                return await real_sleep(delay, *args)
            in_grace.set()
            await asyncio.Event().wait()

        processes: list[StuckProcess] = []

        async def fake_exec(*args, **kwargs):
            processes.append(StuckProcess())
            return processes[-1]

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            with (
                patch("app.session_tunnel.asyncio.create_subprocess_exec", new=fake_exec),
                patch("app.session_tunnel.asyncio.sleep", new=gated_sleep),
                patch("app.session_tunnel._PROCESS_EXIT_WAIT_SECONDS", 0.05),
            ):
                provision = asyncio.create_task(broker.provision("session-1", local_port=6080))
                await asyncio.wait_for(in_grace.wait(), timeout=10)
                tunnel = broker._tunnels["session-1"]
                with self.assertLogs("app.session_tunnel", "WARNING") as logs:
                    provision.cancel()
                    done, _ = await asyncio.wait({provision}, timeout=5)
                    finished = provision in done
                    processes[0].exit.set()  # whatever happened, nothing is left waiting on the fake
                    with self.assertRaises(asyncio.CancelledError):
                        await provision
                    self.assertTrue(finished, "the provision did not end while autossh never exited after the kill")

            process = processes[0]
            self.assertTrue(process.killed)
            self.assertTrue(any("session-1" in line and str(process.pid) in line for line in logs.output), logs.output)
            self.assertEqual(tunnel.error, "provision_cancelled")
            self.assertEqual((broker._tunnels, broker._allocated_ports), ({}, set()))
            self.assertIsNone(tunnel.stderr_handle)
            self.assertFalse(tunnel.info_path.exists())
            self.assertFalse(tunnel.log_path.exists())

    async def _a_registered_tunnel(self, broker, process):
        tunnel = broker._build_tunnel("session-1", remote_port=16181, local_host="h", local_port=6080)
        tunnel.process = process
        tunnel.info_path.write_text("{}", encoding="utf-8")
        tunnel.stderr_handle = tunnel.log_path.open("ab")
        broker._tunnels["session-1"] = tunnel
        broker._allocated_ports.add(16181)
        return tunnel

    def _assert_torn_down(self, broker, tunnel, handle) -> None:
        self.assertEqual(tunnel.status, "inactive")
        self.assertEqual(broker._allocated_ports, set())
        self.assertEqual(broker._tunnels, {})
        self.assertIsNone(tunnel.stderr_handle)
        self.assertTrue(handle.closed)
        self.assertFalse(tunnel.info_path.exists())
        self.assertFalse(tunnel.log_path.exists())

    async def test_w11_2_release_completes_when_autossh_is_gone_before_the_kill(self) -> None:
        """autossh exiting while the first wait's timeout was handled closed its transport, so kill() raised
        ProcessLookupError and the teardown stopped before its bookkeeping, with `released` already set."""

        class GoneAtTheKill(FakeProcess):
            async def wait(self) -> int:
                await asyncio.Event().wait()
                return 0  # pragma: no cover - never reached

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True
                raise ProcessLookupError

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            tunnel = await self._a_registered_tunnel(broker, GoneAtTheKill())
            handle = tunnel.stderr_handle

            with patch("app.session_tunnel._PROCESS_EXIT_WAIT_SECONDS", 0.05):
                await asyncio.wait_for(broker.release(tunnel), timeout=10)

            self.assertTrue(tunnel.process.killed)
            self._assert_torn_down(broker, tunnel, handle)

    async def test_w11_2_release_completes_when_autossh_is_gone_before_the_terminate(self) -> None:
        class GoneAtTheTerminate(FakeProcess):
            def terminate(self) -> None:
                self.terminated = True
                raise ProcessLookupError

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            tunnel = await self._a_registered_tunnel(broker, GoneAtTheTerminate())
            handle = tunnel.stderr_handle

            await asyncio.wait_for(broker.release(tunnel), timeout=10)

            self.assertTrue(tunnel.process.terminated)
            self._assert_torn_down(broker, tunnel, handle)

    async def test_w11_3_a_failed_log_close_still_removes_the_files(self) -> None:
        """A close() that raised (a flush on a full disk) left the handle set and skipped _remove_files, and the
        tunnel had already left _tunnels, so nothing found it again."""

        class FailingClose:
            closed = False

            def close(self) -> None:
                self.closed = True
                raise OSError("no space left on device")

        with tempfile.TemporaryDirectory() as tempdir:
            broker = await self._started_broker(Path(tempdir))
            tunnel = await self._a_registered_tunnel(broker, FakeProcess())
            tunnel.stderr_handle.close()
            failing = FailingClose()
            tunnel.stderr_handle = failing

            with self.assertLogs("app.session_tunnel", "WARNING") as logs:
                await asyncio.wait_for(broker.release(tunnel), timeout=10)

            self._assert_torn_down(broker, tunnel, failing)
            self.assertTrue(
                any("session-1" in line and "no space left on device" in line for line in logs.output), logs.output
            )

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

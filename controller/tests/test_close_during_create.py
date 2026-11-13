"""A close that lands while the session's create is still running (A2-25).

create_session registers its session, where list_sessions and close_session find it, before its start URL has
loaded, its tunnel has started and its record and audit event are written. A close in that time answered closed, and
the create went on with the closed session: it answered ``active``, wrote ``session_created`` after
``session_closed``, and the tunnel it started after the close was released by nobody (its autossh ran, and its
remote port stayed reserved, until the controller stopped). Where the pre-request gate was lost instead, the create
rolled back the session its containment had closed.

docker_ephemeral with the real tunnel broker, on the suite's fakes: autossh is a FakeProcess, the isolated runtime a
record of one.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.action_errors import SESSION_CLOSED_GATE_LOST, SessionNotFoundError
from app.browser.request_gate import RequestGateUnavailable
from app.browser_manager import BrowserManager
from app.config import Settings
from app.session_isolation import IsolatedBrowserRuntime
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage
from tests.test_session_tunnel import FakeProcess

# A grace period no other sleep in the code under test uses, so the gate below can tell it apart.
_GRACE = 0.123
_WAIT = 10


def _settings(root: Path, *, pre_request_blocking: bool) -> Settings:
    ssh = root / "ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("dummy", encoding="utf-8")
    (ssh / "known_hosts").write_text("dummy", encoding="utf-8")
    return Settings(
        _env_file=None,
        ARTIFACT_ROOT=str(root / "artifacts"),
        UPLOAD_ROOT=str(root / "uploads"),
        AUTH_ROOT=str(root / "auth"),
        APPROVAL_ROOT=str(root / "approvals"),
        AUDIT_ROOT=str(root / "audit"),
        WITNESS_ROOT=str(root / "witness"),
        SESSION_STORE_ROOT=str(root / "sessions"),
        REMOTE_ACCESS_INFO_PATH=str(root / "tunnels/reverse-ssh.json"),
        BROWSER_WS_ENDPOINT_FILE=str(root / "missing-ws.txt"),
        WITNESS_ENABLED=False,
        NETWORK_INSPECTOR_ENABLED=False,
        ENABLE_TRACING=False,
        STEALTH_ENABLED=False,
        MAX_SESSIONS=2,
        PRE_REQUEST_BLOCKING=pre_request_blocking,
        SESSION_ISOLATION_MODE="docker_ephemeral",
        ISOLATED_TUNNEL_ENABLED="true",
        ISOLATED_TUNNEL_HOST="bastion.example.com",
        ISOLATED_TUNNEL_USER="tunnel",
        ISOLATED_TUNNEL_KEY_PATH=str(ssh / "id_ed25519"),
        ISOLATED_TUNNEL_KNOWN_HOSTS_PATH=str(ssh / "known_hosts"),
        ISOLATED_TUNNEL_INFO_ROOT=str(root / "tunnels" / "sessions"),
        ISOLATED_TUNNEL_REMOTE_PORT_START=16181,
        ISOLATED_TUNNEL_REMOTE_PORT_END=16182,
        ISOLATED_TUNNEL_STARTUP_GRACE_SECONDS=_GRACE,
        ISOLATED_TUNNEL_INFO_INTERVAL_SECONDS=30,
    )


class Gate:
    """One step of the code under test, held: ``reached`` once it has started, and it goes on at ``open``."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.open = asyncio.Event()

    async def hold(self, *_args: object, **_kwargs: object) -> None:
        self.reached.set()
        await self.open.wait()


class _ACloseWhileTheCreateRuns:
    PRE_REQUEST_BLOCKING: bool

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = BrowserManager(_settings(self.root, pre_request_blocking=self.PRE_REQUEST_BLOCKING))
        self.manager._settle = AsyncMock()  # type: ignore[method-assign]
        self.broker = self.manager.tunnel_broker
        await self.broker.startup()
        self.addAsyncCleanup(self.broker.shutdown)
        self.page = FakePage()
        self.context = FakeContext(self.page)
        self.browser = FakeBrowser(self.context)
        self.browser.close = AsyncMock()  # type: ignore[attr-defined]
        self.release_runtime = AsyncMock()
        self.manager.runtime_provisioner.release = self.release_runtime  # type: ignore[method-assign]

        async def acquire(session_id: str) -> tuple[FakeBrowser, IsolatedBrowserRuntime]:
            # As DockerBrowserNodeProvisioner.provision builds it: the tunnel forwards to the container's noVNC.
            name = f"browser-session-{session_id}"
            return self.browser, IsolatedBrowserRuntime(
                session_id=session_id,
                container_id=f"container-{session_id}",
                container_name=name,
                network_name=f"{name}-net",
                browser_node_name=name,
                profile_dir=self.root / "browser-sessions" / session_id / "profile",
                downloads_dir=self.root / "browser-sessions" / session_id / "downloads",
                ws_endpoint_file=self.root / "browser-sessions" / session_id / "profile" / "ws.txt",
                ws_endpoint=f"ws://{name}:9223/playwright",
                takeover_url="http://127.0.0.1:16084/vnc.html?autoconnect=true&resize=scale",
                novnc_port=16084,
                tunnel_local_host=name,
                tunnel_local_port=6080,
            )

        self.manager._acquire_session_browser = acquire  # type: ignore[method-assign]
        self.processes: list[FakeProcess] = []

        async def autossh(*_args: object, **_kwargs: object) -> FakeProcess:
            self.processes.append(FakeProcess())
            return self.processes[-1]

        # The tunnel's startup grace is held at this gate; a test that wants the tunnel up at once opens it first.
        self.tunnel_start = Gate()
        real_sleep = asyncio.sleep

        async def sleep(delay: float, *args: object, **kwargs: object) -> None:
            if delay != _GRACE:
                await real_sleep(delay, *args, **kwargs)
                return
            await self.tunnel_start.hold()

        self.released: list[object] = []
        real_release = self.broker.release

        async def release(tunnel: object) -> None:
            self.released.append(tunnel)
            await real_release(tunnel)  # type: ignore[arg-type]

        self.broker.release = release  # type: ignore[method-assign]
        for patcher in (
            patch("app.session_tunnel.asyncio.create_subprocess_exec", new=autossh),
            patch("app.session_tunnel.asyncio.sleep", new=sleep),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def creating(self, **kwargs: object) -> asyncio.Task:
        task = asyncio.create_task(self.manager.create_session(name="racing", **kwargs))
        self.addCleanup(task.cancel)
        return task

    def the_session(self):
        (session,) = self.manager.sessions.values()
        return session

    async def reach(self, gate: Gate) -> None:
        await asyncio.wait_for(gate.reached.wait(), _WAIT)

    async def events(self) -> list[str]:
        return [event["event_type"] for event in reversed(await self.manager.list_audit_events(limit=20))]

    async def assert_answered_closed(self, create: asyncio.Task, *, reason: str | None = None) -> None:
        with self.assertRaises(SessionNotFoundError) as raised:
            await asyncio.wait_for(create, _WAIT)
        self.assertEqual((raised.exception.code, raised.exception.reason), ("session_closed", reason))

    async def assert_left_as_the_close_made_it(
        self, session, *, close_reason: str | None = None, context_closes: int = 1
    ) -> None:
        """One teardown, the close's: the tunnel released once and nothing of it left, and the record its own."""
        tunnels = [] if session.tunnel is None else [session.tunnel]
        self.assertNotIn(session.id, self.manager.sessions)
        self.assertEqual(self.released, tunnels)
        self.assertEqual((self.broker._tunnels, self.broker._allocated_ports), ({}, set()))
        self.assertEqual([process.returncode is None for process in self.processes], [False] * len(tunnels))
        self.assertEqual(self.context.close.await_count, context_closes)
        self.assertEqual(self.release_runtime.await_count, 1)
        record = await self.manager.session_store.get(session.id)
        self.assertEqual((record.status, record.close_reason, record.live), ("closed", close_reason, False))
        self.assertTrue(Path(session.artifact_dir).is_dir())

    async def test_a_close_while_the_tunnel_starts_leaves_no_tunnel_and_the_create_answers_closed(self) -> None:
        create = self.creating()
        await self.reach(self.tunnel_start)
        session = self.the_session()

        closed = await self.manager.close_session(session.id)
        self.assertEqual((closed["closed"], closed["teardown"]), (True, "complete"))
        self.assertEqual(self.released, [])  # the tunnel was not the session's yet
        self.tunnel_start.open.set()

        await self.assert_answered_closed(create)
        self.assertIsNotNone(session.tunnel)
        await self.assert_left_as_the_close_made_it(session)
        self.assertEqual(await self.events(), ["session_closed"])

    async def test_a_close_queued_behind_the_start_url_load_is_answered_closed(self) -> None:
        # The review's probe: another caller lists the sessions and closes the one it finds, while the create waits
        # on its start URL. The close waits for the session lock the load holds, then runs while the tunnel starts.
        load = Gate()

        async def goto(url: str, wait_until: str = "domcontentloaded") -> None:
            await load.hold()
            self.page.url = url

        self.page.goto = AsyncMock(side_effect=goto)
        create = self.creating(start_url="https://example.com/")
        await self.reach(load)
        listed = [item["id"] for item in await self.manager.list_sessions() if item.get("live")]
        session = self.the_session()
        self.assertEqual(listed, [session.id])
        close = asyncio.create_task(self.manager.close_session(listed[0]))
        await asyncio.sleep(0.05)
        self.assertFalse(close.done())

        load.open.set()
        await self.reach(self.tunnel_start)
        closed = await asyncio.wait_for(close, _WAIT)
        self.assertEqual((closed["closed"], closed["teardown"]), (True, "complete"))
        self.tunnel_start.open.set()

        await self.assert_answered_closed(create)
        await self.assert_left_as_the_close_made_it(session)
        self.assertEqual(await self.events(), ["session_closed"])

    async def test_a_close_that_gave_up_on_the_load_ends_the_create_before_its_tunnel(self) -> None:
        # The load does not end, so the close stops waiting for the session lock: it closes the context, and then
        # the session without the lock. When the load returns after all, no tunnel is started for the closed session.
        load = Gate()

        async def goto(url: str, wait_until: str = "domcontentloaded") -> None:
            await load.hold()
            self.page.url = url

        self.page.goto = AsyncMock(side_effect=goto)
        create = self.creating(start_url="https://example.com/")
        await self.reach(load)
        session = self.the_session()
        with (
            patch("app.browser.services.sessions.CLOSE_LOCK_WAIT_SECONDS", 0.05),
            self.assertLogs("app.browser.services.sessions", level="WARNING"),
        ):
            closed = await asyncio.wait_for(self.manager.close_session(session.id), _WAIT)
        self.assertEqual((closed["closed"], closed["teardown"]), (True, "complete"))

        load.open.set()
        await self.assert_answered_closed(create)
        self.assertEqual(self.processes, [])
        # The close's own two: the one that should have ended the load, then its teardown's.
        await self.assert_left_as_the_close_made_it(session, context_closes=2)
        self.assertEqual(await self.events(), ["session_closed"])

    STEPS = ("first save", "receipt", "second save", "summary", "audit event")

    async def assert_a_close_ends_the_create_after(self, held: str) -> None:
        """The tunnel is up, and the close lands as the create's step ``held`` ends: the close saw the tunnel and
        released it, and the create takes no further step, releases nothing again and answers closed."""
        self.tunnel_start.open.set()
        manager, gate, started = self.manager, Gate(), []

        def step(name: str, real):
            async def run(*args: object, **kwargs: object):
                started.append(name)
                result = await real(*args, **kwargs)
                if name == held:
                    await gate.hold()
                return result

            return run

        saves = iter(("first save", "second save"))
        real_save, real_summary, real_append = manager._persist_session, manager._session_summary, manager.audit.append

        async def save(*args: object, **kwargs: object) -> None:
            await step(next(saves), real_save)(*args, **kwargs)

        async def summary(session, **kwargs: object):
            # The create's own is the one with no status: a save's and the close's name theirs.
            return await (step("summary", real_summary)(session) if not kwargs else real_summary(session, **kwargs))

        async def append(**kwargs: object) -> None:
            created = kwargs["event_type"] == "session_created"
            await (step("audit event", real_append) if created else real_append)(**kwargs)

        manager._persist_session = save  # type: ignore[method-assign]
        manager._session_summary = summary  # type: ignore[method-assign]
        manager.audit.append = append  # type: ignore[method-assign]
        bridge = manager.witness_bridge
        bridge.record_session_receipt = step("receipt", bridge.record_session_receipt)  # type: ignore[method-assign]
        create = self.creating()
        await self.reach(gate)
        session = self.the_session()
        self.assertIsNotNone(session.tunnel)

        await manager.close_session(session.id)
        gate.open.set()

        await self.assert_answered_closed(create)
        self.assertEqual(started, list(self.STEPS[: self.STEPS.index(held) + 1]))
        await self.assert_left_as_the_close_made_it(session)
        written = ["session_created", "session_closed"] if held == "audit event" else ["session_closed"]
        self.assertEqual(await self.events(), written)

    async def test_a_close_after_the_first_save_ends_the_create_there(self) -> None:
        await self.assert_a_close_ends_the_create_after("first save")

    async def test_a_close_after_the_receipt_ends_the_create_there(self) -> None:
        await self.assert_a_close_ends_the_create_after("receipt")

    async def test_a_close_after_the_second_save_ends_the_create_there(self) -> None:
        await self.assert_a_close_ends_the_create_after("second save")

    async def test_a_close_after_the_summary_ends_the_create_there(self) -> None:
        await self.assert_a_close_ends_the_create_after("summary")

    async def test_a_close_after_the_audit_event_is_still_answered_closed(self) -> None:
        await self.assert_a_close_ends_the_create_after("audit event")

    async def test_a_tunnel_that_comes_up_while_the_close_tears_the_session_down_is_released(self) -> None:
        # The close has looked for the tunnel (there was none yet) and is still releasing the runtime, so the session
        # is still registered when the tunnel comes up: it is being closed all the same.
        runtime_release = Gate()
        self.release_runtime.side_effect = runtime_release.hold
        create = self.creating()
        await self.reach(self.tunnel_start)
        session = self.the_session()
        close = asyncio.create_task(self.manager.close_session(session.id))
        await self.reach(runtime_release)
        self.assertIn(session.id, self.manager.sessions)

        self.tunnel_start.open.set()
        await self.assert_answered_closed(create)
        runtime_release.open.set()
        closed = await asyncio.wait_for(close, _WAIT)

        self.assertEqual((closed["closed"], closed["teardown"]), (True, "complete"))
        self.assertIsNotNone(session.tunnel)
        await self.assert_left_as_the_close_made_it(session)
        self.assertEqual(await self.events(), ["session_closed"])

    async def test_a_create_that_fails_beside_a_close_does_not_roll_the_session_back(self) -> None:
        # Its own step fails while the close is tearing the session down: the teardown is the close's alone, and the
        # caller is told the session is closed. The error it hides is in the log.
        self.tunnel_start.open.set()
        receipt = Gate()

        async def failing_receipt(*_args: object, **_kwargs: object) -> None:
            await receipt.hold()
            raise RuntimeError("witness down")

        bridge = self.manager.witness_bridge
        bridge.record_session_receipt = AsyncMock(side_effect=failing_receipt)  # type: ignore[method-assign]
        runtime_release = Gate()
        self.release_runtime.side_effect = runtime_release.hold
        create = self.creating()
        await self.reach(receipt)
        session = self.the_session()
        close = asyncio.create_task(self.manager.close_session(session.id))
        await self.reach(runtime_release)

        with self.assertLogs("app.browser.services.session_creation", level="WARNING") as logs:
            receipt.open.set()
            await self.assert_answered_closed(create)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("its create then failed: RuntimeError: 'witness down'", logs.records[0].getMessage())
        runtime_release.open.set()
        await asyncio.wait_for(close, _WAIT)

        await self.assert_left_as_the_close_made_it(session)
        self.assertEqual(await self.events(), ["session_closed"])

    async def test_a_create_cancelled_after_its_session_closed_raises_the_cancel_and_rolls_nothing_back(self) -> None:
        self.tunnel_start.open.set()
        receipt = Gate()
        bridge = self.manager.witness_bridge
        bridge.record_session_receipt = AsyncMock(side_effect=receipt.hold)  # type: ignore[method-assign]
        create = self.creating()
        await self.reach(receipt)
        session = self.the_session()
        await self.manager.close_session(session.id)

        create.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(create, _WAIT)

        await self.assert_left_as_the_close_made_it(session)

    async def test_what_the_created_hook_registered_after_the_close_is_dropped(self) -> None:
        # The close's own hook ran before the created hook had registered anything (a CDP session, in the app's).
        self.tunnel_start.open.set()
        created = Gate()
        calls: list[str] = []

        async def on_created(_session_id: str, _page: object) -> None:
            await created.hold()
            calls.append("created hook registers")

        async def on_closed(_session_id: str) -> None:
            calls.append("closed")

        async def on_create_failed(_session_id: str) -> None:
            calls.append("create failed")

        self.manager.register_extension_hooks(
            session_created=on_created, session_closed=on_closed, session_create_failed=on_create_failed
        )
        create = self.creating()
        await self.reach(created)
        session = self.the_session()
        await self.manager.close_session(session.id)
        created.open.set()

        await self.assert_answered_closed(create)
        self.assertEqual(calls, ["closed", "created hook registers", "create failed"])
        self.assertEqual(self.processes, [])  # no tunnel was started for the closed session
        self.assertEqual(self.context.close.await_count, 1)

    async def test_a_create_no_close_touched_still_answers_active_and_its_close_releases_the_tunnel(self) -> None:
        self.tunnel_start.open.set()
        created = await self.manager.create_session(name="plain")
        session = self.the_session()
        self.assertEqual(created["status"], "active")
        self.assertIsNotNone(session.tunnel)

        await self.manager.close_session(session.id)

        await self.assert_left_as_the_close_made_it(session)
        self.assertEqual(await self.events(), ["session_created", "session_closed"])


class WithThePreRequestGateTests(_ACloseWhileTheCreateRuns, unittest.IsolatedAsyncioTestCase):
    PRE_REQUEST_BLOCKING = True

    async def test_a_gate_lost_while_the_session_starts_is_closed_once_by_its_containment(self) -> None:
        # The verification's probe: the containment closes every session on the browser, the starting one too. The
        # create then found its gate not live and rolled back the session that close had torn down already.
        load = Gate()

        async def goto(url: str, wait_until: str = "domcontentloaded") -> None:
            await load.hold()
            self.page.url = url

        self.page.goto = AsyncMock(side_effect=goto)
        contained = asyncio.Event()
        real_close = self.manager.close_session

        async def close_session(session_id: str, **kwargs: object) -> dict:
            try:
                return await real_close(session_id, **kwargs)  # type: ignore[arg-type]
            finally:
                contained.set()

        self.manager.close_session = close_session  # type: ignore[method-assign]
        create = self.creating(start_url="https://example.com/")
        await self.reach(load)
        session = self.the_session()
        gate = self.manager.request_gates.get(self.browser)
        self.assertTrue(gate.live)

        with self.assertLogs("app.browser.services.runtime", level="ERROR"):
            gate._on_close()  # the gate's CDP session goes down on its own: the containment starts
            await asyncio.sleep(0.05)
            load.open.set()
            await self.reach(self.tunnel_start)
            await asyncio.wait_for(contained.wait(), _WAIT)
            self.tunnel_start.open.set()
            await self.assert_answered_closed(create, reason=SESSION_CLOSED_GATE_LOST)
            for _ in range(200):
                if "request_gate_lost" in await self.events():
                    break
                await asyncio.sleep(0.01)

        await self.assert_left_as_the_close_made_it(session, close_reason=SESSION_CLOSED_GATE_LOST)
        self.assertEqual(await self.events(), ["session_closed", "request_gate_lost"])

    async def test_a_create_that_finds_its_gate_lost_rolls_the_session_back_alone(self) -> None:
        # No tunnel to wait for, as on the shared browser node: the create finds its gate not live in the turn of the
        # loop in which the load released the lock the containment's close was waiting for. Its rollback started a
        # turn later, and that close ran beside it: two teardowns, and session_closed for a session never created.
        self.manager.settings.isolated_tunnel_enabled = False
        load = Gate()

        async def goto(url: str, wait_until: str = "domcontentloaded") -> None:
            await load.hold()
            self.page.url = url

        self.page.goto = AsyncMock(side_effect=goto)
        create = self.creating(start_url="https://example.com/")
        await self.reach(load)
        session = self.the_session()

        with self.assertLogs("app.browser.services.runtime", level="ERROR"):
            self.manager.request_gates.get(self.browser)._on_close()
            await asyncio.sleep(0.05)
            load.open.set()
            with self.assertRaises(RequestGateUnavailable):
                await asyncio.wait_for(create, _WAIT)
            for _ in range(200):
                if "request_gate_lost" in await self.events():
                    break
                await asyncio.sleep(0.01)

        self.assertEqual(await self.events(), ["request_gate_lost"])
        self.assertEqual((self.context.close.await_count, self.release_runtime.await_count), (1, 1))
        self.assertFalse(Path(session.artifact_dir).exists())
        with self.assertRaises(KeyError):
            await self.manager.session_store.get(session.id)


class WithoutThePreRequestGateTests(_ACloseWhileTheCreateRuns, unittest.IsolatedAsyncioTestCase):
    # Without the gate nothing ends the create when the close takes its browser away: the tunnel was left.
    PRE_REQUEST_BLOCKING = False


if __name__ == "__main__":
    unittest.main()

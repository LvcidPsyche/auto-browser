"""End to end against the REAL browser-node server.mjs and a real Chromium.

Skipped unless REAL_BROWSER_NODE_TESTS=1 (needs node, `npm ci` in
browser-node/, Playwright's Chromium, and a display). The controller side is
the real BrowserManager; it reaches browser-node through the machine's LAN
address, the way the controller container reaches browser-node over the
tenant network, so the CDP relay (not Chromium's loopback-only port) is what
carries the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

from app.audit import reset_current_operator, set_current_operator
from app.browser_manager import BrowserManager
from app.config import Settings

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "browser-node" / "server.mjs"
TOKEN = "real-test-token"
CONTROL_PORT = 19324
RELAY_PORT = 19325
LEGACY_PORT = 19323


def _lan_address() -> str | None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


LAN = _lan_address()


@unittest.skipUnless(os.environ.get("REAL_BROWSER_NODE_TESTS") == "1" and LAN, "real browser-node test disabled")
class RealBrowserNodeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="real-bn-"))
        cls.endpoint_file = cls.tmp / "profile" / "ws.txt"
        env = {
            **os.environ,
            "PERSISTENT_PROFILES_ENABLED": "true",
            "PROFILE_CONTROL_TOKEN": TOKEN,
            "PROFILE_CONTROL_PORT": str(CONTROL_PORT),
            "PROFILE_CDP_RELAY_PORT": str(RELAY_PORT),
            "PROFILE_CDP_RELAY_ADVERTISED_HOST": LAN,
            "PLAYWRIGHT_SERVER_HOST": "0.0.0.0",
            "PLAYWRIGHT_SERVER_PORT": str(LEGACY_PORT),
            "PLAYWRIGHT_SERVER_ADVERTISED_HOST": LAN,
            "BROWSER_WS_ENDPOINT_FILE": str(cls.endpoint_file),
            "BROWSER_PROFILES_ROOT": str(cls.tmp / "browser-profiles"),
            "BROWSER_DOWNLOADS_DIR": str(cls.tmp / "downloads"),
        }
        cls.server = subprocess.Popen(
            ["node", str(SERVER)], cwd=str(SERVER.parent), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 60
        while not cls.endpoint_file.exists():
            if time.time() > deadline or cls.server.poll() is not None:
                raise RuntimeError("browser-node server.mjs did not start")
            time.sleep(0.25)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.terminate()
        try:
            cls.server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            cls.server.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    async def asyncSetUp(self) -> None:
        self.manager = self._make_manager()
        await self.manager.startup()

    def _make_manager(self, **overrides) -> BrowserManager:
        root = Path(tempfile.mkdtemp(prefix="real-ctl-", dir=self.tmp))
        self.root = root
        settings = dict(
                _env_file=None,
                ARTIFACT_ROOT=str(root / "artifacts"),
                UPLOAD_ROOT=str(root / "uploads"),
                AUTH_ROOT=str(root / "auth"),
                APPROVAL_ROOT=str(root / "approvals"),
                AUDIT_ROOT=str(root / "audit"),
                WITNESS_ROOT=str(root / "witness"),
                SESSION_STORE_ROOT=str(root / "sessions"),
                STATE_DB_PATH=str(root / "db" / "operator.db"),
                REMOTE_ACCESS_INFO_PATH=str(root / "tunnels/reverse-ssh.json"),
                BROWSER_WS_ENDPOINT_FILE=str(self.endpoint_file),
                WITNESS_ENABLED=False,
                ENABLE_TRACING=False,
                STEALTH_ENABLED=True,
                MAX_SESSIONS=1,
                ALLOWED_HOSTS="*",
                SESSION_ISOLATION_MODE="shared_browser_node",
                PERSISTENT_PROFILES_ENABLED=True,
                PROFILE_CONTROL_TOKEN=TOKEN,
                BROWSER_NODE_HOST=LAN,
                PROFILE_CONTROL_PORT=CONTROL_PORT,
                AUTO_PERSIST_INTERVAL_SECONDS=0,
                SESSION_WATCHDOG_INTERVAL_SECONDS=1,
        )
        settings.update(overrides)
        return BrowserManager(Settings(**settings))

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()

    async def test_open_reuse_close_then_fork_path_and_denied_default(self) -> None:
        manager = self.manager
        first = await manager.create_session(name="one", start_url=f"http://{LAN}:{CONTROL_PORT}/healthz")
        session = manager.sessions[first["id"]]
        self.assertEqual(session.persistent_profile_name, "owner-default")
        probe = await session.page.evaluate(
            "() => ({wd: navigator.webdriver, own: Object.getOwnPropertyDescriptor(navigator, 'webdriver') !== undefined})"
        )
        self.assertEqual(probe, {"wd": False, "own": False})
        await session.page.evaluate("() => localStorage.setItem('k', 'v')")

        second = await manager.create_session(name="two")
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["reused_existing_session"])

        await manager.close_session(first["id"])
        self.assertEqual(manager.sessions, {})

        # Reopen: a new launch (not a reuse), and the on-disk state survived.
        third = await manager.create_session(name="three", start_url=f"http://{LAN}:{CONTROL_PORT}/healthz")
        self.assertNotEqual(third["id"], first["id"])
        page = manager.sessions[third["id"]].page
        self.assertEqual(await page.evaluate("() => localStorage.getItem('k')"), "v")
        await manager.close_session(third["id"])

        # Finding 6: an explicit storage_state_path (fork) runs on the shared
        # launchServer() browser, which browser-node now keeps up in this mode.
        state = self.root / "auth" / "fork.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        forked = await manager.create_session(name="fork", storage_state_path=str(state))
        forked_session = manager.sessions[forked["id"]]
        self.assertIsNone(forked_session.persistent_profile_name)
        await forked_session.page.goto(f"http://{LAN}:{CONTROL_PORT}/healthz")
        self.assertIsNone(await forked_session.page.evaluate("() => localStorage.getItem('k')"))
        await manager.close_session(forked["id"])

        # Finding 2: owner-default owned by alice, caller mallory -> a plain
        # fresh browser, never the on-disk profile holding alice's logins.
        profile_dir = self.root / "auth" / "profiles" / "owner-default"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "profile.json").write_text(json.dumps({"owner": "alice"}), encoding="utf-8")
        token = set_current_operator("mallory", source="token")
        try:
            denied = await manager.create_session(name="denied")
        finally:
            reset_current_operator(token)
        denied_session = manager.sessions[denied["id"]]
        self.assertIsNone(denied_session.persistent_profile_name)
        await denied_session.page.goto(f"http://{LAN}:{CONTROL_PORT}/healthz")
        self.assertIsNone(await denied_session.page.evaluate("() => localStorage.getItem('k')"))
        await manager.close_session(denied["id"])

    async def test_open_racing_a_close_gets_a_live_browser(self) -> None:
        """Re-review finding 1, against the real server: Open fired while the
        previous session's close is in flight must end with a working
        browser, never one killed by the late close."""
        manager = self.manager
        url = f"http://{LAN}:{CONTROL_PORT}/healthz"
        for _ in range(3):
            first = await manager.create_session(name="one", start_url=url)
            closing = asyncio.create_task(manager.close_session(first["id"]))
            await asyncio.sleep(0)  # let close start and flip its state
            second = await manager.create_session(name="two")
            await closing
            self.assertNotEqual(second["id"], first["id"])
            page = manager.sessions[second["id"]].page
            await page.goto(url)
            self.assertIn("persistent_profiles_enabled", await page.content())
            await manager.close_session(second["id"])


    # -- dead browser link: the 2026-09-25 incident, for real ------------------

    def _relay_ws_endpoint(self, profile: str = "owner-default") -> str:
        request = urllib.request.Request(
            f"http://{LAN}:{RELAY_PORT}/cdp/{profile}/json/version",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())["webSocketDebuggerUrl"]

    @staticmethod
    async def _process_ids(browser, kind: str) -> list[int]:
        cdp = await browser.new_browser_cdp_session()
        try:
            info = await cdp.send("SystemInfo.getProcessInfo")
        finally:
            await cdp.detach()
        return [item["id"] for item in info["processInfo"] if item["type"] == kind]

    @staticmethod
    def _kill(pid: int) -> None:
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass

    async def _crash_driver_like_production(self, session) -> None:
        """Reproduce the exact driver death of 2026-09-25.

        A command is in flight in the tab's renderer, the renderer dies (in
        production: the pid cap), the tab is reloaded (the owner clicks Reload
        on the sad tab) -- Chromium then answers the command Playwright had
        already failed, and Playwright 1.62's driver dies on
        `assert(!object.id)` in CRSession._onMessage.
        """
        async with async_playwright() as other_pw:
            other = await other_pw.chromium.connect_over_cdp(
                self._relay_ws_endpoint(), headers={"Authorization": f"Bearer {TOKEN}"}, no_defaults=True
            )
            try:
                renderers = await self._process_ids(other, "renderer")
                in_flight = asyncio.ensure_future(
                    session.page.evaluate("() => new Promise((r) => setTimeout(() => r(1), 3000))")
                )
                await asyncio.sleep(0.5)
                for pid in renderers:
                    self._kill(pid)
                await asyncio.sleep(1.0)
                victim = other.contexts[0].pages[-1]
                reloader = await other.contexts[0].new_cdp_session(victim)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(reloader.send("Page.reload"), 10)
                with contextlib.suppress(BaseException):
                    await in_flight
            finally:
                with contextlib.suppress(Exception):
                    await other.close()

    async def _wait_for_reattach(self, manager: BrowserManager, session, count: int, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if session.reattach_count >= count and manager.sessions.get(session.id) is session:
                return
            await asyncio.sleep(0.25)
        self.fail(f"session {session.id} was not re-attached (count={session.reattach_count})")

    async def test_driver_death_reattaches_to_the_still_running_profile(self) -> None:
        manager = self.manager
        url = f"http://{LAN}:{CONTROL_PORT}/healthz"
        opened = await manager.create_session(name="owner", start_url=url)
        session = manager.sessions[opened["id"]]
        await session.page.evaluate("() => localStorage.setItem('live-login', 'kept')")
        await session.context.add_cookies([{"name": "live", "value": "1", "url": url}])
        browser_pids = await self._process_ids(session.browser, "browser")
        epoch = manager._driver_epoch

        # 1) The real crash path: the driver must actually die from it.
        await self._crash_driver_like_production(session)
        await self._wait_for_reattach(manager, session, 1)
        self.assertEqual(manager._driver_epoch, epoch + 1, "the production crash killed the driver")
        # 2) And a driver killed outright.
        manager.playwright._impl_obj._connection._transport._proc.kill()
        await self._wait_for_reattach(manager, session, 2)
        self.assertEqual(manager._driver_epoch, epoch + 2)

        listed = await manager.list_sessions()
        self.assertEqual([item["status"] for item in listed if item["id"] == session.id], ["active"])
        # Same Chromium process: the owner's live browser never went away.
        self.assertEqual(await self._process_ids(session.browser, "browser"), browser_pids)
        self.assertIn("live", [c["name"] for c in await session.context.cookies()])
        await session.page.goto(url)
        self.assertEqual(await session.page.evaluate("() => localStorage.getItem('live-login')"), "kept")
        # A second Open hands back the same (live) session, not a zombie.
        again = await manager.create_session(name="again")
        self.assertEqual(again["id"], session.id)
        await manager.close_session(session.id)

    async def test_chromium_crash_relaunches_the_profile_under_the_same_session(self) -> None:
        """The owner's Chromium itself dies (2026-09-25 03:18:57: SIGSEGV in
        the browser process). The CDP link drops; the watchdog must re-open
        the profile from disk and keep the same session id."""
        manager = self.manager
        url = f"http://{LAN}:{CONTROL_PORT}/healthz"
        opened = await manager.create_session(name="owner", start_url=url)
        session = manager.sessions[opened["id"]]
        [browser_pid] = await self._process_ids(session.browser, "browser")
        started = time.monotonic()
        self._kill(browser_pid)
        await self._wait_for_reattach(manager, session, 1, timeout=45)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 30, f"re-attach took {elapsed:.1f}s")
        self.assertNotEqual(await self._process_ids(session.browser, "browser"), [browser_pid])
        await session.page.goto(url)
        self.assertIn("persistent_profiles_enabled", await session.page.content())
        listed = await manager.list_sessions()
        self.assertEqual([item["status"] for item in listed if item["id"] == session.id], ["active"])
        await manager.close_session(session.id)

    @unittest.skipUnless(os.environ.get("REAL_BROWSER_NODE_LONGRUN_SECONDS"), "long run disabled")
    async def test_long_run_auto_persist_healthcheck_navigation_and_recovery(self) -> None:
        """Production cadence for REAL_BROWSER_NODE_LONGRUN_SECONDS (>= 1800).

        Auto-persist every 180s, the container's /healthz/deep poll every 10s
        (5-min cache), a navigation or observe every 10s, and the production
        driver crash induced twice -- the session must stay the same live
        session on the same Chromium the whole time.
        """
        duration = float(os.environ["REAL_BROWSER_NODE_LONGRUN_SECONDS"])
        log_path = os.environ.get("REAL_BROWSER_NODE_LONGRUN_LOG")
        log_file = open(log_path, "a", encoding="utf-8") if log_path else None  # noqa: SIM115

        def log(*parts) -> None:
            line = time.strftime("%H:%M:%S ") + " ".join(str(p) for p in parts)
            print(line, flush=True)
            if log_file:
                log_file.write(line + "\n")
                log_file.flush()

        import logging

        app_log = logging.getLogger("app")
        handler = logging.FileHandler(log_path, encoding="utf-8") if log_path else logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        handler.setLevel(logging.INFO)
        app_log.addHandler(handler)
        app_log.setLevel(logging.INFO)
        self.addCleanup(app_log.removeHandler, handler)

        await self.manager.shutdown()
        manager = self.manager = self._make_manager(
            AUTO_PERSIST_INTERVAL_SECONDS=180, SESSION_WATCHDOG_INTERVAL_SECONDS=5
        )
        await manager.startup()
        sites = [
            "https://www.google.com/",
            "https://en.wikipedia.org/wiki/Main_Page",
            "https://www.youtube.com/",
            "https://github.com/",
            "https://example.com/",
            f"http://{LAN}:{CONTROL_PORT}/healthz",
        ]
        opened = await manager.create_session(name="owner", start_url=sites[0])
        session = manager.sessions[opened["id"]]
        browser_pids = await self._process_ids(session.browser, "browser")
        profile_dir = Path(manager.settings.auth_root) / "profiles" / "owner-default"

        def state_mtime() -> float | None:
            # Encrypted (state.json.enc) when a key is configured, else plain.
            for name in ("state.json.enc", "state.json"):
                if (profile_dir / name).exists():
                    return (profile_dir / name).stat().st_mtime
            return None
        stats = {
            "health_ok": 0, "health_fail": 0, "nav_ok": 0, "nav_fail": 0, "observe_ok": 0,
            "observe_fail": 0, "persist_writes": 0, "crashes": 0, "max_pages": 0, "new_pages": 0,
        }
        health_modes: set[str] = set()
        hooked: set[int] = set()

        def deep_health() -> dict:
            request = urllib.request.Request(
                f"http://{LAN}:{CONTROL_PORT}/healthz/deep", headers={"Authorization": f"Bearer {TOKEN}"}
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read())

        async def health_poller() -> None:
            while True:
                try:
                    body = await asyncio.to_thread(deep_health)
                    stats["health_ok" if body.get("ok") else "health_fail"] += 1
                    health_modes.add(str(body.get("mode")))
                except Exception as exc:
                    stats["health_fail"] += 1
                    log("deep health FAILED", exc)
                await asyncio.sleep(10)

        poller = asyncio.create_task(health_poller())
        start = time.monotonic()
        crash_at = [duration / 3, 2 * duration / 3]
        last_mtime = state_mtime()
        step = 0
        log("long run start", duration, "s; session", session.id, "browser pids", browser_pids)
        try:
            while time.monotonic() - start < duration:
                elapsed = time.monotonic() - start
                if crash_at and elapsed >= crash_at[0]:
                    crash_at.pop(0)
                    log("inducing the production driver crash")
                    before = session.reattach_count
                    await self._crash_driver_like_production(session)
                    await self._wait_for_reattach(manager, session, before + 1, timeout=60)
                    stats["crashes"] += 1
                    log("recovered: reattach_count", session.reattach_count, "driver epoch", manager._driver_epoch)
                self.assertIs(manager.sessions.get(session.id), session, "session must never be retired")
                try:
                    if step % 2 == 0:
                        await manager.navigate(session.id, sites[(step // 2) % len(sites)])
                        stats["nav_ok"] += 1
                    else:
                        await manager.observe(session.id)
                        stats["observe_ok"] += 1
                except Exception as exc:
                    stats["nav_fail" if step % 2 == 0 else "observe_fail"] += 1
                    log("action failed", type(exc).__name__, str(exc)[:200])
                step += 1
                mtime = state_mtime()
                if mtime is not None and mtime != last_mtime:
                    stats["persist_writes"] += 1
                    last_mtime = mtime
                stats["max_pages"] = max(stats["max_pages"], len(session.context.pages))
                if id(session.context) not in hooked:
                    hooked.add(id(session.context))
                    session.context.on("page", lambda _page: stats.__setitem__("new_pages", stats["new_pages"] + 1))
                if step % 18 == 0:
                    log("progress", round(elapsed), stats, sorted(health_modes))
                await asyncio.sleep(10)
        finally:
            poller.cancel()
            with contextlib.suppress(BaseException):
                await poller
        log("final", stats, sorted(health_modes), "reattach_count", session.reattach_count)
        if log_file:
            log_file.close()
        self.assertEqual(stats["crashes"], 2)
        self.assertEqual(session.reattach_count, 2)
        self.assertEqual(await self._process_ids(session.browser, "browser"), browser_pids)
        self.assertGreaterEqual(stats["persist_writes"], int(duration // 180) - 1)
        self.assertEqual(stats["health_fail"], 0)
        self.assertEqual(health_modes, {"live-profiles"})
        # Auto-persist no longer opens tabs in the owner's window.
        self.assertEqual(stats["max_pages"], 1)
        self.assertEqual(stats["new_pages"], 0)
        self.assertLessEqual(stats["nav_fail"] + stats["observe_fail"], 2)
        listed = await manager.list_sessions()
        self.assertEqual([item["status"] for item in listed if item["id"] == session.id], ["active"])
        await manager.close_session(session.id)


if __name__ == "__main__":
    unittest.main()

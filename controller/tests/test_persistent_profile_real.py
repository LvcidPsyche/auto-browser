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
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

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
        root = Path(tempfile.mkdtemp(prefix="real-ctl-", dir=self.tmp))
        self.root = root
        self.manager = BrowserManager(
            Settings(
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
            )
        )
        await self.manager.startup()

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


if __name__ == "__main__":
    unittest.main()

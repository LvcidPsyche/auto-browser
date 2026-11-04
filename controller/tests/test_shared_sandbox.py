"""The shared browser-node's sandbox state: bound to its run, snapshotted at connect time, enforced under `on`.

browser-node's server.mjs writes its advertised endpoint into browser-sandbox.json. The controller takes the state
only when that endpoint is the one it just connected to (a 1.13.0 rollback leaves a stale 'on' beside a new
endpoint), keeps it in memory for that connection and never re-reads it (a compromised browser can rewrite both
files later), and under BROWSER_SANDBOX=on refuses shared-mode sessions unless it is a verified 'on'.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.browser.services.runtime import BrowserRuntimeService
from app.browser_sandbox import browser_sandbox_check, browser_sandbox_status, snapshot_shared_state, verified_on
from app.config import Settings

RUN_1 = "ws://browser-node:9223/run-1"
RUN_2 = "ws://browser-node:9223/run-2"


def _on(endpoint: str) -> dict:
    return {"setting": "on", "sandbox": "on", "endpoint": endpoint, "verified": {"renderers": 2, "renderer_userns_differs": True, "browser_seccomp_filters": 2, "renderer_seccomp_filters": [3, 3]}}


class FakeBrowser:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def is_connected(self) -> bool:
        return True


class SharedSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.dir = Path(self.tempdir.name)
        self.endpoint_file = self.dir / "browser-ws-endpoint.txt"
        self.state_file = self.dir / "browser-sandbox.json"

    def _runtime(self, setting: str = "on") -> BrowserRuntimeService:
        settings = Settings(
            _env_file=None,
            BROWSER_WS_ENDPOINT_FILE=str(self.endpoint_file),
            BROWSER_SANDBOX=setting,
            CONNECT_RETRIES="1",
            CONNECT_RETRY_DELAY_SECONDS="0",
        )

        async def connect(endpoint: str) -> FakeBrowser:
            return FakeBrowser(endpoint)

        async def attach(browser, *, attach_mode: bool) -> None:
            return None

        manager = SimpleNamespace(
            settings=settings,
            _browser_lock=asyncio.Lock(),
            browser=None,
            playwright=SimpleNamespace(chromium=SimpleNamespace(connect=connect)),
            request_gates=SimpleNamespace(attach=attach, release=attach),
        )
        return BrowserRuntimeService(manager)

    def _write(self, endpoint: str, state: dict | None) -> None:
        self.endpoint_file.write_text(endpoint, encoding="utf-8")
        if state is None:
            self.state_file.unlink(missing_ok=True)
        else:
            self.state_file.write_text(json.dumps(state), encoding="utf-8")

    async def test_a_state_from_another_run_is_unknown_and_refused(self) -> None:
        # A 1.13.0 browser-node rolled back after a 1.14 run: the old 'on' is still on disk beside a new endpoint.
        self._write(RUN_2, _on(RUN_1))
        runtime = self._runtime()
        # The API answers a bare 409 (it never returns a RuntimeError's text), so the reason goes to the log.
        with (
            self.assertLogs("app.browser.services.runtime", "WARNING") as logs,
            self.assertRaisesRegex(RuntimeError, r"BROWSER_SANDBOX=on, but the shared browser-node reports unknown"),
        ):
            await runtime.acquire_session_browser("s1")
        self.assertTrue(any("the session was refused" in line for line in logs.output), logs.output)
        self.assertEqual(runtime.shared_sandbox["sandbox"], "unknown")
        self.assertIn("another run", runtime.shared_sandbox["detail"])

    async def test_a_verified_on_for_this_run_is_accepted(self) -> None:
        self._write(RUN_1, _on(RUN_1))
        runtime = self._runtime()
        browser, isolated = await runtime.acquire_session_browser("s1")
        self.assertIsNone(isolated)
        self.assertEqual(browser.endpoint, RUN_1)
        self.assertEqual(runtime.shared_sandbox["sandbox"], "on")

    async def test_an_older_image_off_and_unverified_on_are_refused_under_on(self) -> None:
        for state in (
            None,
            {"sandbox": "off", "cause": "userns_refused", "endpoint": RUN_1},
            {"sandbox": "on", "endpoint": RUN_1},
        ):
            with self.subTest(state=state):
                self._write(RUN_1, state)
                with self.assertRaisesRegex(RuntimeError, "the session was refused"):
                    await self._runtime().acquire_session_browser("s1")

    async def test_auto_runs_sessions_whatever_the_state(self) -> None:
        self._write(RUN_1, {"sandbox": "off", "cause": "userns_refused", "endpoint": RUN_1})
        browser, _ = await self._runtime("auto").acquire_session_browser("s1")
        self.assertIsNotNone(browser)

    async def test_the_snapshot_is_never_re_read_for_a_connection(self) -> None:
        self._write(RUN_1, {"sandbox": "off", "cause": "userns_refused", "endpoint": RUN_1})
        runtime = self._runtime("auto")
        await runtime.ensure_browser()
        # A compromised browser rewrites both files to claim 'on'.
        self._write(RUN_2, _on(RUN_2))
        await runtime.ensure_browser()  # still connected: no new snapshot
        status = browser_sandbox_status(runtime.manager.settings, shared=runtime.shared_sandbox)
        self.assertEqual(status["browser_node"]["sandbox"], "off")


class IsolatedRefusalLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_isolated_refusal_is_logged(self) -> None:
        async def provision(session_id):
            raise RuntimeError(
                "BROWSER_SANDBOX=on, but isolated browser container x reports off; the session was refused."
            )

        manager = SimpleNamespace(
            settings=SimpleNamespace(session_isolation_mode="docker_ephemeral"),
            runtime_provisioner=SimpleNamespace(provision=provision),
        )
        with (
            self.assertLogs("app.browser.services.runtime", "WARNING") as logs,
            self.assertRaises(RuntimeError),
        ):
            await BrowserRuntimeService(manager).acquire_session_browser("s9")
        self.assertIn("isolated session s9 was not started: BROWSER_SANDBOX=on", logs.output[0])


class VerifiedOnTests(unittest.TestCase):
    def test_on_needs_at_least_one_verified_renderer(self) -> None:
        self.assertTrue(verified_on({"sandbox": "on", "verified": {"renderers": 1, "renderer_userns_differs": True, "browser_seccomp_filters": 2, "renderer_seccomp_filters": [3]}}))
        # D4: renderers alone, or without their own namespace, or filters not above the browser's, are not enough.
        self.assertFalse(verified_on({"sandbox": "on", "verified": {"renderers": 1}}))
        self.assertFalse(verified_on({"sandbox": "on", "verified": {**{"renderers": 1, "renderer_userns_differs": True, "browser_seccomp_filters": 2, "renderer_seccomp_filters": [3]}, "renderer_userns_differs": False}}))
        self.assertFalse(verified_on({"sandbox": "on", "verified": {**{"renderers": 1, "renderer_userns_differs": True, "browser_seccomp_filters": 2, "renderer_seccomp_filters": [3]}, "renderer_seccomp_filters": [2]}}))
        self.assertFalse(verified_on({"sandbox": "on", "verified": {**{"renderers": 1, "renderer_userns_differs": True, "browser_seccomp_filters": 2, "renderer_seccomp_filters": [3]}, "renderer_seccomp_filters": []}}))
        # The old-kernel evidence (chrome://sandbox) needs no filter counts.
        self.assertTrue(
            verified_on(
                {"sandbox": "on", "verified": {"renderers": 1, "renderer_userns_differs": True, "method": "chrome://sandbox"}}
            )
        )
        self.assertFalse(verified_on({"sandbox": "on"}))
        self.assertFalse(verified_on({"sandbox": "on", "verified": {"renderers": 0}}))
        self.assertFalse(verified_on({"sandbox": "unreadable"}))
        self.assertFalse(verified_on(None))
        # Renderers do not make a state that is not 'on' count (R11).
        for name in ("unreadable", "off", "refused", "unknown"):
            self.assertFalse(verified_on({"sandbox": name, "verified": {"renderers": 2, "renderer_userns_differs": True, "browser_seccomp_filters": 2, "renderer_seccomp_filters": [3, 3]}}), name)

    def test_snapshot_binds_to_the_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            endpoint_file = Path(tempdir) / "browser-ws-endpoint.txt"
            (Path(tempdir) / "browser-sandbox.json").write_text(json.dumps(_on(RUN_1)), encoding="utf-8")
            settings = SimpleNamespace(browser_ws_endpoint_file=str(endpoint_file))
            self.assertEqual(snapshot_shared_state(settings, RUN_1)["sandbox"], "on")
            self.assertEqual(snapshot_shared_state(settings, RUN_2)["sandbox"], "unknown")


class ReadinessRound3Tests(unittest.TestCase):
    def _status(self, **overrides) -> dict:
        base = {
            "setting": "on",
            "isolation_mode": "docker_ephemeral",
            "profile_path": "/x",
            "profile_readable": True,
            "seccomp_supported": True,
            "browser_node": None,
            "isolated": {},
        }
        base.update(overrides)
        return base

    def test_on_with_a_daemon_without_seccomp_fails(self) -> None:
        check = browser_sandbox_check(self._status(seccomp_supported=False))
        self.assertEqual(check.status, "fail")
        self.assertIn("seccomp", check.message)

    def test_on_with_a_browser_node_that_has_not_reported_warns(self) -> None:
        check = browser_sandbox_check(
            self._status(
                isolation_mode="shared_browser_node", browser_node={"sandbox": "unknown", "detail": "not connected yet"}
            )
        )
        self.assertIn(check.status, {"warn", "fail"})
        self.assertNotEqual(check.status, "pass")

    def test_on_with_an_unverified_on_fails(self) -> None:
        check = browser_sandbox_check(self._status(isolated={"s1": {"sandbox": "on"}}))
        self.assertEqual(check.status, "fail")


if __name__ == "__main__":
    unittest.main()

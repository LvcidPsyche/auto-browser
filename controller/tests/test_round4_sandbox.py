"""1.14.0 round 4 (closure3-sandbox): readiness by isolation mode, the snapshot's lifetime, cdp_attach under on.

- docker_ephemeral: isolated sessions never use the shared browser-node, so its line is informational (read from its
  state file, as in round 2) and never warns or fails readiness (S3#1, a round-3 regression).
- shared mode with neither a snapshot nor a state file: "has not reported its sandbox yet", a warning, and a failure
  under on (S3#3), not the "nothing running" pass.
- The snapshot is kept with its endpoint: a reconnect to the same endpoint reuses it (a rewrite in between changes
  nothing), a new endpoint is re-read (S3#2). It is published only while the browser is connected (S3#3).
- browser.cdp_attach is refused under on, and in every mode the attached Chrome reads as "attached over CDP, not
  checked", never verified (S3#4). CDP_CONNECT_URL stays exempt.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.browser.services.runtime import BrowserRuntimeService
from app.browser_sandbox import (
    BROWSER_SANDBOX_STATE_FILENAME,
    SECCOMP_PROFILE_PATH,
    browser_sandbox_check,
    browser_sandbox_status,
    read_sandbox_state,
    refusal,
)
from app.config import Settings

RUN_1 = "ws://browser-node:9223/run-1"
RUN_2 = "ws://browser-node:9223/run-2"


def _on(endpoint: str) -> dict:
    return {"setting": "on", "sandbox": "on", "endpoint": endpoint, "verified": {"renderers": 2}}


class _Files:
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.endpoint_file = self.dir / "browser-ws-endpoint.txt"
        self.state_file = self.dir / BROWSER_SANDBOX_STATE_FILENAME

    def write(self, endpoint: str | None, state: dict | None) -> None:
        if endpoint is None:
            self.endpoint_file.unlink(missing_ok=True)
        else:
            self.endpoint_file.write_text(endpoint, encoding="utf-8")
        if state is None:
            self.state_file.unlink(missing_ok=True)
        else:
            self.state_file.write_text(json.dumps(state), encoding="utf-8")

    def settings(self, setting: str, mode: str) -> SimpleNamespace:
        return SimpleNamespace(
            browser_sandbox=setting,
            session_isolation_mode=mode,
            browser_ws_endpoint_file=str(self.endpoint_file),
            isolated_browser_seccomp_profile=str(SECCOMP_PROFILE_PATH),
        )


class EphemeralReadinessTests(_Files, unittest.TestCase):
    def test_the_shared_node_is_informational_in_docker_ephemeral_mode(self) -> None:
        # An endpoint and a state are there, nothing ever connected: round 3 failed (on) and warned (auto) here.
        for state in (_on(RUN_1), {"sandbox": "off", "cause": "setting_off", "endpoint": RUN_1}, None):
            self.write(RUN_1, state)
            for setting in ("on", "auto"):
                with self.subTest(setting=setting, state=state):
                    status = browser_sandbox_status(self.settings(setting, "docker_ephemeral"))
                    check = browser_sandbox_check(status)
                    self.assertEqual(check.status, "pass", check.message)
                    self.assertTrue(status["browser_node_informational"])
                    if state is not None:
                        # Reported from its state file, as in round 2.
                        self.assertEqual(status["browser_node"]["sandbox"], state["sandbox"])

    def test_an_isolated_session_still_counts(self) -> None:
        self.write(RUN_1, _on(RUN_1))
        provisioner = SimpleNamespace(sandbox_states=lambda: {"s1": {"sandbox": "off", "cause": "userns_refused"}})
        check = browser_sandbox_check(browser_sandbox_status(self.settings("on", "docker_ephemeral"), provisioner))
        self.assertEqual(check.status, "fail")
        self.assertIn("session s1", check.message)
        self.assertNotIn("browser-node:", check.message)


class SharedNothingReportedTests(_Files, unittest.TestCase):
    def test_neither_a_snapshot_nor_a_state_is_not_a_pass(self) -> None:
        self.write(None, None)
        for setting, expected in (("auto", "warn"), ("on", "fail")):
            with self.subTest(setting=setting):
                check = browser_sandbox_check(browser_sandbox_status(self.settings(setting, "shared_browser_node")))
                self.assertEqual(check.status, expected)
                self.assertIn("browser-node has not reported its sandbox yet", check.message)

    def test_an_attached_chrome_is_a_warning_never_verified(self) -> None:
        for setting in ("auto", "on"):
            with self.subTest(setting=setting):
                status = browser_sandbox_status(
                    self.settings(setting, "shared_browser_node"), shared={"sandbox": "attached"}
                )
                check = browser_sandbox_check(status)
                self.assertEqual(check.status, "warn")
                self.assertIn("a Chrome attached over CDP, not checked", check.message)


class _FakeBrowser:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.connected = True
        self.handlers: dict[str, list] = {}
        self.version = "fake"

    def is_connected(self) -> bool:
        return self.connected

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def drop(self) -> None:
        self.connected = False
        for handler in self.handlers.get("disconnected", []):
            handler(self)

    async def close(self) -> None:
        self.connected = False


class SnapshotLifetimeTests(_Files, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()

    def _runtime(self, setting: str = "auto", **extra) -> BrowserRuntimeService:
        settings = Settings(
            _env_file=None,
            BROWSER_WS_ENDPOINT_FILE=str(self.endpoint_file),
            BROWSER_SANDBOX=setting,
            CONNECT_RETRIES="1",
            CONNECT_RETRY_DELAY_SECONDS="0",
            **extra,
        )

        async def connect(endpoint: str, **_kwargs) -> _FakeBrowser:
            return _FakeBrowser(endpoint)

        async def connect_over_cdp(url: str) -> _FakeBrowser:
            return _FakeBrowser(url)

        async def attach(browser, *, attach_mode: bool) -> None:
            return None

        async def release(browser) -> None:
            return None

        manager = SimpleNamespace(
            settings=settings,
            _browser_lock=asyncio.Lock(),
            browser=None,
            playwright=SimpleNamespace(chromium=SimpleNamespace(connect=connect, connect_over_cdp=connect_over_cdp)),
            request_gates=SimpleNamespace(attach=attach, release=release),
            sessions={},
            session_lifecycle=SimpleNamespace(_creating=0),
            audit=SimpleNamespace(append=AsyncMock()),
        )
        return BrowserRuntimeService(manager)

    async def test_a_reconnect_to_the_same_endpoint_reuses_the_snapshot(self) -> None:
        self.write(RUN_1, {"sandbox": "off", "cause": "userns_refused", "endpoint": RUN_1})
        runtime = self._runtime()
        await runtime.ensure_browser()
        self.assertEqual(runtime.shared_sandbox["sandbox"], "off")
        # The browser user forges a verified on for the same run, then the connection drops.
        self.write(RUN_1, _on(RUN_1))
        runtime.manager.browser.drop()
        self.assertIsNone(runtime.shared_sandbox, "a dead connection's snapshot is not reported")
        await runtime.ensure_browser()
        self.assertEqual(runtime.shared_sandbox["sandbox"], "off", "the same run is never re-read")

    async def test_a_reconnect_to_a_new_endpoint_re_reads(self) -> None:
        self.write(RUN_1, _on(RUN_1))
        runtime = self._runtime()
        await runtime.ensure_browser()
        self.assertEqual(runtime.shared_sandbox["sandbox"], "on")
        self.write(RUN_2, {"sandbox": "off", "cause": "userns_refused", "endpoint": RUN_2})
        runtime.manager.browser.drop()
        await runtime.ensure_browser()
        self.assertEqual(runtime.shared_sandbox["sandbox"], "off")
        self.assertEqual(runtime.shared_sandbox["endpoint"], RUN_2)

    async def test_cdp_attach_is_refused_under_on(self) -> None:
        self.write(RUN_1, _on(RUN_1))
        runtime = self._runtime("on")
        await runtime.ensure_browser()
        with (
            self.assertLogs("app.browser.services.runtime", "WARNING") as logs,
            self.assertRaisesRegex(RuntimeError, "browser.cdp_attach is refused under BROWSER_SANDBOX=on"),
        ):
            await runtime.cdp_attach("http://operator-chrome:9222")
        self.assertTrue(any("cdp_attach" in line for line in logs.output))
        self.assertEqual(runtime.manager.browser.endpoint, RUN_1, "the browser-node stays")
        self.assertEqual(runtime.shared_sandbox["sandbox"], "on")

    async def test_cdp_attach_under_auto_reads_as_attached(self) -> None:
        self.write(RUN_1, _on(RUN_1))
        runtime = self._runtime("auto")
        await runtime.ensure_browser()
        await runtime.cdp_attach("http://operator-chrome:9222")
        self.assertEqual(runtime.shared_sandbox, {"sandbox": "attached"})
        # And the attached Chrome is never gated as a browser-node, nor called verified.
        status = browser_sandbox_status(runtime.manager.settings, shared=runtime.shared_sandbox)
        self.assertEqual(browser_sandbox_check(status).status, "warn")

    async def test_cdp_connect_url_reads_as_attached_and_stays_exempt_under_on(self) -> None:
        runtime = self._runtime("on", CDP_CONNECT_URL="http://operator-chrome:9222")
        browser, isolated = await runtime.acquire_session_browser("s1")
        self.assertIsNone(isolated)
        self.assertEqual(runtime.shared_sandbox, {"sandbox": "attached"})


class ProvisionerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_info_is_asked_at_startup_in_docker_ephemeral_mode(self) -> None:
        from app.session_isolation import DockerBrowserNodeProvisioner
        from tests.test_browser_sandbox import _settings
        from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer

        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "data"
            data_root.mkdir()
            client = FakeDockerClient(FakeDockerContainer(container_id="controller-id", name="controller", attrs={}))
            client.security_options = []  # a daemon without seccomp
            provisioner = DockerBrowserNodeProvisioner(
                _settings(data_root, SESSION_ISOLATION_MODE="docker_ephemeral"), client=client
            )
            provisioner._controller_container_id = "controller-id"
            self.assertIsNone(provisioner.seccomp_support_known())
            await provisioner.startup()
            self.assertIs(provisioner.seccomp_support_known(), False)


class RouteReadinessTests(unittest.IsolatedAsyncioTestCase):
    """The snapshot reaches readiness over GET /readiness and the MCP readiness tool (N6, N6')."""

    async def asyncSetUp(self) -> None:
        from app.browser_manager import BrowserManager
        from tests.test_browser_manager_create_session import _settings

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = _settings(Path(self.tmp.name))
        settings.browser_sandbox = "on"
        settings.session_isolation_mode = "shared_browser_node"
        settings.browser_ws_endpoint_file = str(Path(self.tmp.name) / "browser-ws-endpoint.txt")
        Path(settings.browser_ws_endpoint_file).write_text(RUN_1, encoding="utf-8")
        self.manager = BrowserManager(settings)
        self.manager.runtime.shared_sandbox = _on(RUN_1)

    def _check(self, report: dict) -> dict:
        return next(check for check in report["checks"] if check["name"] == "browser_sandbox")

    async def test_rest(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routes.system import create_system_router

        app = FastAPI()
        app.include_router(
            create_system_router(
                settings=self.manager.settings,
                manager=self.manager,
                metrics=MagicMock(),
                maintenance=MagicMock(),
                orchestrator=MagicMock(),
                version="test",
            )
        )
        with patch("app.readiness.tesseract_available", return_value=True):
            response = TestClient(app).get("/readiness")
        self.assertEqual(self._check(response.json())["status"], "pass", response.json())

    async def test_mcp(self) -> None:
        from app.tool_gateway import McpToolGateway
        from app.tool_inputs import ReadinessCheckInput

        gateway = McpToolGateway(manager=self.manager, orchestrator=MagicMock(), job_queue=MagicMock())
        with patch("app.readiness.tesseract_available", return_value=True):
            report = await gateway._readiness_check(ReadinessCheckInput())
        self.assertEqual(self._check(report)["status"], "pass", report)


class WordingTests(_Files, unittest.TestCase):
    def test_a_missing_cause_prints_no_none(self) -> None:
        text = refusal("the shared browser-node", {"sandbox": "unknown", "detail": "no browser-sandbox.json"})
        self.assertNotIn("(None)", text)
        self.assertIn("reports unknown: no browser-sandbox.json", text)

    def test_control_characters_are_stripped_from_the_detail(self) -> None:
        self.state_file.write_text(
            json.dumps({"sandbox": "off", "cause": "x\ny", "detail": "line one\nFORGED LOG LINE\r\x1b[31mred\x00"}),
            encoding="utf-8",
        )
        state = read_sandbox_state(self.state_file)
        for key in ("detail", "cause"):
            self.assertFalse(any(ord(ch) < 32 or ord(ch) == 127 for ch in state[key]), state[key])
        self.assertIn("line one FORGED LOG LINE", state["detail"])

    def test_only_eloop_is_worded_as_a_symlink(self) -> None:
        self.state_file.write_text("{}", encoding="utf-8")
        for code, expected, absent in (
            (errno.ELOOP, "a symlink, which is refused", None),
            (errno.EACCES, os.strerror(errno.EACCES), "symlink"),
        ):
            with self.subTest(errno=code):
                with patch("app.browser_sandbox.os.open", side_effect=OSError(code, os.strerror(code))):
                    state = read_sandbox_state(self.state_file)
                self.assertEqual(state["sandbox"], "unreadable")
                self.assertIn(expected, state["detail"])
                if absent:
                    self.assertNotIn(absent, state["detail"])


if __name__ == "__main__":
    unittest.main()

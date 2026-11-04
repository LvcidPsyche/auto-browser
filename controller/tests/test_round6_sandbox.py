"""1.14.0 round 6: endpoint files read like state files, evidence of the right shape, and what reaches a response.

- Item 6 (S5#1): the shared and isolated endpoint files: no link, no FIFO, a short ws:// URL, read off the event loop,
  and an error that never carries the file's content.
- Item 12 (S5#8): a filter list whole or not at all; verified_on needs one count per renderer, none negative.
- Item 13 (S5#4, O5-2): surrogates, private-use, unassigned and variation selectors stripped; a lone surrogate in a
  state file never turns readiness into a 500.
- Items 14 to 16 (S5#5, S5#6, S5#13): the isolated exit error's wording and log tail, the docker info error in the
  startup WARNING, and an unconnected CDP_CONNECT_URL Chrome.
- S5#7 and E4: verified_on's exact method and a missing browser count; a sandbox.mjs-shaped file round trip.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.browser_sandbox import (
    EndpointFileError,
    _printable,
    browser_sandbox_check,
    browser_sandbox_status,
    read_endpoint_file,
    read_sandbox_state,
    verified_on,
)

LINUX = sys.platform == "linux"
SECRET = "SECRET-tok3n-value-0123456789"


class EndpointFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "browser-ws-endpoint.txt"

    def test_a_good_endpoint_and_an_absent_or_empty_file(self) -> None:
        self.assertIsNone(read_endpoint_file(self.path))
        self.path.write_text("  \n", encoding="utf-8")
        self.assertIsNone(read_endpoint_file(self.path))
        self.path.write_text("ws://browser-node:9223/abc\n", encoding="utf-8")
        self.assertEqual(read_endpoint_file(self.path), "ws://browser-node:9223/abc")

    def test_an_oversized_or_non_url_file_is_refused_without_its_content(self) -> None:
        for content in ("ws://x/" + "a" * 600, SECRET, f"ws://x/{SECRET} more"):
            with self.subTest(content=content[:20]):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(EndpointFileError) as raised:
                    read_endpoint_file(self.path)
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertNotIn("aaaa", str(raised.exception))

    @unittest.skipUnless(LINUX, "symlinks and FIFOs: Linux")
    def test_a_symlink_or_a_fifo_is_refused_and_never_blocks(self) -> None:
        secret = self.dir / "api-token"
        secret.write_text(SECRET, encoding="utf-8")
        self.path.symlink_to(secret)
        with self.assertRaisesRegex(EndpointFileError, "a symlink, which is refused") as raised:
            read_endpoint_file(self.path)
        self.assertNotIn(SECRET, str(raised.exception))
        self.path.unlink()
        os.mkfifo(self.path)
        outcome: list = []
        worker = threading.Thread(target=lambda: outcome.append(_try(read_endpoint_file, self.path)), daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "read_endpoint_file blocked on a FIFO")
        self.assertIsInstance(outcome[0], EndpointFileError)

    def test_the_shared_read_runs_off_the_loop_and_hides_the_content(self) -> None:
        from app.browser.services.runtime import BrowserRuntimeService

        self.path.write_text(SECRET, encoding="utf-8")
        manager = SimpleNamespace(
            settings=SimpleNamespace(browser_ws_endpoint_file=str(self.path), browser_ws_endpoint="")
        )
        runtime = BrowserRuntimeService(manager)
        with patch("app.browser.services.runtime.asyncio.to_thread", wraps=asyncio.to_thread) as to_thread:
            with self.assertRaises(EndpointFileError) as raised:
                asyncio.run(runtime.resolve_browser_ws_endpoint())
        self.assertTrue(to_thread.called)
        self.assertNotIn(SECRET, str(raised.exception))

    def test_the_isolated_wait_refuses_a_bad_endpoint_file(self) -> None:
        from app.session_isolation import DockerBrowserNodeProvisioner
        from tests.test_browser_sandbox import _settings

        self.path.write_text(SECRET, encoding="utf-8")
        provisioner = DockerBrowserNodeProvisioner(_settings(self.dir), client=MagicMock())
        container = SimpleNamespace(name="browser-session-x", status="running", attrs={}, reload=lambda: None)
        with self.assertRaises(RuntimeError) as raised:
            asyncio.run(provisioner._wait_for_ws_endpoint(container, self.path))
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertIn("does not hold a ws:// or wss:// URL", str(raised.exception))


def _try(function, *args):
    try:
        return function(*args)
    except Exception as exc:  # noqa: BLE001 - the test inspects it
        return exc


class EvidenceShapeTests(unittest.TestCase):
    EV = {
        "renderers": 2,
        "renderer_userns_differs": True,
        "browser_seccomp_filters": 2,
        "renderer_seccomp_filters": [3, 3],
    }

    def _state(self, **verified) -> dict:
        return {"sandbox": "on", "verified": {**self.EV, **verified}}

    def test_one_count_per_renderer_and_none_negative(self) -> None:
        self.assertTrue(verified_on(self._state()))
        self.assertFalse(verified_on(self._state(renderer_seccomp_filters=[3])))  # 2 renderers, 1 count
        self.assertFalse(verified_on(self._state(browser_seccomp_filters=-5, renderer_seccomp_filters=[-1, -1])))
        self.assertFalse(verified_on(self._state(browser_seccomp_filters=-1)))

    def test_exact_method_and_a_missing_browser_count(self) -> None:
        # D4d, D4e
        page = {"renderers": 1, "renderer_userns_differs": True, "method": "proc"}
        self.assertFalse(verified_on({"sandbox": "on", "verified": page}))
        self.assertFalse(verified_on({"sandbox": "on", "verified": {**page, "method": "chrome://sandbox/x"}}))
        missing = {k: v for k, v in self.EV.items() if k != "browser_seccomp_filters"}
        self.assertFalse(verified_on({"sandbox": "on", "verified": missing}))

    def test_a_filter_list_with_a_non_int_is_dropped_whole(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "browser-sandbox.json"
            path.write_text(
                json.dumps({"sandbox": "on", "verified": {**self.EV, "renderer_seccomp_filters": [3, "3", True]}}),
                encoding="utf-8",
            )
            state = read_sandbox_state(path)
        self.assertNotIn("renderer_seccomp_filters", state["verified"])
        self.assertFalse(verified_on(state))

    def test_sandbox_mjs_shaped_files_round_trip(self) -> None:
        # E4: what sandbox.mjs writes on each kernel path is a verified on after read_sandbox_state.
        proc = {
            "setting": "on",
            "seccomp_profile": "auto-browser",
            "sandbox": "on",
            "cause": None,
            "detail": "Chromium runs sandboxed: renderers are in their own user namespace under Chromium's seccomp filter.",
            "verified": {
                "renderers": 3,
                "renderer_userns_differs": True,
                "browser_seccomp_filters": 2,
                "renderer_seccomp_filters": [3, 3, 3],
            },
            "endpoint": "ws://browser-node:9223/abc",
        }
        page = {**proc, "verified": {"renderers": 1, "renderer_userns_differs": True, "method": "chrome://sandbox"}}
        with tempfile.TemporaryDirectory() as tempdir:
            for name, state in (("proc", proc), ("chrome", page)):
                with self.subTest(path=name):
                    file = Path(tempdir) / f"{name}.json"
                    file.write_text(json.dumps(state), encoding="utf-8")
                    self.assertTrue(verified_on(read_sandbox_state(file)))


class PrintableTests(unittest.TestCase):
    def test_surrogates_private_use_unassigned_and_variation_selectors(self) -> None:
        self.assertEqual(_printable("a\ud800bc\U000e0100d️e\U0010ffffz"), "a b c d e z")

    def test_a_lone_surrogate_never_makes_readiness_a_500(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.browser_manager import BrowserManager
        from app.routes.system import create_system_router
        from tests.test_browser_manager_create_session import _settings

        with tempfile.TemporaryDirectory() as tempdir:
            settings = _settings(Path(tempdir))
            settings.browser_sandbox = "on"
            settings.session_isolation_mode = "shared_browser_node"
            settings.browser_ws_endpoint_file = str(Path(tempdir) / "browser-ws-endpoint.txt")
            # A forged state with a JSON-escaped lone surrogate, and no endpoint: its refusal is reported.
            (Path(tempdir) / "browser-sandbox.json").write_text(
                '{"sandbox": "refused", "cause": "x\\ud800", "detail": "lone \\udc00 surrogate"}', encoding="utf-8"
            )
            manager = BrowserManager(settings)
            app = FastAPI()
            app.include_router(
                create_system_router(
                    settings=settings,
                    manager=manager,
                    metrics=MagicMock(),
                    maintenance=MagicMock(),
                    orchestrator=MagicMock(),
                    version="test",
                )
            )
            with patch("app.readiness.tesseract_available", return_value=True):
                response = TestClient(app, raise_server_exceptions=False).get("/readiness")
        self.assertIn(response.status_code, (200, 503))
        check = next(c for c in response.json()["checks"] if c["name"] == "browser_sandbox")
        self.assertEqual(check["status"], "fail")


class WordingTests(unittest.TestCase):
    def test_an_unconnected_cdp_connect_url_chrome_is_attached_not_a_browser_node(self) -> None:
        # Item 16 (S5#13), and S5b's label.
        with tempfile.TemporaryDirectory() as tempdir:
            settings = SimpleNamespace(
                browser_sandbox="on",
                session_isolation_mode="shared_browser_node",
                browser_ws_endpoint_file=str(Path(tempdir) / "browser-ws-endpoint.txt"),
                cdp_connect_url="http://operator-chrome:9222",
                isolated_browser_seccomp_profile="/x",
            )
            status = browser_sandbox_status(settings)
        self.assertEqual(status["browser_node"], {"sandbox": "attached", "detail": "not connected"})
        check = browser_sandbox_check(status)
        self.assertEqual(check.status, "warn")
        self.assertIn(
            "attached Chrome: attached over CDP, not checked (not connected)", check.message + _describe(status)
        )

    def test_the_isolated_exit_error_joins_its_parts_and_cleans_the_log(self) -> None:
        # Item 14 (S5#5): no "(None)", and the container's own output never raw.
        from app.session_isolation import DockerBrowserNodeProvisioner
        from tests.test_browser_sandbox import _settings

        with tempfile.TemporaryDirectory() as tempdir:
            directory = Path(tempdir)
            (directory / "browser-sandbox.json").write_text('{"sandbox": "refused", "detail": "no namespace"}', "utf-8")
            provisioner = DockerBrowserNodeProvisioner(_settings(directory), client=MagicMock())
            provisioner._container_logs = lambda container: "line one\n\x1b[31mFORGED\x1b[0m\x07"
            container = SimpleNamespace(name="browser-session-x", status="exited", attrs={}, reload=lambda: None)
            with self.assertRaises(RuntimeError) as raised:
                asyncio.run(provisioner._wait_for_ws_endpoint(container, directory / "browser-ws-endpoint.txt"))
        text = str(raised.exception)
        self.assertIn("Its sandbox state: refused: no namespace.", text)
        self.assertNotIn("(None)", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)

    def test_the_startup_warning_carries_the_docker_info_error(self) -> None:
        # Item 15 (S5#6)
        from app.session_isolation import DockerBrowserNodeProvisioner
        from tests.test_browser_sandbox import _settings
        from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer

        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "data"
            data_root.mkdir()
            client = FakeDockerClient(FakeDockerContainer(container_id="controller-id", name="controller", attrs={}))

            def broken_info():
                raise ConnectionError("daemon unreachable at /var/run/docker.sock")

            client.info = broken_info
            provisioner = DockerBrowserNodeProvisioner(
                _settings(data_root, SESSION_ISOLATION_MODE="docker_ephemeral"), client=client
            )
            provisioner._controller_container_id = "controller-id"
            with self.assertLogs("app.session_isolation", "WARNING") as logs:
                asyncio.run(provisioner.startup())
        self.assertTrue(any("daemon unreachable at /var/run/docker.sock" in line for line in logs.output), logs.output)


def _describe(status: dict) -> str:
    from app.browser_sandbox import _Line

    node = status["browser_node"]
    return _Line("attached Chrome", node).describe()


if __name__ == "__main__":
    unittest.main()

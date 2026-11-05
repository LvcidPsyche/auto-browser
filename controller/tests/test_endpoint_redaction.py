"""The path of a browser-node's ws endpoint is that Playwright server's only secret, and CI dumps controller logs.

scripts/assert_browser_sandbox.py redacts it where the scripts print; this is the controller's own twin, used in
the connect log line, the connect failure message and the readiness details.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
import unittest
from types import SimpleNamespace

from app.browser.services.runtime import BrowserRuntimeService
from app.browser_sandbox import browser_sandbox_check, redact_endpoints

SECRET = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
ENDPOINT = f"ws://browser-node:9223/{SECRET}"


class RedactEndpointsTests(unittest.TestCase):
    def test_the_path_goes_and_scheme_host_and_port_stay(self) -> None:
        self.assertEqual(redact_endpoints(ENDPOINT), "ws://browser-node:9223/<redacted>")
        self.assertEqual(redact_endpoints(f"wss://h.example/{SECRET}?x=1"), "wss://h.example/<redacted>")

    def test_every_endpoint_in_a_message_is_redacted(self) -> None:
        text = f'connect to "{ENDPOINT}" failed, last good {ENDPOINT} (retry)'

        redacted = redact_endpoints(text)

        self.assertNotIn(SECRET, redacted)
        self.assertEqual(redacted.count("ws://browser-node:9223/<redacted>"), 2)

    def test_text_without_an_endpoint_and_an_endpoint_without_a_path_are_unchanged(self) -> None:
        self.assertEqual(redact_endpoints("no endpoint here"), "no endpoint here")
        self.assertEqual(redact_endpoints("ws://browser-node:9223"), "ws://browser-node:9223")


class FakeChromium:
    async def connect(self, endpoint: str) -> object:
        return object()


class FailingChromium:
    async def connect(self, endpoint: str) -> object:
        raise ConnectionRefusedError("refused")


class NamingChromium:
    """A connect failure as Playwright words it: the error text carries the endpoint, path and all."""

    async def connect(self, endpoint: str) -> object:
        raise ConnectionRefusedError(f"BrowserType.connect: connect ECONNREFUSED <ws connecting> {endpoint}")


def _manager(direct_endpoint: str | None = None) -> SimpleNamespace:
    async def attach(browser: object, *, attach_mode: bool) -> None:
        return None

    return SimpleNamespace(
        playwright=SimpleNamespace(chromium=FakeChromium()),
        request_gates=SimpleNamespace(attach=attach),
        settings=SimpleNamespace(
            connect_retries=1,
            connect_retry_delay_seconds=0,
            browser_ws_endpoint=direct_endpoint,
            browser_ws_endpoint_file="/data/browser-profile/browser-ws-endpoint.txt",
        ),
    )


class ControllerOutputRedactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_connect_log_line_does_not_carry_the_path(self) -> None:
        runtime = BrowserRuntimeService(_manager())

        async def factory() -> str:
            return ENDPOINT

        with self.assertLogs("app.browser.services.runtime", level=logging.DEBUG) as logs:
            await runtime.connect_browser(factory, failure_context="unused")

        output = "\n".join(logs.output)
        self.assertIn("connected to browser node", output)
        self.assertIn("ws://browser-node:9223/<redacted>", output)
        self.assertNotIn(SECRET, output)

    async def test_the_failure_message_does_not_carry_a_configured_direct_endpoint(self) -> None:
        manager = _manager(direct_endpoint=ENDPOINT)
        manager._browser_lock = asyncio.Lock()
        manager.browser = None
        manager.settings.cdp_connect_url = None
        manager.playwright.chromium = FailingChromium()

        with self.assertRaises(RuntimeError) as raised:
            await BrowserRuntimeService(manager).ensure_browser()

        self.assertNotIn(SECRET, str(raised.exception))
        self.assertIn("ws://browser-node:9223/<redacted>", str(raised.exception))

    async def test_a_connect_failure_names_the_endpoint_without_its_path_in_the_log_and_the_traceback(self) -> None:
        manager = _manager()
        manager.playwright.chromium = NamingChromium()

        async def factory() -> str:
            return ENDPOINT

        with self.assertLogs("app.browser.services.runtime", level=logging.WARNING) as logs:
            with self.assertRaises(RuntimeError) as raised:
                await BrowserRuntimeService(manager).connect_browser(
                    factory, failure_context="browser node unreachable"
                )

        formatted = "".join(traceback.format_exception(raised.exception))
        output = "\n".join(logs.output)
        self.assertNotIn(SECRET, formatted)
        self.assertNotIn(SECRET, output)
        self.assertIn("browser node unreachable", formatted)
        self.assertIn("ws://browser-node:9223/<redacted>", output)  # what failed is still logged, minus the path

    def test_readiness_details_do_not_carry_the_endpoint(self) -> None:
        state = {"sandbox": "on", "endpoint": ENDPOINT}
        status = {
            "setting": "auto",
            "isolation_mode": "shared_browser_node",
            "browser_node": dict(state),
            "isolated": {"s1": {"sandbox": "on", "endpoint": ENDPOINT}},
        }

        check = browser_sandbox_check(status)

        self.assertNotIn(SECRET, repr(check.details))
        self.assertEqual(check.details["browser_node"]["endpoint"], "ws://browser-node:9223/<redacted>")
        self.assertEqual(status["browser_node"]["endpoint"], ENDPOINT, "the input is not mutated")

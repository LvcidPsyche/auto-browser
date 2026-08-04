from __future__ import annotations

import unittest

from app.cdp import passthrough as cdp_passthrough
from app.cdp.passthrough import CDPPassthrough


class _FakeCDPSession:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict | None = None) -> dict:
        payload = params or {}
        self.calls.append((method, payload))
        response = self.responses[method]
        if isinstance(response, list):
            value = response.pop(0)
        else:
            value = response
        if isinstance(value, Exception):
            raise value
        return value


class _FakeContext:
    def __init__(self, session: _FakeCDPSession) -> None:
        self.session = session

    async def new_cdp_session(self, page) -> _FakeCDPSession:
        return self.session


class _FakePage:
    def __init__(self, session: _FakeCDPSession) -> None:
        self.context = _FakeContext(session)


class CDPPassthroughTests(unittest.IsolatedAsyncioTestCase):
    async def test_domain_allowed_matches_empty_and_glob_lists(self) -> None:
        self.assertTrue(cdp_passthrough._domain_allowed("https://example.com"))

        original = list(cdp_passthrough._ALLOWED_DOMAINS)
        cdp_passthrough._ALLOWED_DOMAINS[:] = ["https://*.example.com/*"]
        try:
            self.assertTrue(cdp_passthrough._domain_allowed("https://app.example.com/home"))
            self.assertFalse(cdp_passthrough._domain_allowed("https://example.org"))
        finally:
            cdp_passthrough._ALLOWED_DOMAINS[:] = original

    async def test_get_element_intelligence_returns_filtered_dom_data(self) -> None:
        fake = _FakeCDPSession(
            {
                "DOM.getDocument": {"root": {"nodeId": 11}},
                "DOM.querySelector": {"nodeId": 22},
                "DOM.getAttributes": {"attributes": ["id", "hero", "onclick", "go()"]},
                "DOM.getBoxModel": {"model": {"width": 640, "height": 360, "content": [1, 2, 3, 4]}},
                "CSS.getComputedStyleForNode": {
                    "computedStyle": [
                        {"name": "display", "value": "block"},
                        {"name": "width", "value": "640px"},
                        {"name": "transform", "value": "scale(1.1)"},
                    ]
                },
                "Runtime.evaluate": [
                    {"result": {"value": ["click"]}},
                    {
                        "result": {
                            "value": {
                                "src": "https://cdn.example.com/hero.png",
                                "href": None,
                                "currentSrc": "",
                                "backgroundImage": "none",
                            }
                        }
                    },
                ],
            }
        )

        result = await CDPPassthrough(fake).get_element_intelligence("#hero")

        self.assertEqual(result["node_id"], 22)
        self.assertEqual(result["attributes"]["id"], "hero")
        self.assertEqual(result["computed_styles"], {"display": "block", "width": "640px"})
        self.assertEqual(result["event_listener_types"], ["click"])
        self.assertEqual(result["assets"], {"src": "https://cdn.example.com/hero.png"})

    async def test_get_element_intelligence_returns_not_found_error(self) -> None:
        fake = _FakeCDPSession(
            {
                "DOM.getDocument": {"root": {"nodeId": 11}},
                "DOM.querySelector": {"nodeId": 0},
            }
        )

        result = await CDPPassthrough(fake).get_element_intelligence(".missing")

        self.assertEqual(result, {"error": "Selector not found: '.missing'"})

    async def test_get_element_intelligence_catches_cdp_errors(self) -> None:
        fake = _FakeCDPSession({"DOM.getDocument": RuntimeError("boom")})

        result = await CDPPassthrough(fake).get_element_intelligence(".hero")

        self.assertEqual(result["selector"], ".hero")
        self.assertEqual(result["error"], "cdp_element_intelligence_failed")

    async def test_raw_cdp_command_enforces_allowlist_and_catches_runtime_errors(self) -> None:
        fake = _FakeCDPSession(
            {
                "DOM.getDocument": {"root": {"nodeId": 1}},
                # Previously this used Runtime.evaluate as the "allowed command
                # that fails at runtime" example. It is no longer allowed through
                # the raw passthrough at all — arbitrary JS in an authenticated
                # page is cookie theft plus SSRF — so a genuinely read-only
                # command stands in for that case.
                "Accessibility.getFullAXTree": RuntimeError("ax tree failed"),
            }
        )
        passthrough = CDPPassthrough(fake)

        success = await passthrough.raw_cdp_command("DOM.getDocument", {"depth": 1})
        self.assertEqual(success["root"]["nodeId"], 1)

        error = await passthrough.raw_cdp_command("Accessibility.getFullAXTree")
        self.assertEqual(error, {"error": "cdp_command_failed", "method": "Accessibility.getFullAXTree"})

        with self.assertRaisesRegex(ValueError, "not in the allowed list"):
            await passthrough.raw_cdp_command("Page.navigate")

        # The security property this endpoint depends on.
        for blocked in ("Runtime.evaluate", "Network.getCookies"):
            with self.assertRaisesRegex(ValueError, "not in the allowed list"):
                await passthrough.raw_cdp_command(blocked, {"expression": "document.cookie"})

    async def test_from_page_builds_passthrough_from_new_cdp_session(self) -> None:
        fake_session = _FakeCDPSession({"DOM.getDocument": {"root": {"nodeId": 1}}})

        passthrough = await CDPPassthrough.from_page(_FakePage(fake_session))

        self.assertIsInstance(passthrough, CDPPassthrough)
        self.assertIs(passthrough._cdp, fake_session)

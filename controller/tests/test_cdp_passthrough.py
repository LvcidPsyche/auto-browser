from __future__ import annotations

import asyncio
import json
import re
import unittest
from unittest.mock import patch

from app.browser_scripts import AB_ATTR_MAX, AB_HREF_MAX, AB_MAX_ATTRIBUTES, AB_TRUNCATED_MARK
from app.cdp import passthrough as cdp_passthrough
from app.cdp.passthrough import CDPPassthrough


class _FakeCDPSession:
    def __init__(
        self, responses: dict[str, object], *, fail: frozenset[str] = frozenset(), hang: frozenset[str] = frozenset()
    ) -> None:
        self.responses = responses
        self.fail = fail
        self.hang = hang
        self.gates: dict[str, asyncio.Event] = {}
        self.reached: dict[str, asyncio.Event] = {}
        self.calls: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict | None = None) -> dict:
        payload = params or {}
        self.calls.append((method, payload))
        if method in self.hang:
            await asyncio.Event().wait()
        if method in self.gates:
            self.reached.setdefault(method, asyncio.Event()).set()
            await self.gates[method].wait()
        if method in self.fail:
            raise RuntimeError(f"{method} failed: target closed")
        if method.endswith((".enable", ".disable")):
            return {}
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

    async def test_page_controlled_attributes_and_listener_names_are_capped(self) -> None:
        huge = "x" * 1_000_000
        pairs = ["data-big", huge, "href", "https://e.example/" + huge, "srcset", huge, huge, "v"]
        for index in range(200):
            pairs += [f"data-{index}", "v"]
        responses = dict(self._READ_RESPONSES)
        responses["DOM.getAttributes"] = {"attributes": pairs}
        responses["Runtime.evaluate"] = [
            {"result": {"value": [huge] + ["click"] * 200}},
            {"result": {"value": {}}},
        ]

        result = await CDPPassthrough(_FakeCDPSession(responses)).get_element_intelligence("#hero")

        attributes = result["attributes"]
        self.assertEqual(len(attributes), AB_MAX_ATTRIBUTES)
        self.assertEqual(len(attributes["data-big"]), AB_ATTR_MAX)
        self.assertEqual(len(attributes["href"]), AB_HREF_MAX)
        self.assertTrue(attributes["href"].endswith(AB_TRUNCATED_MARK))
        self.assertEqual(len(attributes["srcset"]), AB_HREF_MAX)
        self.assertTrue(all(len(name) <= AB_ATTR_MAX for name in attributes))
        self.assertEqual(len(result["event_listener_types"]), AB_MAX_ATTRIBUTES)
        self.assertTrue(all(len(name) <= AB_ATTR_MAX for name in result["event_listener_types"]))

    async def test_selector_is_embedded_as_an_exact_js_string_literal(self) -> None:
        # repr() wrote "\U000e0001" for this tag character — not a JavaScript
        # escape — so the in-page query used a different selector. A JSON string
        # is valid JavaScript string-literal syntax, so decoding the embedded
        # literal as JSON must give back exactly the selector.
        selectors = ["[data-x='a\"b']", "[title='\U000e0001']", "a b", "[x='\\\\']"]
        for selector in selectors:
            fake = _FakeCDPSession(
                {
                    "DOM.getDocument": {"root": {"nodeId": 11}},
                    "DOM.querySelector": {"nodeId": 22},
                    "DOM.getAttributes": {"attributes": []},
                    "DOM.getBoxModel": {"model": {}},
                    "CSS.getComputedStyleForNode": {"computedStyle": []},
                    "Runtime.evaluate": {"result": {"value": []}},
                }
            )

            await CDPPassthrough(fake).get_element_intelligence(selector)

            expressions = [params["expression"] for method, params in fake.calls if method == "Runtime.evaluate"]
            self.assertEqual(len(expressions), 2)
            for expression in expressions:
                with self.subTest(selector=selector):
                    literal = re.search(r"document\.querySelector\((.*)\);", expression).group(1)
                    self.assertEqual(json.loads(literal), selector)

    async def test_dom_and_css_agents_are_enabled_before_their_calls(self) -> None:
        # On real Chromium, CSS.* calls fail until CSS.enable has run, and CSS.enable needs DOM.enable first.
        # (DOM.getDocument enables the DOM agent itself, so only the DOM.enable order is stricter than needed.)
        fake = _FakeCDPSession(
            {
                "DOM.getDocument": {"root": {"nodeId": 11}},
                "DOM.querySelector": {"nodeId": 22},
                "DOM.getAttributes": {"attributes": []},
                "DOM.getBoxModel": {"model": {}},
                "CSS.getComputedStyleForNode": {"computedStyle": []},
                "Runtime.evaluate": {"result": {"value": []}},
            }
        )

        await CDPPassthrough(fake).get_element_intelligence("#hero")

        order = [method for method, _ in fake.calls]
        self.assertIn("DOM.enable", order)
        self.assertIn("CSS.enable", order)
        self.assertLess(order.index("DOM.enable"), order.index("CSS.enable"))
        self.assertLess(order.index("CSS.enable"), order.index("CSS.getComputedStyleForNode"))
        self.assertLess(order.index("DOM.enable"), order.index("DOM.getDocument"))

    _READ_RESPONSES = {  # noqa: RUF012 - read only
        "DOM.getDocument": {"root": {"nodeId": 11}},
        "DOM.querySelector": {"nodeId": 22},
        "DOM.getAttributes": {"attributes": []},
        "DOM.getBoxModel": {"model": {}},
        "CSS.getComputedStyleForNode": {"computedStyle": []},
        "CSS.getMatchedStylesForNode": {"matchedCSSRules": []},
        "Runtime.evaluate": {"result": {"value": []}},
    }

    async def test_the_agents_are_disabled_when_the_read_is_done(self) -> None:
        # A CSS agent left on streams stylesheet events for the life of the cached passthrough.
        fake = _FakeCDPSession(dict(self._READ_RESPONSES))

        await CDPPassthrough(fake).get_element_intelligence("#hero")

        order = [method for method, _ in fake.calls]
        # Only the CSS agent goes off: DOM.disable would discard every node id the client holds, among them the
        # node_id this very read returns.
        self.assertEqual(order[-1], "CSS.disable")
        self.assertNotIn("DOM.disable", order)
        self.assertLess(order.index("CSS.getComputedStyleForNode"), order.index("CSS.disable"))

    async def test_the_agents_are_disabled_when_the_read_fails_and_a_failed_disable_is_not_raised(self) -> None:
        fake = _FakeCDPSession({"DOM.getDocument": RuntimeError("boom")}, fail=frozenset({"CSS.disable"}))

        result = await CDPPassthrough(fake).get_element_intelligence("#hero")

        self.assertEqual(result["error"], "cdp_element_intelligence_failed")
        order = [method for method, _ in fake.calls]
        self.assertEqual(order[-1], "CSS.disable")
        self.assertNotIn("DOM.disable", order)

    async def test_a_stalled_agent_enable_gives_up_and_still_cleans_up(self) -> None:
        fake = _FakeCDPSession(dict(self._READ_RESPONSES), hang=frozenset({"CSS.enable"}))

        with patch.object(cdp_passthrough, "CDP_AGENT_TIMEOUT_SECONDS", 0.05):
            result = await asyncio.wait_for(CDPPassthrough(fake).get_element_intelligence("#hero"), timeout=5)

        self.assertEqual(result["error"], "cdp_element_intelligence_failed")
        self.assertIn("CSS.disable", [method for method, _ in fake.calls])

    async def test_raw_css_commands_work_on_a_fresh_passthrough_and_switch_only_the_css_agent_off(self) -> None:
        for method in ("CSS.getComputedStyleForNode", "CSS.getMatchedStylesForNode"):
            fake = _FakeCDPSession(dict(self._READ_RESPONSES))

            with self.subTest(method=method):
                result = await CDPPassthrough(fake).raw_cdp_command(method, {"nodeId": 22})

                self.assertNotIn("error", result)
                self.assertEqual(
                    [name for name, _ in fake.calls],
                    ["DOM.enable", "CSS.enable", method, "CSS.disable"],
                )

    async def test_two_concurrent_reads_are_serialised_by_the_agents_lock(self) -> None:
        fake = _FakeCDPSession(dict(self._READ_RESPONSES))
        fake.gates["CSS.getComputedStyleForNode"] = asyncio.Event()
        passthrough = CDPPassthrough(fake)

        first = asyncio.create_task(passthrough.get_element_intelligence("#a"))
        await asyncio.wait_for(self._reached(fake, "CSS.getComputedStyleForNode"), timeout=5)
        second = asyncio.create_task(passthrough.get_element_intelligence("#b"))
        for _ in range(20):
            await asyncio.sleep(0)

        # The second read must not have switched the agents on (or off) under the first.
        self.assertEqual([name for name, _ in fake.calls].count("DOM.enable"), 1)
        self.assertNotIn("CSS.disable", [name for name, _ in fake.calls])

        fake.gates["CSS.getComputedStyleForNode"].set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

        order = [name for name, _ in fake.calls]
        self.assertEqual(order.count("DOM.enable"), 2)
        self.assertLess(order.index("CSS.disable"), len(order) - order[::-1].index("DOM.enable") - 1)

    @staticmethod
    async def _reached(fake: _FakeCDPSession, method: str) -> None:
        while method not in fake.reached:
            await asyncio.sleep(0)
        await fake.reached[method].wait()

    async def test_a_cancelled_read_does_not_wait_for_the_disable_and_still_switches_the_css_agent_off(self) -> None:
        fake = _FakeCDPSession(dict(self._READ_RESPONSES), hang=frozenset({"CSS.getComputedStyleForNode"}))
        passthrough = CDPPassthrough(fake)
        fake.gates["CSS.disable"] = asyncio.Event()  # the disable would hang the cancellation if it were awaited

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(passthrough.get_element_intelligence("#hero"), timeout=0.1)

        await asyncio.wait_for(self._reached(fake, "CSS.disable"), timeout=5)
        self.assertEqual(len(passthrough._background_tasks), 1)
        # The lock stays held until the background disable is done, so a next read cannot be switched off under.
        self.assertTrue(passthrough._agents_lock.locked())
        fake.gates["CSS.disable"].set()
        await asyncio.wait_for(asyncio.gather(*passthrough._background_tasks), timeout=5)
        self.assertFalse(passthrough._agents_lock.locked())
        self.assertNotIn("DOM.disable", [name for name, _ in fake.calls])

    async def test_other_raw_commands_do_not_touch_the_agents(self) -> None:
        fake = _FakeCDPSession(dict(self._READ_RESPONSES))

        await CDPPassthrough(fake).raw_cdp_command("DOM.getDocument", {"depth": 1})

        self.assertEqual([name for name, _ in fake.calls], ["DOM.getDocument"])

    async def test_the_agent_switches_are_not_reachable_through_the_allowlist(self) -> None:
        for blocked in ("DOM.enable", "CSS.enable", "CSS.disable", "DOM.disable"):
            with self.subTest(method=blocked):
                with self.assertRaisesRegex(ValueError, "not in the allowed list"):
                    await CDPPassthrough(_FakeCDPSession({})).raw_cdp_command(blocked)

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

"""MCP tool selection: MCP_TOOL_PROFILE picks a list of tool packs, MCP_TOOL_PACKS adjusts it.

The curated set is what most deployments serve and what clients cache, so its
names and their order are pinned here; lean is pinned the same way. Full is
every registered tool.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.app_factory import build_controller_services
from app.config import Settings
from app.models import McpToolCallRequest
from app.tool_gateway import McpToolGateway, ToolRegistry, ToolSpec
from app.tool_gateway.gateway import IMPLICIT_SESSION_CREATE_TOOLS
from app.tool_gateway.packs import register_all
from app.tool_gateway.selection import DEFAULT_PROFILE, PACKS, PROFILES, resolve_tool_selection, smallest_profile_with
from app.tool_inputs import EmptyInput

LEAN = [
    "browser.observe",
    "browser.screenshot",
    "browser.execute_action",
    "browser.request_human_takeover",
    "browser.close_session",
    "browser.get_html",
    "browser.find_elements",
]
# In the order tools/list returns them: clients and prompt caches key on it.
CURATED = [
    "browser.create_session",
    "browser.list_sessions",
    "browser.get_session",
    "browser.observe",
    "browser.screenshot",
    "browser.list_auth_profiles",
    "browser.list_downloads",
    "browser.read_download",
    "browser.list_tabs",
    "browser.activate_tab",
    "browser.close_tab",
    "browser.execute_action",
    "browser.save_auth_profile",
    "browser.request_human_takeover",
    "browser.close_session",
    "browser.fork_session",
    "browser.eval_js",
    "browser.wait_for_selector",
    "browser.get_html",
    "browser.find_elements",
]
TABS = ["browser.list_tabs", "browser.activate_tab", "browser.close_tab"]
CURATED_PACKS = ("browse", "sessions", "tabs", "downloads", "auth_profiles", "wait", "scripting", "vision")
# Every tool's pack. Moving a tool changes what operators' selections serve, so
# it is a deliberate edit here and in the docs table, never a side effect.
PACK_OF = {
    "browser.observe": "browse",
    "browser.screenshot": "browse",
    "browser.execute_action": "browse",
    "browser.request_human_takeover": "browse",
    "browser.close_session": "browse",
    "browser.get_html": "browse",
    "browser.find_elements": "browse",
    "browser.create_session": "sessions",
    "browser.list_sessions": "sessions",
    "browser.get_session": "sessions",
    "browser.fork_session": "sessions",
    "browser.list_tabs": "tabs",
    "browser.activate_tab": "tabs",
    "browser.close_tab": "tabs",
    "browser.list_downloads": "downloads",
    "browser.read_download": "downloads",
    "browser.list_auth_profiles": "auth_profiles",
    "browser.save_auth_profile": "auth_profiles",
    "browser.wait_for_selector": "wait",
    "browser.eval_js": "scripting",
    "browser.find_by_vision": "vision",
    "browser.get_console": "devtools",
    "browser.get_page_errors": "devtools",
    "browser.get_request_failures": "devtools",
    "browser.stop_trace": "devtools",
    "browser.get_network_log": "devtools",
    "browser.export_script": "devtools",
    "browser.get_remote_access": "deployment",
    "browser.readiness_check": "deployment",
    "browser.pii_scrubber_status": "deployment",
    "browser.verify_witness": "witness",
    "browser.export_witness_bundle": "witness",
    "browser.save_memory_profile": "memory_profiles",
    "browser.get_memory_profile": "memory_profiles",
    "browser.list_memory_profiles": "memory_profiles",
    "browser.delete_memory_profile": "memory_profiles",
    "browser.drag_drop": "advanced_actions",
    "browser.set_viewport": "advanced_actions",
    "browser.get_cookies": "storage",
    "browser.set_cookies": "storage",
    "browser.get_local_storage": "storage",
    "browser.set_local_storage": "storage",
    "browser.get_auth_profile": "auth_admin",
    "browser.save_auth_state": "auth_admin",
    "browser.cdp_attach": "session_admin",
    "browser.share_session": "session_admin",
    "browser.enable_shadow_browse": "session_admin",
    "browser.list_approvals": "approvals",
    "browser.approve_approval": "approvals",
    "browser.reject_approval": "approvals",
    "browser.execute_approval": "approvals",
    "browser.list_agent_jobs": "agent_jobs",
    "browser.get_agent_job": "agent_jobs",
    "browser.resume_agent_job": "agent_jobs",
    "browser.discard_agent_job": "agent_jobs",
    "browser.cancel_agent_job": "agent_jobs",
    "browser.queue_agent_step": "agent_jobs",
    "browser.queue_agent_run": "agent_jobs",
    "browser.list_providers": "agent_jobs",
    "browser.list_proxy_personas": "proxies",
    "browser.create_proxy_persona": "proxies",
    "browser.delete_proxy_persona": "proxies",
    "browser.list_cron_jobs": "cron",
    "browser.create_cron_job": "cron",
    "browser.delete_cron_job": "cron",
    "browser.trigger_cron_job": "cron",
    "harness.start_convergence": "harness",
    "harness.get_status": "harness",
    "harness.get_trace": "harness",
    "harness.list_runs": "harness",
    "harness.list_candidates": "harness",
    "harness.get_candidate": "harness",
    "harness.check_drift": "harness",
    "harness.check_all_drifts": "harness",
    "harness.graduate": "harness",
}


def _all_specs() -> dict[str, ToolSpec]:
    """Every registered tool, in registration order."""
    registry = ToolRegistry(tool_profile="full", experimental_enabled=lambda _: True)
    register_all(registry, MagicMock())
    return registry.tools


def _manager(**overrides):
    return SimpleNamespace(
        settings=SimpleNamespace(force_governed_approvals=False),
        ensure_session_accessible=AsyncMock(),
        list_sessions=AsyncMock(return_value=[]),
        create_session=AsyncMock(return_value={"id": "session-1"}),
        **overrides,
    )


def _gateway(manager=None, **kwargs) -> McpToolGateway:
    return McpToolGateway(
        manager=manager or _manager(),
        orchestrator=SimpleNamespace(),
        job_queue=SimpleNamespace(),
        **kwargs,
    )


def _names(gateway: McpToolGateway) -> list[str]:
    return [tool["name"] for tool in gateway.list_tools()]


class SelectionTests(unittest.TestCase):
    def test_profiles_are_nested_lists_of_packs(self) -> None:
        self.assertEqual(list(PROFILES), ["lean", "curated", "full"])
        self.assertEqual(PROFILES["lean"], ("browse",))
        self.assertEqual(PROFILES["curated"], CURATED_PACKS)
        self.assertEqual(PROFILES["full"], PACKS)
        self.assertEqual(len(set(PACKS)), len(PACKS))
        lean, curated, full = (set(PROFILES[name]) for name in PROFILES)
        self.assertLess(lean, curated)
        self.assertLess(curated, full)
        self.assertEqual(DEFAULT_PROFILE, "curated")

    def test_the_suggested_profile_is_the_smallest_with_the_pack(self) -> None:
        self.assertEqual(smallest_profile_with("browse"), "lean")
        self.assertEqual(smallest_profile_with("tabs"), "curated")
        self.assertEqual(smallest_profile_with("harness"), "full")

    def test_an_unknown_profile_warns_and_serves_curated(self) -> None:
        # Every release before packs served curated for any value but "full",
        # so a deployment running on a typo keeps starting. Exact names only:
        # reading FULL or "full " as full would widen its tools unasked.
        for value, hint in (
            ("leen", " (did you mean lean?)"),
            ("FULL", " (did you mean full?)"),
            ("full ", " (did you mean full?)"),
            ("Lean", " (did you mean lean?)"),
            ("minimal", ""),
        ):
            with self.subTest(value=value):
                with self.assertLogs("app.tool_gateway.selection", level="WARNING") as logs:
                    selection = resolve_tool_selection(value)
                self.assertEqual(
                    logs.output,
                    [
                        f"WARNING:app.tool_gateway.selection:MCP_TOOL_PROFILE={value!r} is not a tool profile"
                        f"{hint}; serving curated. Use lean, curated or full."
                    ],
                )
                self.assertEqual(selection.profile, "curated")
                self.assertEqual(selection.packs, frozenset(CURATED_PACKS))
        # Unset, or an empty MCP_TOOL_PROFILE= line, is no choice: curated, silently.
        for value in ("", "curated", "lean", "full"):
            with self.subTest(value=value), self.assertNoLogs("app.tool_gateway.selection", level="WARNING"):
                selection = resolve_tool_selection(value)
            self.assertEqual(selection.profile, value or "curated")

    def test_an_unknown_pack_stops_startup(self) -> None:
        every_pack = ", ".join(PACKS)
        for value, hint in (
            ("tab", " (did you mean tabs?)"),
            ("+tabs", " (did you mean tabs?)"),
            ("Tabs", " (did you mean tabs?)"),
            ("-", ""),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                resolve_tool_selection("curated", f"downloads,{value}")
            self.assertEqual(
                str(caught.exception),
                f"MCP_TOOL_PACKS: {value!r} is not a tool pack{hint}. "
                f"Name packs to add and -pack to remove one: {every_pack}.",
            )

    def test_a_profile_named_as_a_pack_says_where_it_goes(self) -> None:
        for value in ("full", "-full"):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                resolve_tool_selection("curated", value)
            self.assertEqual(
                str(caught.exception), "MCP_TOOL_PACKS: full is a tool profile, not a pack; set MCP_TOOL_PROFILE=full."
            )

    def test_removals_apply_after_additions_and_noops_are_not_turned_off(self) -> None:
        for packs in ("tabs,-tabs", "-tabs,tabs"):
            with self.subTest(packs=packs):
                selection = resolve_tool_selection("lean", packs)
                self.assertEqual(selection.packs, frozenset({"browse"}))
                self.assertEqual(selection.added, frozenset())
                self.assertEqual(selection.turned_off, frozenset({"tabs"}))
        # Removing a pack the selection lacks changes nothing, and is not
        # reported as something MCP_TOOL_PACKS turned off.
        selection = resolve_tool_selection("lean", "-harness")
        self.assertEqual(selection.packs, frozenset({"browse"}))
        self.assertEqual(selection.turned_off, frozenset())
        # Adding a pack twice, or one the profile has, adds it once or not at all.
        selection = resolve_tool_selection("lean", "tabs,tabs,browse")
        self.assertEqual(selection.packs, frozenset({"browse", "tabs"}))
        self.assertEqual(selection.added, frozenset({"tabs"}))
        # Items are trimmed and empty ones skipped; the log shows them as parsed.
        selection = resolve_tool_selection("lean", " tabs , ,downloads ")
        self.assertEqual(selection.packs, frozenset({"browse", "tabs", "downloads"}))
        self.assertEqual(selection.modifiers, ("tabs", "downloads"))
        self.assertEqual(selection.describe(), "lean profile with MCP_TOOL_PACKS=tabs,downloads")
        self.assertEqual(resolve_tool_selection("").describe(), "curated profile")

    def test_a_selection_of_nothing_stops_startup(self) -> None:
        with self.assertRaises(ValueError) as caught:
            resolve_tool_selection("lean", "-browse")
        self.assertEqual(
            str(caught.exception), "MCP_TOOL_PROFILE='lean' with MCP_TOOL_PACKS='-browse' selects no tool packs."
        )


class PackCatalogTests(unittest.TestCase):
    def test_every_tool_is_in_the_pinned_pack(self) -> None:
        packs = {name: spec.pack for name, spec in _all_specs().items()}
        self.assertEqual(len(PACK_OF), 75)
        self.assertEqual(packs, PACK_OF)
        self.assertEqual(set(packs.values()), set(PACKS))

    def test_a_tool_with_an_unknown_pack_is_refused(self) -> None:
        registry = ToolRegistry(tool_profile="full", experimental_enabled=lambda _: True)

        async def handler(_: EmptyInput):
            return {}

        with self.assertRaises(ValueError) as caught:
            registry.register(
                ToolSpec(name="test.one", description="", input_model=EmptyInput, handler=handler, pack="tab")
            )
        self.assertEqual(str(caught.exception), "test.one names an unknown tool pack: 'tab'")
        # A tool without a pack does not get one by default.
        with self.assertRaises(TypeError):
            ToolSpec(name="test.two", description="", input_model=EmptyInput, handler=handler)

    def test_a_withheld_tool_is_remembered_by_pack(self) -> None:
        registry = ToolRegistry(tool_profile="lean", experimental_enabled=lambda _: True)
        register_all(registry, MagicMock())
        self.assertEqual(registry.withheld_pack("browser.get_console"), "devtools")
        self.assertEqual(registry.withheld_pack("browser.list_tabs"), "tabs")
        self.assertIsNone(registry.withheld_pack("browser.observe"))
        self.assertIsNone(registry.withheld_pack("browser.nope"))


class GatewaySelectionTests(unittest.TestCase):
    def test_curated_names_and_order_are_unchanged(self) -> None:
        self.assertEqual(_names(_gateway()), CURATED)
        self.assertEqual(_names(_gateway(tool_profile="curated")), CURATED)
        self.assertEqual(_names(_gateway(tool_profile="")), CURATED)
        # find_by_vision registers last of the curated tools.
        self.assertEqual(_names(_gateway(vision_targeter=object())), [*CURATED, "browser.find_by_vision"])

    def test_full_serves_every_tool_in_registration_order(self) -> None:
        every = list(_all_specs())
        self.assertEqual(len(every), 75)
        self.assertEqual(_names(_gateway(tool_profile="full", vision_targeter=object())), every)
        self.assertEqual(
            _names(_gateway(tool_profile="full")), [name for name in every if name != "browser.find_by_vision"]
        )

    def test_lean_serves_the_browsing_loop(self) -> None:
        self.assertEqual(_names(_gateway(tool_profile="lean")), LEAN)
        self.assertLessEqual(set(LEAN), set(CURATED))

    def test_every_lean_tool_that_observes_acts_or_reads_can_start_a_session(self) -> None:
        # Lean has no create_session: these open one when none is live.
        self.assertLessEqual(
            {
                "browser.observe",
                "browser.execute_action",
                "browser.screenshot",
                "browser.get_html",
                "browser.find_elements",
            },
            IMPLICIT_SESSION_CREATE_TOOLS,
        )

    def test_packs_add_to_the_profile_in_registration_order(self) -> None:
        gateway = _gateway(tool_profile="lean", tool_packs="tabs")
        self.assertEqual(
            _names(gateway),
            [
                "browser.observe",
                "browser.screenshot",
                *TABS,
                "browser.execute_action",
                "browser.request_human_takeover",
                "browser.close_session",
                "browser.get_html",
                "browser.find_elements",
            ],
        )
        self.assertEqual(gateway.tool_selection.added, frozenset({"tabs"}))
        self.assertEqual(gateway.tool_profile, "lean")

    def test_a_minus_removes_a_pack(self) -> None:
        names = _names(_gateway(tool_packs="-scripting"))
        self.assertEqual(names, [name for name in CURATED if name != "browser.eval_js"])
        self.assertEqual(len(names), 19)

    def test_adding_vision_without_a_vision_model_warns(self) -> None:
        with self.assertLogs("app.tool_gateway.gateway", level="WARNING") as logs:
            gateway = _gateway(tool_profile="lean", tool_packs="vision")
        self.assertEqual(
            logs.output,
            [
                "WARNING:app.tool_gateway.gateway:MCP_TOOL_PACKS adds vision, but no vision model is configured "
                "(set ANTHROPIC_API_KEY): browser.find_by_vision is not listed"
            ],
        )
        self.assertEqual(_names(gateway), LEAN)
        # The default profile has vision too; without a key it drops out
        # quietly, as it always has.
        with self.assertNoLogs("app.tool_gateway.gateway", level="WARNING"):
            _gateway()
        with self.assertNoLogs("app.tool_gateway.gateway", level="WARNING"):
            gateway = _gateway(tool_profile="lean", tool_packs="vision", vision_targeter=object())
        self.assertEqual(_names(gateway), [*LEAN, "browser.find_by_vision"])

    def test_the_startup_log_names_the_selection(self) -> None:
        with self.assertLogs("app.tool_gateway.gateway", level="INFO") as logs:
            _gateway(tool_profile="lean", tool_packs="tabs")
            _gateway()
        self.assertEqual(
            logs.output,
            [
                "INFO:app.tool_gateway.gateway:MCP tools: lean profile with MCP_TOOL_PACKS=tabs, 10 tools listed",
                "INFO:app.tool_gateway.gateway:MCP tools: curated profile, 20 tools listed",
            ],
        )

    def test_settings_reach_the_gateway(self) -> None:
        services = build_controller_services(Settings(MCP_TOOL_PROFILE="lean", MCP_TOOL_PACKS="tabs"), version="test")
        self.assertEqual(set(_names(services.tool_gateway)), {*LEAN, *TABS})
        # A typo in the profile serves curated, as it always did, and says so.
        with self.assertLogs("app.tool_gateway.selection", level="WARNING") as logs:
            services = build_controller_services(Settings(MCP_TOOL_PROFILE="leen"), version="test")
        self.assertIn("MCP_TOOL_PROFILE='leen' is not a tool profile (did you mean lean?)", logs.output[0])
        self.assertEqual(_names(services.tool_gateway), CURATED)
        # A typo in the packs stops the controller before it serves anything.
        with self.assertRaisesRegex(ValueError, r"^MCP_TOOL_PACKS: 'tab' is not a tool pack"):
            build_controller_services(Settings(MCP_TOOL_PACKS="tab"), version="test")


class WithheldToolTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, name: str, manager=None, **kwargs):
        manager = manager or _manager()
        return await _gateway(manager, **kwargs).call_tool(McpToolCallRequest(name=name, arguments={}))

    async def _error(self, name: str, **kwargs) -> str:
        manager = _manager(get_console_messages=AsyncMock(), list_tabs=AsyncMock())
        response = await self._call(name, manager, **kwargs)
        self.assertTrue(response.isError)
        text = response.content[0].text
        self.assertEqual(response.structuredContent, {"error": text})
        # Refused before the handler runs, and without opening a session.
        manager.get_console_messages.assert_not_awaited()
        manager.list_tabs.assert_not_awaited()
        manager.create_session.assert_not_awaited()
        return text

    async def test_a_tool_outside_the_profile_names_its_pack_and_the_profile_that_has_it(self) -> None:
        cases = [
            (
                {},
                "browser.get_console",
                "browser.get_console is in the devtools tool pack, which this controller does not serve (it serves "
                "the curated profile). To use it, add devtools to MCP_TOOL_PACKS or set MCP_TOOL_PROFILE=full on "
                "the controller.",
            ),
            (
                {"tool_profile": "lean"},
                "browser.list_tabs",
                "browser.list_tabs is in the tabs tool pack, which this controller does not serve (it serves the "
                "lean profile). To use it, add tabs to MCP_TOOL_PACKS or set MCP_TOOL_PROFILE=curated on the "
                "controller.",
            ),
            (
                {"tool_profile": "lean", "tool_packs": "tabs"},
                "browser.get_console",
                "browser.get_console is in the devtools tool pack, which this controller does not serve (it serves "
                "the lean profile). To use it, add devtools to MCP_TOOL_PACKS or set MCP_TOOL_PROFILE=full on the "
                "controller.",
            ),
        ]
        for kwargs, name, expected in cases:
            with self.subTest(name=name, **kwargs):
                text = await self._error(name, **kwargs)
                self.assertEqual(text, expected)
                # Agents and mesh peers read this: it never echoes the operator's setting.
                self.assertNotIn("MCP_TOOL_PACKS=", text)

    async def test_create_session_on_lean_points_at_observe(self) -> None:
        self.assertEqual(
            await self._error("browser.create_session", tool_profile="lean"),
            "browser.create_session is in the sessions tool pack, which this controller does not serve (it serves "
            "the lean profile). To use it, add sessions to MCP_TOOL_PACKS or set MCP_TOOL_PROFILE=curated on the "
            "controller. Without it, browser.observe creates a session when none is live.",
        )
        # Without browse there is no observe to point at, and create_session is served.
        manager = _manager()
        response = await self._call("browser.create_session", manager, tool_packs="-browse")
        self.assertFalse(response.isError, response.content[0].text)
        manager.create_session.assert_awaited_once()

    async def test_a_pack_turned_off_says_which_removal_to_drop(self) -> None:
        self.assertEqual(
            await self._error("browser.eval_js", tool_packs="-scripting"),
            "browser.eval_js is in the scripting tool pack, which MCP_TOOL_PACKS turns off on this controller. "
            "To use it, remove -scripting from MCP_TOOL_PACKS.",
        )

    async def test_the_no_session_error_names_only_served_tools(self) -> None:
        cases = [
            (
                {},
                "browser.close_session",
                "browser.close_session needs a session and none are live — create one with browser.create_session "
                "(or call an observe/act tool, which creates one on demand).",
            ),
            (
                {"tool_profile": "lean"},
                "browser.close_session",
                "browser.close_session needs a session and none are live — call browser.observe, which creates one "
                "on demand.",
            ),
            (
                {"tool_profile": "lean", "tool_packs": "-browse,tabs"},
                "browser.list_tabs",
                "browser.list_tabs needs a session and none are live.",
            ),
            (
                {"tool_profile": "lean", "tool_packs": "-browse,tabs,wait"},
                "browser.list_tabs",
                "browser.list_tabs needs a session and none are live — call browser.wait_for_selector, which "
                "creates one on demand.",
            ),
        ]
        for kwargs, name, expected in cases:
            with self.subTest(**kwargs):
                manager = _manager()
                response = await self._call(name, manager, **kwargs)
                self.assertTrue(response.isError)
                self.assertEqual(response.structuredContent["code"], "no_session")
                self.assertEqual(response.structuredContent["error"], expected)
                manager.create_session.assert_not_awaited()

    async def test_unknown_tools_are_still_unknown(self) -> None:
        for kwargs in ({"tool_profile": "lean"}, {}):
            with self.subTest(**kwargs):
                self.assertEqual(
                    await self._error("browser.no_such_tool", **kwargs), "Unknown tool: browser.no_such_tool"
                )

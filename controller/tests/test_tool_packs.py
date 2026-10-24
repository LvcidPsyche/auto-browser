"""MCP tool selection: MCP_TOOL_PROFILE picks a list of tool packs, MCP_TOOL_PACKS adjusts it.

The curated set is what most deployments serve and what clients cache, so its
names and their order are pinned here; lean is pinned the same way. Full is
every registered tool.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.tool_gateway import ToolRegistry, ToolSpec
from app.tool_gateway.packs import register_all
from app.tool_gateway.selection import DEFAULT_PROFILE, PACKS, PROFILES, resolve_tool_selection, smallest_profile_with
from app.tool_inputs import EmptyInput

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

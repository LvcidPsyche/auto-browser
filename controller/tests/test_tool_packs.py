"""MCP tool selection: MCP_TOOL_PROFILE picks a list of tool packs, MCP_TOOL_PACKS adjusts it.

The curated set is what most deployments serve and what clients cache, so its
names and their order are pinned here; lean is pinned the same way. Full is
every registered tool.
"""

from __future__ import annotations

import unittest

from app.tool_gateway.selection import DEFAULT_PROFILE, PACKS, PROFILES, resolve_tool_selection, smallest_profile_with

CURATED_PACKS = ("browse", "sessions", "tabs", "downloads", "auth_profiles", "wait", "scripting", "vision")


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

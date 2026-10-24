"""Which MCP tools a controller serves: a profile, plus tool packs added or removed.

A pack is a named group of tools an operator switches on or off as a whole
(every ToolSpec names one). A profile is a named list of packs. The operator
picks a profile with MCP_TOOL_PROFILE and adjusts it with MCP_TOOL_PACKS
(``tabs,downloads`` adds, ``-scripting`` removes). Both are read once, when the
tool gateway is built. A name in MCP_TOOL_PACKS that is not a pack stops the
controller; an MCP_TOOL_PROFILE that is not a profile serves curated with a
warning, as every release before packs served curated for it without one.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Collection
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# In the order the pack table in docs/mcp-clients.md lists them: by the
# smallest profile that contains each pack.
PACKS = (
    "browse",
    "sessions",
    "tabs",
    "downloads",
    "auth_profiles",
    "wait",
    "scripting",
    "vision",
    "devtools",
    "deployment",
    "witness",
    "memory_profiles",
    "advanced_actions",
    "storage",
    "auth_admin",
    "session_admin",
    "approvals",
    "agent_jobs",
    "proxies",
    "cron",
    "harness",
)

# Smallest first: each profile contains the one before it.
PROFILES: dict[str, tuple[str, ...]] = {
    "lean": ("browse",),
    "curated": ("browse", "sessions", "tabs", "downloads", "auth_profiles", "wait", "scripting", "vision"),
    # Every pack, so a new pack joins full without a second edit.
    "full": PACKS,
}
DEFAULT_PROFILE = "curated"


@dataclass(frozen=True)
class ToolSelection:
    profile: str
    # MCP_TOOL_PACKS as the operator wrote it, one entry per item.
    modifiers: tuple[str, ...]
    packs: frozenset[str]
    # Packs MCP_TOOL_PACKS adds beyond the profile, and the packs its removals
    # actually took out (removing a pack that was never in is a no-op).
    added: frozenset[str]
    turned_off: frozenset[str]
    # Every pack MCP_TOOL_PACKS removes, taken out or not: adding one of these
    # does not serve it while the removal stays.
    removed: frozenset[str]
    # Every pack MCP_TOOL_PACKS names to add, whether or not the profile has it.
    requested: frozenset[str]

    def describe(self) -> str:
        """The selection as the operator configured it, for the startup log."""
        if not self.modifiers:
            return f"{self.profile} profile"
        return f"{self.profile} profile with MCP_TOOL_PACKS={','.join(self.modifiers)}"


def smallest_profile_with(pack: str) -> str:
    return next(name for name, packs in PROFILES.items() if pack in packs)


def _did_you_mean(value: str, options: Collection[str]) -> str:
    close = difflib.get_close_matches(value.strip().lower(), options, n=1)
    return f" (did you mean {close[0]}?)" if close else ""


def _profile_name(value: str) -> str:
    if not value.strip():
        # Unset, or an empty or blank MCP_TOOL_PROFILE= line: no choice was made.
        return DEFAULT_PROFILE
    if value in PROFILES:
        return value
    names = tuple(PROFILES)
    logger.warning(
        "MCP_TOOL_PROFILE=%r is not a tool profile%s; serving %s. Use %s or %s.",
        value,
        _did_you_mean(value, names),
        DEFAULT_PROFILE,
        ", ".join(names[:-1]),
        names[-1],
    )
    return DEFAULT_PROFILE


def _pack_name(item: str) -> str:
    name = item[1:].strip() if item.startswith("-") else item
    if name in PACKS:
        return name
    if name in PROFILES:
        fix = "MCP_TOOL_PROFILE picks the profile" if item.startswith("-") else f"set MCP_TOOL_PROFILE={name}"
        raise ValueError(f"MCP_TOOL_PACKS: {name} is a tool profile, not a pack; {fix}.")
    raise ValueError(
        f"MCP_TOOL_PACKS: {item!r} is not a tool pack{_did_you_mean(name, PACKS)}. "
        f"Name packs to add and -pack to remove one: {', '.join(PACKS)}."
    )


def resolve_tool_selection(profile: str, packs: str = "") -> ToolSelection:
    """The selection MCP_TOOL_PROFILE and MCP_TOOL_PACKS describe; ValueError for a name that is not a pack."""
    profile = _profile_name(profile)
    modifiers = tuple(item for item in (part.strip() for part in packs.split(",")) if item)
    adds = {_pack_name(item) for item in modifiers if not item.startswith("-")}
    removes = {_pack_name(item) for item in modifiers if item.startswith("-")}
    base = set(PROFILES[profile])
    wanted = base | adds
    # Removals apply after additions, whatever the order they are written in.
    selected = wanted - removes
    if not selected:
        raise ValueError(f"MCP_TOOL_PROFILE={profile!r} with MCP_TOOL_PACKS={packs!r} selects no tool packs.")
    return ToolSelection(
        profile=profile,
        modifiers=modifiers,
        packs=frozenset(selected),
        added=frozenset(adds - base - removes),
        turned_off=frozenset(wanted & removes),
        removed=frozenset(removes),
        requested=frozenset(adds),
    )


def withheld_tool_message(name: str, pack: str, selection: ToolSelection, served: Collection[str]) -> str:
    """What a call to a tool outside the selection returns: its pack, and what to change to serve it.

    Agents and mesh peers read this, so it names the profile but never echoes
    the operator's MCP_TOOL_PACKS value.
    """
    message = _how_to_serve(name, pack, selection)
    # An agent's first call is often to create_session, which lean and a
    # -sessions removal leave out: say what does the same job here.
    if name == "browser.create_session" and "browser.observe" in served:
        message += " Without it, browser.observe creates a session when none is live."
    return message


def _how_to_serve(name: str, pack: str, selection: ToolSelection) -> str:
    if pack in selection.turned_off:
        return (
            f"{name} is in the {pack} tool pack, which MCP_TOOL_PACKS turns off on this controller. "
            f"To use it, remove -{pack} from MCP_TOOL_PACKS."
        )
    if pack in selection.removed:
        # Never in the profile and removed as well: the removal wins over an
        # addition, so serving the pack takes both changes.
        return (
            f"{name} is in the {pack} tool pack, which this controller does not serve: the {selection.profile} "
            f"profile does not include it, and MCP_TOOL_PACKS removes it. To use it, remove -{pack} from "
            f"MCP_TOOL_PACKS and either add {pack} to it or set MCP_TOOL_PROFILE={smallest_profile_with(pack)} "
            "on the controller."
        )
    return (
        f"{name} is in the {pack} tool pack, which this controller does not serve "
        f"(it serves the {selection.profile} profile). To use it, add {pack} to MCP_TOOL_PACKS or set "
        f"MCP_TOOL_PROFILE={smallest_profile_with(pack)} on the controller."
    )

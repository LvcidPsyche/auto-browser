"""The -c flags of a Codex call (the controller's `cli` adapter), the features it looks at, and the MCP servers it
turns off.

codex-cli 0.114.0 starts the MCP servers named in each config layer it reads: the home's config.toml (CODEX_HOME,
else ~/.codex), /etc/codex/config.toml, /etc/codex/managed_config.toml, and a trusted project root's
.codex/config.toml, found from --cd up to a .git. A `-c mcp_servers={}` turns off none of them, since Codex merges a
-c table into its config key by key. So each call pins the project root to its own directory (CONFIG_OVERRIDES),
asks Codex which servers it would start (`codex <the call's -c flags> mcp list --json`), and turns off each of those
in one inline table (mcp_servers_off). Where that list shows a server on, the list runs once more with the table, and
a server it still shows on (the managed configuration's enabled = true outranks the table) stops the call.

A profile a layer selects, the managed configuration's own [features] table, an older name of a feature in the home
config's top-level [features] table, and a [features] table in /etc/codex/requirements.toml can each turn back on a
feature the call's -c settings turn off. So before anything else each call runs `codex <the call's -c flags> features
list`, and a feature the call turns off (LOOKED_AT_FEATURES) that the list shows on stops the call.

The look and the lists share with the call's exec the call's settings (CONFIG_FLAGS), its folder, its environment and
its deadline; the second list carries the server table besides, as the exec does, and the exec its --model where a
model is set.

scripts/codex_host_bridge.py holds a copy of each name here, since it runs without the controller;
tests/test_codex_mcp_servers.py keeps the two equal.
"""

from __future__ import annotations

import json
import re

CONFIG_OVERRIDES = (
    "project_doc_fallback_filenames=[]",
    "agents={}",
    "features.multi_agent=false",
    "features.apps=false",
    # No command tool: the decision needs none (the page goes in on stdin, the screenshot as --image).
    "features.shell_tool=false",
    "features.unified_exec=false",
    'web_search="disabled"',
    # The project root is the call's own new directory, so Codex reads no .codex/config.toml above it.
    "project_root_markers=[]",
    "notify=[]",  # no program run at the turn's end
    "features.codex_hooks=false",  # no hook of any hooks.json
    # No telemetry export: logs, traces, metrics.
    'otel.exporter="none"',
    'otel.trace_exporter="none"',
    'otel.metrics_exporter="none"',
    "project_doc_max_bytes=0",  # no AGENTS.md from the working directory or its parents
    "features.shell_snapshot=false",  # no login shell for a snapshot: it runs the home's .bash_profile and .bashrc
    "features.js_repl=false",  # no js_repl tool, and no Node (js_repl_node_path, else node) run to offer one
    "features.code_mode=false",  # no code_mode tool, and no Node run to offer one
    "features.apply_patch_freeform=false",  # no apply_patch tool from the feature (a model's catalog can bring one)
    "features.artifact=false",  # no artifacts tool
    "features.image_generation=false",  # no image_generation tool
    "features.request_permissions_tool=false",  # no request_permissions tool
)
# CONFIG_OVERRIDES as a Codex command line takes them, each after a -c.
CONFIG_FLAGS = tuple(word for override in CONFIG_OVERRIDES for word in ("-c", override))
MCP_LIST_COMMAND = ("mcp", "list", "--json")
FEATURES_LIST_COMMAND = ("features", "list")
# The one feature the call turns off that the look leaves out: on its own it gives the model no tool and starts nothing
# (it chooses the shape of the command tool that shell_tool brings, and the look refuses shell_tool), and a Codex newer
# than the pinned one lists it on whatever a call says.
FEATURE_NOT_LOOKED_AT = "unified_exec"
# The features the look reads: each the call turns off (a features.<name>=false of CONFIG_OVERRIDES) but that one.
LOOKED_AT_FEATURES = tuple(
    override.removeprefix("features.").removesuffix("=false")
    for override in CONFIG_OVERRIDES
    if override.startswith("features.")
    and override.endswith("=false")
    and override != f"features.{FEATURE_NOT_LOOKED_AT}=false"
)
# The shape of a feature's name in `codex features list`: a newer Codex lists a dotted name.
FEATURE_NAME = re.compile(r"[a-z0-9_.]+")
# What toml_basic_string writes for each character it escapes.
_TOML_ESCAPES = {ord('"'): '\\"', ord("\\"): "\\\\", **{code: "\\u%04x" % code for code in (*range(0x20), 0x7F)}}


def toml_basic_string(text: str) -> str:
    """``text`` as a TOML basic string: quote and backslash escaped, and each control character as a \\u escape."""
    return '"' + text.translate(_TOML_ESCAPES) + '"'


def mcp_servers_off(names: list[str]) -> str:
    """The -c value that turns off each named MCP server: `mcp_servers={"<name>"={enabled=false},...}`.

    One inline table holds for every name; the dotted form (`mcp_servers.<name>.enabled=false`) makes Codex exit 1
    for a name outside [a-zA-Z0-9_-].
    """
    return "mcp_servers={%s}" % ",".join(toml_basic_string(name) + "={enabled=false}" for name in dict.fromkeys(names))


def listed_mcp_servers(text: str) -> dict[str, bool] | None:
    """Each server `codex mcp list --json` printed (a JSON list of objects, each with a string "name"), by name, with
    whether it is on; None for anything else.

    A server counts as off only where its "enabled" is the JSON false: true, no "enabled" or any other value counts as
    on. A name listed more than once is on where any of its entries is.
    """
    try:
        listed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(listed, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in listed
    ):
        return None
    on = {entry["name"] for entry in listed if entry.get("enabled") is not False}
    return {entry["name"]: entry["name"] in on for entry in listed}


def features_on(text: str) -> list[str] | None:
    """Those of LOOKED_AT_FEATURES that `codex features list` printed as on, in their order; None where what it
    printed is no list of features.

    It counts as a list only where it is not blank and each of its non-empty lines is a row: a feature's name (one or
    more of a-z, 0-9, underscore and dot: FEATURE_NAME), its stage's words, then the word true or the word false. So a
    name in which run_cli's decode left a U+FFFD, for a byte that is no UTF-8, makes it no list. A feature is on where
    its row ends in true; of two rows for one name, the true one wins. A feature with no row under the call's name for
    it counts as off.
    """
    rows = [line.split() for line in text.splitlines() if line.strip()]
    if not rows or any(
        len(words) < 3 or not FEATURE_NAME.fullmatch(words[0]) or words[-1] not in ("true", "false") for words in rows
    ):
        return None
    on = {words[0] for words in rows if words[-1] != "false"}
    return [name for name in LOOKED_AT_FEATURES if name in on]


def features_on_error(names: list[str]) -> str:
    """What a call the look stops says: the features it found on (the call's own names for them), where a configuration
    can turn one on against the call, and what to do."""
    features, them = ("the feature", "it") if len(names) == 1 else ("the features", "them")
    return (
        f"the Codex configuration turns on {features} {', '.join(names)}, which a decision call needs off: turn {them} "
        f"off where the configuration turns {them} on, such as in a selected profile, in the managed configuration, in "
        "a requirements file, or under an older name in a [features] table"
    )

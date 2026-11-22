"""The lists each Codex call runs before its exec (app/codex_config.py, and its copy in scripts/codex_host_bridge.py),
`codex ... features list` and `codex ... mcp list --json`, as the tests see them: how they tell them, what a fake
answers to them, the names, listings and feature lists they check with, and the -c settings each of the call's
commands carries."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable
from pathlib import Path

from app.codex_config import FEATURES_LIST_COMMAND, MCP_LIST_COMMAND
from app.providers.base import CLIResult

# Server names Codex can list, the odd ones among them: a dot, a space, a quote, a backslash, a control character and
# characters outside ASCII and outside the Basic Multilingual Plane.
NAMES = ["home", "a.b", "a b", 'a"b', "x-y_1", "back\\slash", "tab\there", "ünï", "\U0001f600"]
# Each -c setting of a Codex call, in order, written out here: app/codex_config.py's CONFIG_OVERRIDES and the bridge's
# copy must each equal it, and the tests of each one's command lines expect them, in this order, as each command's -c
# values.
SETTINGS = (
    "project_doc_fallback_filenames=[]",
    "agents={}",
    "features.multi_agent=false",
    "features.apps=false",
    "features.shell_tool=false",
    "features.unified_exec=false",
    'web_search="disabled"',
    "project_root_markers=[]",
    "notify=[]",
    "features.codex_hooks=false",
    'otel.exporter="none"',
    'otel.trace_exporter="none"',
    'otel.metrics_exporter="none"',
    "project_doc_max_bytes=0",
    "features.shell_snapshot=false",
    "features.js_repl=false",
    "features.code_mode=false",
    "features.apply_patch_freeform=false",
    "features.artifact=false",
    "features.image_generation=false",
    "features.request_permissions_tool=false",
)
# The features the look at `codex features list` reads, written out here: each the call turns off but unified_exec.
LOOKED_AT = (
    "multi_agent",
    "apps",
    "shell_tool",
    "codex_hooks",
    "shell_snapshot",
    "js_repl",
    "code_mode",
    "apply_patch_freeform",
    "artifact",
    "image_generation",
    "request_permissions_tool",
)
_FEATURE_LISTS = Path(__file__).resolve().parent / "fixtures" / "codex_features_list"
# `codex <the call's -c flags> features list` as the image's codex-cli prints it (48 rows), and as a newer Codex prints
# it (175 rows: its unified_exec row reads true and it has no codex_hooks row), each captured with nothing in the
# configuration that turns a feature on: every feature of LOOKED_AT that has a row reads false.
PINNED_FEATURES_LIST = (_FEATURE_LISTS / "pinned.txt").read_text(encoding="utf-8")
NEWER_FEATURES_LIST = (_FEATURE_LISTS / "newer.txt").read_text(encoding="utf-8")
# The pinned list without its shell_tool row, for a row of shell_tool to go before.
WITHOUT_SHELL_TOOL = "".join(
    row + "\n" for row in PINNED_FEATURES_LIST.splitlines() if not row.startswith("shell_tool ")
)
# Rows of shell_tool on whose first word is of no feature name's shape (a-z, 0-9, underscore and dot): a list with one
# of them before WITHOUT_SHELL_TOOL is no list.
DECORATED_ROWS = {
    "a BOM before the name": "\ufeffshell_tool  stable  true\n",
    "another case": "Shell_Tool  stable  true\n",
    "a star before the name": "*shell_tool  stable  true\n",
    "a bullet word before the name": "- shell_tool  stable  true\n",
    "ANSI bold around the name": "\x1b[1mshell_tool\x1b[0m  stable  true\n",
    "a replacement character in the name": "shell\ufffdtool  stable  true\n",
    "a NUL in the name": "shell\x00_tool  stable  true\n",
    "a colon after the name": "shell_tool:  stable  true\n",
}
# What a look prints with a line whose first word is of no feature name's shape: each of DECORATED_ROWS before
# WITHOUT_SHELL_TOOL, and a note that ends in false before the pinned list. None of them is a list.
FIRST_WORD_NO_NAME = {
    **{case: row + WITHOUT_SHELL_TOOL for case, row in DECORATED_ROWS.items()},
    "a note that ends in false": "Note: shell_tool is false\n" + PINNED_FEATURES_LIST,
}
# The pinned list with blank lines and CRLF line ends: a list, as the pinned one is.
BLANK_LINES_AND_CRLF = "\n" + PINNED_FEATURES_LIST.replace("\n", "\r\n") + "\n\n"
# The row of shell_tool on with a byte in its name that is no UTF-8, before WITHOUT_SHELL_TOOL: such a byte arriving on
# the look's stdout.
NOT_UTF8_ROW = b"shell\xff_tool  stable  true\n" + WITHOUT_SHELL_TOOL.encode("utf-8")


def is_mcp_list(command: list[str]) -> bool:
    """Whether ``command`` is a Codex call's `mcp list --json`."""
    return tuple(command[-len(MCP_LIST_COMMAND) :]) == MCP_LIST_COMMAND


def is_features_list(command: list[str]) -> bool:
    """Whether ``command`` is a Codex call's `features list`."""
    return tuple(command[-len(FEATURES_LIST_COMMAND) :]) == FEATURES_LIST_COMMAND


def mcp_list_json(names: list[str], off: Iterable[str] = (), value: str | None = None) -> str:
    """`codex mcp list --json` as codex-cli 0.114.0 prints it: one object per server, its name among other keys, and
    "enabled" false for each of ``off``, else true; where ``value`` is given, each server's env holds it, as Codex
    prints env values."""
    off = set(off)
    transport = {"type": "stdio"} if value is None else {"type": "stdio", "env": {"TOKEN": value}}
    return json.dumps([{"name": name, "enabled": name not in off, "transport": transport} for name in names])


def features_list(on: Iterable[str] = (), *, listed: str = PINNED_FEATURES_LIST) -> str:
    """``listed`` (a `codex features list`) with the row of each of ``on`` reading true; a name of ``on`` with no row in
    ``listed`` raises ValueError."""
    on = set(on)
    rows = []
    for row in listed.splitlines():
        name, state = row.split()[0], row.split()[-1]
        rows.append(row[: -len(state)] + "true" if name in on else row)
    if missing := on - {row.split()[0] for row in rows}:
        raise ValueError(f"no row in the list for {sorted(missing)}")
    return "\n".join(rows) + "\n"


def config_values(command: list[str]) -> list[str]:
    """The value after each -c of ``command``."""
    return [command[index + 1] for index, word in enumerate(command[:-1]) if word == "-c"]


def turned_off(command: list[str]) -> set[str]:
    """The servers the -c mcp_servers table of ``command`` turns off; none where it has no table."""
    tables = [value for value in config_values(command) if value.startswith("mcp_servers=")]
    return {name for table in tables for name in tomllib.loads(table)["mcp_servers"]}


def listing(names: list[str], kept_on: Iterable[str] = (), value: str | None = None):
    """A Codex's answer to a call's `mcp list --json`, for a fake run_cli: it lists ``names`` (with ``value`` in each
    one's env, where it is given), each on but where the command's table turns it off, and each of ``kept_on`` on
    whatever the table says (as the managed configuration's enabled = true keeps a server on)."""
    kept_on = set(kept_on)

    def answer(command: list[str]) -> CLIResult:
        off = turned_off(command) - kept_on
        return CLIResult(command=command, stdout=mcp_list_json(names, off, value), stderr="", returncode=0)

    return answer


def no_mcp_servers(command: list[str]) -> CLIResult | None:
    """For a Codex call's `features list` and `mcp list --json`, what a Codex that turns on none of the call's
    features and names no MCP server prints (PINNED_FEATURES_LIST, an empty list); None for any other command."""
    if is_features_list(command):
        return CLIResult(command=command, stdout=PINNED_FEATURES_LIST, stderr="", returncode=0)
    if not is_mcp_list(command):
        return None
    return CLIResult(command=command, stdout="[]", stderr="", returncode=0)


def answers_mcp_list(fake):
    """A fake run_cli that answers each `features list` and `mcp list --json` as no_mcp_servers does and hands every
    other command to ``fake``."""

    async def run_cli(*, command, **kwargs):
        if (listed := no_mcp_servers(command)) is not None:
            return listed
        return await fake(command=command, **kwargs)

    return run_cli


class NoServers:
    """subprocess.Popen for `codex features list` and `codex mcp list --json` where none of the call's features is on
    and no MCP server is configured: they print PINNED_FEATURES_LIST and an empty list."""

    def __init__(self, command, **kwargs) -> None:
        self.command = self.args = command
        self.returncode = 0
        self.pid = 0

    def communicate(self, input=None, timeout=None):
        return (PINNED_FEATURES_LIST.encode("utf-8") if is_features_list(self.command) else b"[]"), b""


def listing_no_servers(fake):
    """Popen for a fake codex exec: each call's `codex features list` and `codex mcp list --json` come first, and turn
    on none of the call's features and list no server."""

    def popen(command, **kwargs):
        if is_mcp_list(command) or is_features_list(command):
            return NoServers(command, **kwargs)
        return fake(command, **kwargs)

    return popen

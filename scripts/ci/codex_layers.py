"""What the two Codex checks of scripts/ci (codex_bridge_mcp_check.py, codex_adapter_check.py) plant in the config
layers Codex reads, each in its own paths: an MCP server's table, the extras' keys (a notify program, the hooks
feature, the three telemetry exporters, project_doc_max_bytes), a hooks.json with a SessionStart hook and the model
provider; for the features case, a home config's top-level keys that turn on each feature `codex features list`
names, with a planted Node (and a planted zsh_path), and the shell files in the CLI's home; the same features turned on
in a profile the home config selects and in the managed configuration's [features] table, and a managed server with
enabled = true beside a home server with enabled = false; a feature's older names in the home config's top-level
[features] table; the settings that keep the command tools, the extras and those features' tools and programs off;
how `codex features list` is read (its rows, as the call's look reads them); what each command of a call is, and the
commands a decision and a refused call run; the tools a decision's model is offered; what a control's model must be
offered; what a refused call must show; the bundled skills a control leaves and each decision must keep; the model of
Codex's own catalog the patch case names, the files its patches add, and what its control and its decisions must show
of them; and /etc/codex/requirements.toml, moved aside and put back, or written with a [features] table and put back.
The programs and the notes are cli_stand_ins.py's.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import cli_stand_ins as stand_ins
from cli_stand_ins import fail

SYSTEM = Path("/etc/codex")
REQUIREMENTS = SYSTEM / "requirements.toml"
# The home's servers. The second has a name only a quoted TOML key can hold (a dot, a space, a quote): Codex lists it,
# so the call's table must carry it, but codex-cli 0.114.0 starts no server whose name is outside [a-zA-Z0-9_-].
HOME_SERVERS = ("home", 'odd "a.b c"')
COMMAND_TOOL_SETTINGS = ("features.shell_tool=false", "features.unified_exec=false")
# The command tools Codex can offer its model; a control's model must be offered one.
COMMAND_TOOLS = ("exec_command", "write_stdin", "shell", "shell_command", "local_shell")
# The tools a decision's model is offered, and no other: Codex's own, as codex-cli 0.114.0 offers them to the stand-in
# model with every MCP server and command tool off; where the home config names PATCH_MODEL, a model of its own
# catalog, it offers apply_patch as well, and the patch case holds that neither of the two patches the stand-in model
# sends there (each adding one file: one in the call's directory, one in the temp directory) writes its file under the
# default sandbox mode.
DECISION_TOOLS = ("request_user_input", "update_plan", "view_image")
# The model the home config names for the stand-in model, which is no model of Codex's own catalog.
STAND_IN_MODEL = "mock-model"
# A model of the pinned codex-cli's own catalog: its catalog entry brings Codex's file-edit tool.
PATCH_MODEL = "gpt-5.3-codex"
# The files the stand-in model's patches add in the patch case: one in the call's own directory, named by a path
# relative to it, and one in the temp directory, which Codex's workspace-write sandbox lets a call write.
PATCHED_IN_THE_CALL = "patched-in-the-call-directory.txt"
PATCHED_IN_TEMP = "patched-in-the-temp-directory.txt"
# The sandbox mode of the patch case's control, and Codex's words where it applied a patch; and where it refused one
# (codex-cli 0.114.0 answers each patch of a decision under the default sandbox mode with "patch rejected: writing
# outside of the project; rejected by user approval settings").
WORKSPACE_WRITE = "workspace-write"
WRITTEN = "Success. Updated the following files"
PATCH_REFUSED = "patch rejected"
EXTRAS_SETTINGS = (
    "notify=[]",
    "features.codex_hooks=false",
    'otel.exporter="none"',
    'otel.trace_exporter="none"',
    'otel.metrics_exporter="none"',
    "project_doc_max_bytes=0",
)
TELEMETRY = ("POST /v1/logs", "POST /v1/traces", "POST /v1/metrics")
# The settings that keep off the shell snapshot's login shell, js_repl's and code_mode's Node and tools, and the tools
# of apply_patch_freeform, artifact, image_generation and request_permissions_tool.
FEATURE_SETTINGS = (
    "features.shell_snapshot=false",
    "features.js_repl=false",
    "features.code_mode=false",
    "features.apply_patch_freeform=false",
    "features.artifact=false",
    "features.image_generation=false",
    "features.request_permissions_tool=false",
)
# The tools those features offer the model, as the stand-in model logs them, where the home config's top-level
# [features] table turns them on and the call lacks FEATURE_SETTINGS.
FEATURE_TOOLS = (
    "apply_patch",
    "artifacts",
    "code_mode",
    "image_generation",
    "js_repl",
    "js_repl_reset",
    "request_permissions",
)
# The shell files planted in the CLI's home, and the Node planted as js_repl_node_path, as their notes name them.
SHELL_FILES = (".bash_profile", ".bashrc")
NODE = "js_repl node"
RAN_WITH_THE_FEATURES = (*(f"{name} ran" for name in SHELL_FILES), f"{NODE} ran")
# The feature the features case leaves off, as it needs a zsh_path: its zsh_path case turns it on with the others and
# names ZSH, a planted program (as its note names it), as zsh_path, and a control without the call's command-tool
# settings is offered ZSH_FORK_TOOL; without a zsh_path, Codex refuses the session. How many of the last characters
# of what Codex said the line of that call prints.
ZSH_FORK = "shell_zsh_fork"
ZSH = "zsh_path program"
ZSH_FORK_TOOL = "shell_command"
ZSH_FORK_REFUSED = "zsh fork feature enabled, but `zsh_path` is not configured"
SAID_CHARS = 400
# The one feature the call turns off that its look at `codex features list` leaves out.
NOT_LOOKED_AT = "unified_exec"
# A feature's name in `codex features list`, as the call's look reads it: a-z, 0-9, underscore and dot.
FEATURE_NAME = re.compile(r"[a-z0-9_.]+")
# The older names a config layer's [features] table (the home config's, a profile's, the managed configuration's) can
# give a feature: a hand copy of the pinned codex-cli's list (codex-rs/core/src/features/legacy.rs at rust-v0.114.0),
# its eight names, none of which `codex features list` prints; the [features] table of /etc/codex/requirements.toml
# takes current names only. Each of these three, on alone in the home config's top-level [features] table, turns on a
# feature the look reads: by older name, that feature and the tool it offers the model.
OLDER_NAMES = {
    "connectors": ("apps", "search_tool_bm25"),
    "include_apply_patch_tool": ("apply_patch_freeform", "apply_patch"),
    "experimental_use_freeform_apply_patch": ("apply_patch_freeform", "apply_patch"),
}
# The list's other five older names, on together in that table: the call goes through.
OTHER_OLDER_NAMES = (
    "collab",
    "experimental_use_unified_exec_tool",
    "web_search",
    "memory_tool",
    "enable_experimental_windows_sandbox",
)
# The feature a [features] table of /etc/codex/requirements.toml turns on in its case.
REQUIRED_ON = "shell_tool"
# The profile the home config selects where a case turns features on in a profile.
PROFILE = "planted"
# The managed configuration's server with enabled = true, which outranks a call's table; and a server of the home's
# with enabled = false, planted beside it, so the call's first list shows one server off and the others on.
MANAGED_ON = "mgdon"
HOME_OFF = "homeoff"
# The commands of a Codex call, by their words after the codex path, its -c settings and its --model: its look, its
# list, its second list (run with the call's table of servers, -c mcp_servers=...) and its exec. A call refused at the
# look runs it alone; one refused at the second list (a server kept on) runs the look and both lists.
LOOK = "features list"
LIST = "mcp list"
TABLE_LIST = "mcp list with the table"
EXEC = "exec"
REFUSED_AT_THE_LOOK = (LOOK,)
REFUSED_AT_THE_SECOND_LIST = (LOOK, LIST, TABLE_LIST)
# What CODEX_HOME's hooks.json notes where its hook runs (the codex_hooks feature).
HOME_HOOK_RAN = "home hook ran"
# The AGENTS.md planted in the call's own directory, as the model's request holds it.
CALL_DOC = "planted-project-doc-of-the-call-directory"
# How long Codex gives each planted server to start (its startup_timeout_sec). Codex's own default is 10 seconds: under
# docker run --cpus 0.25 a control's servers took longer, and Codex stopped them before they noted their start.
SERVER_START_SECONDS = 60
IN_PLACE = f", with {REQUIREMENTS} in place"
NOT_IN_THE_IMAGE = f"=== no {REQUIREMENTS} in this image: no decision with it in place"


def server_table(label: str, notes: stand_ins.Notes) -> str:
    command, *args = stand_ins.server_command(label, notes.marker)
    return (
        f"[mcp_servers.{json.dumps(label)}]\ncommand = {json.dumps(command)}\nargs = {json.dumps(args)}\n"
        f"startup_timeout_sec = {SERVER_START_SECONDS}\n"
    )


def notify(notes: stand_ins.Notes) -> str:
    """A layer's notify key: the planted notify program."""
    return f"notify = {json.dumps(stand_ins.mark_command('notify', notes.extras))}\n"


def exporters(otel_port: int) -> str:
    """A layer's [otel] table: the three exporters, to the listener on ``otel_port``."""

    def exporter(signal: str) -> str:
        endpoint = f"http://127.0.0.1:{otel_port}/v1/{signal}"
        return f'{{ otlp-http = {{ endpoint = "{endpoint}", protocol = "json" }} }}'

    return (
        f"[otel]\nexporter = {exporter('logs')}\ntrace_exporter = {exporter('traces')}\n"
        f"metrics_exporter = {exporter('metrics')}\n"
    )


def extras(notes: stand_ins.Notes, otel_port: int) -> str:
    """The extras' keys of a config layer: a notify program, the hooks feature, the three exporters to the listener
    on ``otel_port`` and the project docs' size."""
    return notify(notes) + "project_doc_max_bytes = 32768\n[features]\ncodex_hooks = true\n" + exporters(otel_port)


def hooks(label: str, notes: stand_ins.Notes) -> str:
    command = shlex.join(stand_ins.mark_command(f"{label} hook", notes.extras))
    return json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}})


def provider(port: int, project: Path, keys: str = "", model: str = STAND_IN_MODEL) -> str:
    """A home's config.toml up to its servers: ``model`` as its model, the stand-in model on ``port`` as the model
    provider, ``keys`` (a layer's top-level keys and tables) and ``project`` a trusted project."""
    return (
        f'model = {json.dumps(model)}\nmodel_provider = "mock"\n' + keys + '[model_providers.mock]\nname = "mock"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\nwire_api = "responses"\n'
        f'[projects."{project}"]\ntrust_level = "trusted"\n'
    )


def feature_setting(setting: str) -> bool:
    """Whether a call's -c ``setting`` turns a feature off (features.<name>=false)."""
    return setting.startswith("features.") and setting.endswith("=false")


def call_features(settings: Iterable[str]) -> list[str]:
    """The features a call's -c ``settings`` turn off: the name of each features.<name>=false, in their order."""
    return [
        setting.removeprefix("features.").removesuffix("=false") for setting in settings if feature_setting(setting)
    ]


def looked_at(settings: Iterable[str]) -> list[str]:
    """The features a call's look at `codex features list` reads: each its ``settings`` turn off but NOT_LOOKED_AT."""
    return [name for name in call_features(settings) if name != NOT_LOOKED_AT]


def _row(words: list[str]) -> bool:
    """Whether a line's ``words`` are a row of `codex features list` as the call's look reads one: a name of
    FEATURE_NAME's shape, the stage's words, then true or false."""
    return len(words) >= 3 and bool(FEATURE_NAME.fullmatch(words[0])) and words[-1] in ("true", "false")


def feature_names(codex: str, env: dict[str, str], settings: Iterable[str]) -> list[str]:
    """The features `codex features list` names, run with ``env``, its output read as UTF-8 (a byte that is not, as
    U+FFFD); prints its exit code and how many rows (_row) it printed. The check fails where it exits non-zero, by its
    exit code, where it names no feature, names each non-empty line that is no row, and names each feature the call's
    ``settings`` turn off that it has no row for."""
    listed = subprocess.run(
        [codex, "features", "list"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=stand_ins.CALL_TIMEOUT_SECONDS,
        env=env,
    )
    lines = [line for line in listed.stdout.splitlines() if line.strip()]
    print(f"codex features list: exit {listed.returncode}; {sum(_row(line.split()) for line in lines)} features")
    if listed.returncode:
        fail(f"codex features list exited with {listed.returncode}: {listed.stderr[-1500:]}")
    if not lines:
        fail(f"codex features list named no feature: exit {listed.returncode}; {listed.stderr[-1500:]}")
    if not_rows := [line for line in lines if not _row(line.split())]:
        fail(f"codex features list printed lines that are no row of a feature: {not_rows}")
    names = [line.split()[0] for line in lines]
    if missing := [name for name in call_features(settings) if name not in names]:
        fail(f"codex features list has no row for {missing}, which the call's settings turn off")
    return names


def node_keys(node: Path) -> str:
    """A home config's top-level keys that name ``node`` as js_repl_node_path and turn on
    experimental_use_freeform_apply_patch."""
    return f"js_repl_node_path = {json.dumps(str(node))}\nexperimental_use_freeform_apply_patch = true\n"


def _turned_on(names: Iterable[str], *, zsh_fork: bool = False) -> str:
    """A [features] table's lines that turn on each of ``names`` but ZSH_FORK (that one too where ``zsh_fork``)."""
    return "".join(f"{name} = true\n" for name in names if zsh_fork or name != ZSH_FORK)


def features_on(names: Iterable[str], node: Path, *, zsh_fork: bool = False, zsh_path: Path | None = None) -> str:
    """A home config's top-level keys for the features case: js_repl_node_path names ``node``,
    experimental_use_freeform_apply_patch is on, zsh_path names ``zsh_path`` where it is given, and the [features]
    table turns on each of ``names`` but ZSH_FORK (that one too where ``zsh_fork``)."""
    zsh = f"zsh_path = {json.dumps(str(zsh_path))}\n" if zsh_path else ""
    return f"{node_keys(node)}{zsh}[features]\n{_turned_on(names, zsh_fork=zsh_fork)}"


def features_in_a_profile(names: Iterable[str], node: Path) -> str:
    """A home config's top-level keys that select PROFILE, with the features case's js_repl_node_path (``node``) and
    experimental_use_freeform_apply_patch, and PROFILE's [features] table that turns on each of ``names`` but
    ZSH_FORK."""
    return f"profile = {json.dumps(PROFILE)}\n{node_keys(node)}[profiles.{PROFILE}.features]\n{_turned_on(names)}"


def managed_features(names: Iterable[str], notes: stand_ins.Notes) -> str:
    """/etc/codex/managed_config.toml with its "mgd" server and a [features] table that turns on each of ``names``
    but ZSH_FORK."""
    return server_table("mgd", notes) + f"[features]\n{_turned_on(names)}"


def features_table(names: Iterable[str]) -> str:
    """A home config's top-level [features] table that turns on each of ``names`` (older names among them)."""
    return f"[features]\n{_turned_on(names)}"


def managed_server_on(notes: stand_ins.Notes) -> str:
    """/etc/codex/managed_config.toml with its "mgd" server and MANAGED_ON, which names enabled = true."""
    return server_table("mgd", notes) + server_table(MANAGED_ON, notes) + "enabled = true\n"


def server_off(notes: stand_ins.Notes) -> str:
    """A home config's table of HOME_OFF, which names enabled = false."""
    return server_table(HOME_OFF, notes) + "enabled = false\n"


def _settings(words: list[str]) -> list[str]:
    """The value of each -c setting among ``words``."""
    pairs = iter(words)
    return [next(pairs) for word in pairs if word == "-c"]


def command_kind(words: list[str]) -> str:
    """What a command of a Codex call is, from ``words``, its words after the codex path: LOOK, LIST, TABLE_LIST or
    EXEC; one that is none of them is named by its words without its -c settings."""
    rest = without(words, lambda value: True)
    rest = rest[2:] if rest[:1] == ["--model"] else rest
    if rest[:2] == ["features", "list"]:
        return LOOK
    if rest[:3] == ["mcp", "list", "--json"]:
        return TABLE_LIST if any(value.startswith("mcp_servers=") for value in _settings(words)) else LIST
    return EXEC if rest[:1] == ["exec"] else " ".join(rest)


def decision_children() -> tuple[str, ...]:
    """The commands a decision of the checks runs, in order: its look, its list, its second list, as each case
    plants servers that are on, and its exec. With the image's /etc/codex/requirements.toml in place (its
    mcp_servers = {} allows no server) Codex lists each planted server off, and the call makes no second list."""
    lists = (LIST,) if REQUIREMENTS.exists() else (LIST, TABLE_LIST)
    return (LOOK, *lists, EXEC)


def node_path(notes: stand_ins.Notes) -> Path:
    """Where plant_programs plants the Node."""
    return notes.work / "node"


def plant_programs(home: Path, notes: stand_ins.Notes) -> Path:
    """Plants the shell files in ``home`` (the CLI's HOME) and the Node in ``notes``' directory, each noting its run
    with the extras' notes; returns the Node's path. A shell file notes its run with the shell's own echo, not with a
    stand-in program: Codex ends the login shell when its call ends, soon after the stand-in model answers, and with a
    program started from the first file the second was not read before then."""
    for name in SHELL_FILES:
        line = f"echo {shlex.quote(f'{name} ran')} >> {shlex.quote(str(notes.extras))}\n"
        (home / name).write_text(line, encoding="utf-8")
    return stand_ins.node_program(NODE, notes.extras, node_path(notes))


def zsh_program(notes: stand_ins.Notes) -> Path:
    """Plants ZSH, for zsh_path, in ``notes``' directory, noting its run with the extras' notes; returns its path."""
    return stand_ins.mark_program(ZSH, notes.extras, notes.work / "zsh")


def without(command: list[str], taken_out: Callable[[str], bool]) -> list[str]:
    """``command`` without each -c setting whose value ``taken_out`` names."""
    kept, words = [], iter(command)
    for word in words:
        if word == "-c":
            value = next(words)
            kept += [] if taken_out(value) else [word, value]
        else:
            kept.append(word)
    return kept


def plant_call_doc(where: str, name: str = "AGENTS.md") -> None:
    (Path(where) / name).write_text(CALL_DOC + "\n", encoding="utf-8")


def extras_seen(
    notes: stand_ins.Notes, requests: list[dict], in_the_request: tuple[str, ...]
) -> tuple[list, list, list]:
    """What ran, what the listener got and which of ``in_the_request`` the model's requests hold."""
    return stand_ins.noted(notes.extras), stand_ins.noted(notes.telemetry), stand_ins.held(requests, in_the_request)


def command_tools(requests: list[dict]) -> list[str]:
    return [name for name in stand_ins.offered(requests) if name in COMMAND_TOOLS]


def offered_in_the_control(requests: list[dict], servers: Iterable[str], without: str) -> None:
    """Fails unless the control's model was offered the tool of each of ``servers`` and a command tool: else a
    decision's verdict on the tools offered would prove nothing. ``without`` names the settings the control lacks."""
    if missing := [tool for tool in map(stand_ins.server_tool, servers) if tool not in stand_ins.offered(requests)]:
        fail(f"the model was not offered {missing} without {without}: the check would prove nothing")
    if not command_tools(requests):
        fail(f"the model was offered no command tool without {without}: the check would prove nothing")


def features_in_the_control(requests: list[dict], ran: list[str], without: str) -> None:
    """Fails unless the features control's model was offered each of FEATURE_TOOLS and the shell files and the Node
    ran (Codex's own DECISION_TOOLS are not required here): else a decision's verdicts on its tools and on what ran
    would prove nothing. ``without`` names the settings the control lacks."""
    if missing := [tool for tool in FEATURE_TOOLS if tool not in stand_ins.offered(requests)]:
        fail(f"the model was not offered {missing} without {without}: the check would prove nothing")
    if missing := [mark for mark in RAN_WITH_THE_FEATURES if mark not in ran]:
        fail(f"{missing} not noted without {without}: the check would prove nothing")


def features_on_in_the_control(requests: list[dict], ran: list[str], without: str) -> None:
    """Fails unless a control with the features turned on in a profile or the managed configuration was offered what
    features_in_the_control names, and ran those programs and CODEX_HOME's hook too. ``without`` names what the control
    lacks."""
    features_in_the_control(requests, ran, without)
    if HOME_HOOK_RAN not in ran:
        fail(f"{[HOME_HOOK_RAN]} not noted without {without}: the check would prove nothing")


def tool_in_the_control(requests: list[dict], tools: Iterable[str], without: str) -> None:
    """Fails unless a control's model was offered one of ``tools``: else a refused call would prove nothing. ``without``
    names what the control lacks."""
    tools = list(tools)
    if not set(tools) & set(stand_ins.offered(requests)):
        fail(f"the model was offered none of {tools} without {without}: the check would prove nothing")


def server_on_in_the_control(started: list[str], without: str) -> None:
    """Fails unless a control started MANAGED_ON. ``without`` names what the control lacks."""
    if f"{MANAGED_ON} started" not in started:
        what = f"the managed server {MANAGED_ON} with enabled = true"
        fail(f"{what} did not start without {without}: the check would prove nothing")


def refused(side: str, why: str, outcome: str, expected: str, notes: stand_ins.Notes) -> None:
    """Prints the line of a call the ``side`` ("adapter" or "bridge") must refuse, planted as ``why`` says; fails
    unless it was refused with ``expected`` (the code's own words), started no MCP server, asked the model nothing, and
    ran and sent nothing planted, read from ``notes``. The commands it ran (no exec among them) are held by its
    caller."""
    started, requests, ran, sent = _notes(notes)
    asked = bool(requests)
    print(
        f"refused: {outcome == expected}; servers: {started or 'none started'}; model asked: {asked}; ran: {ran}; "
        f"telemetry: {sent}"
    )
    if started:
        fail(f"the {side}'s Codex started MCP servers: {started}")
    if outcome != expected or requests or ran or sent:
        others = [name for name in stand_ins.offered(requests) if name not in DECISION_TOOLS]
        besides = ", ".join(DECISION_TOOLS)
        fail(
            f"the {side}'s call {why} was not refused: {outcome}; tools offered besides {besides}: {others}; ran: {ran}"
            f"; telemetry: {sent}"
        )


def _notes(notes: stand_ins.Notes) -> tuple[list[str], list[dict], list[str], list[str]]:
    """What ``notes`` hold: the servers started, the model's requests, the planted programs that ran, what the
    listener got."""
    return (
        stand_ins.noted(notes.marker),
        stand_ins.logged(notes.model_log),
        stand_ins.noted(notes.extras),
        stand_ins.noted(notes.telemetry),
    )


def none_after_the_wait(side: str, case: str, calls: int, notes: stand_ins.Notes) -> None:
    """Prints what ``notes`` hold once more after the wait, where a case's ``calls`` calls, each refused and its notes
    read at once, have noted; fails, naming the case (``case``), where one of those calls started an MCP server, asked
    the model, or ran or sent anything planted."""
    started, requests, ran, sent = _notes(notes)
    print(f"servers: {started or 'none started'}; model asked: {bool(requests)}; ran: {ran}; telemetry: {sent}")
    if started or requests or ran or sent:
        tools = stand_ins.offered(requests)
        fail(
            f"one of the {side}'s {calls} calls {case} started, asked or ran something, as the notes read after the "
            f"wait show: servers: {started}; tools offered: {tools}; ran: {ran}; telemetry: {sent}"
        )


def refused_as(side: str, why: str, kind: str, final: str) -> None:
    """Prints how the ``side``'s call planted as ``why`` says was refused (``kind``); fails unless it is ``final``, the
    kind of refusal the ``side`` gives a call no retry mends."""
    print(f"refused as: {kind}")
    if kind != final:
        fail(f"the {side}'s call {why} was refused as {kind}, not as {final}")


def zsh_fork_refused(side: str, outcome: str, decided: bool, said: str, notes: stand_ins.Notes) -> None:
    """Prints the line of the ``side``'s call with ZSH_FORK on and no zsh_path, and the last SAID_CHARS of what Codex
    said (``said``), its notes read from ``notes`` (after the wait); fails where the call made a decision
    (``decided``), started an MCP server, or ran or sent anything planted, and unless the model was never asked and
    what Codex said names the zsh_path it lacks."""
    started, requests, ran, sent = _notes(notes)
    refused = ZSH_FORK_REFUSED in said
    asked = bool(requests)
    print(f"=== with {ZSH_FORK} on and no zsh_path: {outcome}; model asked: {asked}; for want of zsh_path: {refused}")
    print(f"servers: {started or 'none started'}; ran: {ran}; telemetry: {sent}")
    print(f"Codex said, last: {said[-SAID_CHARS:]!r}")
    if decided:
        fail(f"with {ZSH_FORK} on and no zsh_path, the {side}'s call made a decision: {outcome}")
    if started:
        fail(f"the {side}'s Codex started MCP servers: {started}")
    if ran or sent:
        fail(f"the {side}'s Codex ran, sent or passed on planted extras: {ran + sent}")
    if requests or not refused:
        fail(f"with {ZSH_FORK} on as well, Codex did not refuse the session for want of a zsh_path: {outcome}")


def patch_targets(temp: Path) -> tuple[str, str]:
    """The files the stand-in model's patches add in the patch case: PATCHED_IN_THE_CALL, by a path relative to the
    call's directory, and PATCHED_IN_TEMP in ``temp``."""
    return PATCHED_IN_THE_CALL, str(temp / PATCHED_IN_TEMP)


def patched_files(in_the_call: list[bool], temp: Path) -> list[str]:
    """Which of the patch case's files are there: PATCHED_IN_THE_CALL where a call's directory held it once its exec
    had returned (``in_the_call``, read before the call removed the directory), and PATCHED_IN_TEMP in ``temp``."""
    in_temp = temp / PATCHED_IN_TEMP
    there = {PATCHED_IN_THE_CALL: any(in_the_call), str(in_temp): in_temp.exists()}
    return [name for name, found in there.items() if found]


def patch_control(requests: list[dict], written: list[str], targets: tuple[str, ...], under: str) -> None:
    """Prints what the patch case's control, a call made ``under`` workspace-write, was offered, how Codex answered its
    patches and which of their files are there (``written``); fails unless its model was offered PATCH_TOOL, Codex
    answered each of the patches for ``targets`` as WRITTEN, and each of ``targets`` is there: else a decision's
    verdicts on its patches would prove nothing."""
    offered, answers = stand_ins.offered(requests), stand_ins.patched(requests)
    print(f"tools offered: {offered}; Codex answered the patches: {answers}; written: {written}")
    if stand_ins.PATCH_TOOL not in offered:
        fail(f"the model was not offered {stand_ins.PATCH_TOOL} {under}: {offered}; the check would prove nothing")
    if len(answers) != len(targets) or not all(WRITTEN in answer for answer in answers):
        fail(f"Codex did not answer each patch as written {under}: {answers}; the check would prove nothing")
    if missing := [target for target in targets if target not in written]:
        fail(f"the model's patches did not write {missing} {under}: the check would prove nothing")


def patches_held(
    side: str, requests: list[dict], in_the_call: list[bool], temp: Path, targets: tuple[str, ...]
) -> None:
    """Prints how Codex answered a decision's patches and which of their files are there (patched_files, from
    ``in_the_call`` and ``temp``); fails, naming them, where one is there, where the check never read the call's
    directory after an exec (``in_the_call`` is empty), where Codex did not answer the patch of each of ``targets``,
    where an answer says the file was written (WRITTEN), and where an answer is not Codex's refusal (PATCH_REFUSED).
    ``side`` is "adapter" or "bridge"."""
    answers, written = stand_ins.patched(requests), patched_files(in_the_call, temp)
    print(f"Codex answered the patches: {answers}; written: {written}")
    if written:
        fail(f"the {side}'s Codex wrote the files of the model's patches: {written}; Codex answered: {answers}")
    if not in_the_call:
        fail(f"the check read no directory of the {side}'s call after an exec: it cannot say what a patch wrote there")
    if len(answers) != len(targets):
        fail(f"Codex did not answer each of the model's patches: {answers}")
    if said := [answer for answer in answers if WRITTEN in answer]:
        fail(f"Codex answered a patch of the model's as written: {said}")
    if others := [answer for answer in answers if PATCH_REFUSED not in answer]:
        fail(f"Codex answered a patch of the model's with no refusal ({PATCH_REFUSED!r}): {others}")


def show(ran: list[str], sent: list[str], held: list[str], skills: Path) -> None:
    print(f"ran: {ran}; telemetry: {sent}; in the request: {held}; bundled skills: {skills.exists()}")


def _listing(folder: Path) -> list[str]:
    """What ``folder`` holds, recursively, each name relative to it, sorted; none where it is not a directory."""
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*")) if folder.is_dir() else []


def skills_left(skills: Path, note: Path, without: str) -> None:
    """Notes in ``note`` what a control left in ``skills``, the folder Codex writes its bundled skills into as it
    starts (_listing), for each decision after it; fails where it left nothing. ``without`` names the settings the
    control lacks."""
    left = _listing(skills)
    if not left:
        fail(f"no bundled skill was written in {skills} without {without}")
    note.write_text(json.dumps(left), encoding="utf-8")


def skills_kept(skills: Path, note: Path, side: str) -> None:
    """Fails unless ``skills`` holds what the control left there (noted in ``note``), naming each name that went and
    each that came. ``side`` is "adapter" or "bridge"."""
    left, now = json.loads(note.read_text(encoding="utf-8")), _listing(skills)
    went, came = [name for name in left if name not in now], [name for name in now if name not in left]
    if went or came:
        fail(f"the {side}'s Codex changed the home's bundled skills in {skills}: went {went}; came {came}")


@contextmanager
def requirements_aside() -> Iterator[bool]:
    """The image's /etc/codex/requirements.toml moved aside for the block and put back after it, whatever its outcome;
    yields whether the image has one. With it in place Codex turns off every planted server by itself, so a control
    would start none."""
    if not REQUIREMENTS.exists():
        yield False
        return
    aside = REQUIREMENTS.with_name("requirements.toml.aside")
    REQUIREMENTS.rename(aside)
    try:
        yield True
    finally:
        aside.rename(REQUIREMENTS)


@contextmanager
def requirements_turning_on(names: Iterable[str]) -> Iterator[None]:
    """/etc/codex/requirements.toml written for the block: the image's own text where the image has the file, then a
    [features] table that turns on each of ``names``; what was there put back after the block, whatever its outcome
    (the file removed where there was none)."""
    kept = REQUIREMENTS.read_text(encoding="utf-8") if REQUIREMENTS.exists() else None
    REQUIREMENTS.write_text((kept + "\n" if kept else "") + features_table(names), encoding="utf-8")
    try:
        yield
    finally:
        if kept is None:
            REQUIREMENTS.unlink()
        else:
            REQUIREMENTS.write_text(kept, encoding="utf-8")

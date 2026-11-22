"""The Codex host bridge starts no MCP server, offers its model no tool but Codex's own update_plan, request_user_input
and view_image (and apply_patch where the home config names codex_layers.PATCH_MODEL, a model of Codex's own catalog:
neither of the two patches the stand-in model sends there, each adding one file, writes it), and runs none of the extras
a config layer names and no shell file, Node or zsh_path program of a feature the home config's top-level [features]
table turns on; it refuses a call, with no exec, where the managed configuration keeps a server on, or where a selected
profile, the managed configuration, an older name in the home config's [features] table or a [features] table of
/etc/codex/requirements.toml turns on a feature the call turns off; end to end with the image's codex-cli: the
agent-cli-image workflow runs it in the controller image built with INSTALL_AGENT_CLIS=true, as root, with scripts/ at
/scripts.

An MCP server is planted in each config layer Codex reads: /etc/codex/config.toml, /etc/codex/managed_config.toml, the
home's config.toml (two there, one with a name only a quoted TOML key can hold), and a trusted project root's
.codex/config.toml above the bridge's call directory (the temp directory, given a .git). The extras: the home's
config.toml also names a notify program, turns on the codex_hooks feature, sends the three telemetry exporters to a
listener, and names project_doc_max_bytes; a hooks.json with a SessionStart hook is in the home's .codex, /etc/codex and
the project root's .codex; an AGENTS.md is in the temp directory and, planted as each command of the call runs, in the
call's own directory.

The image's /etc/codex/requirements.toml, which a host does not have, is moved aside. A control `codex exec` with the
bridge's flags but neither of its two MCP settings, neither of its two command-tool settings and none of its extras
settings starts the servers of every layer but the home's odd-named one (which codex-cli 0.114.0 lists and does not
start), offers the model the tool of each server it started and a command tool, runs the notify program and the three
hooks, sends to the three exporters, passes both AGENTS.md to the model, and writes Codex's bundled skills into the
home's .codex/skills/.system; the check measures how long after it returned every one of those notes was there. Then the
bridge's own CodexBridgeService makes one decision, which must start none, offer the model those three tools of Codex's
own and no other, run and send none of the extras, leave the bundled skills folder holding what the control left there
(each name in it, recursively), and come back. Then the file is put back where it was, and the bridge makes one more
decision with everything still planted, which must do the same; where the image has no such file, the check says so. A
decision's notes are read SETTLE_SECONDS after its call returned, or SETTLE_FACTOR times the control's measure where
that is longer (cli_stand_ins.py's).

The features case. The check asks Codex for its features (`codex features list`) and writes the home's config.toml
again: its servers, and in place of the extras' keys, top-level keys that turn on each of those features but
shell_zsh_fork in the [features] table, name a planted Node as js_repl_node_path and turn on
experimental_use_freeform_apply_patch; a .bash_profile and a .bashrc are planted in HOME. With the file moved aside
again, a control, the bridge's call without its look at `codex features list` (which would refuse a call with its
settings taken out) and its feature settings, offers the model the tools of apply_patch_freeform, artifact, code_mode,
image_generation, js_repl and request_permissions_tool besides those three, and runs both shell files (the shell
snapshot's login shell) and the Node. Then the bridge makes one decision, and one more with the file put back, each held
to what the decisions above are held to, its notes read after the longer of the two controls' waits. Then the zsh_path
case: shell_zsh_fork turned on in that table as well, and a planted program named as zsh_path. With the file moved
aside, a control, the bridge's call without its look and its command-tool settings, is offered shell_command; then the
bridge makes one decision, and one more with the file put back, each held to what the decisions above are held to (the
zsh_path program among what must not run). Last, with the zsh_path taken out of that config.toml, the bridge's call
makes no decision, starts no server, runs and sends nothing planted, its model is never asked, and Codex says that
zsh_path is not configured; the line prints the end of the bridge's log. The adapter check's settings case is not
repeated here, as the unit tests hold the bridge's settings to the list the controller's are held to. The check fails,
by its exit code, where `codex features list` exits non-zero, and by name where it prints a line that is no row as the
call's look reads one (a name of a-z, 0-9, underscore and dot, its stage's words, then true or false), and where it has
no row for a feature the bridge's settings turn off.

The later cases, each with the file moved aside, then put back where the image has it. A refused call must be refused
with the bridge's own words and status 409 (REFUSED, which the controller's client does not retry), start no server, ask
the stand-in model nothing, run and send nothing planted, and have run, of the commands its service runs, the look
alone, or where a server is kept on the look and both lists, and no exec. The managed server case:
/etc/codex/managed_config.toml also names a server with enabled = true, and the home's config.toml one with enabled =
false, so the call's first list shows one server off and the others on. A control, the bridge's call without its MCP
settings (its project root and its `codex mcp list` runs), starts it; the bridge's call is refused (its second `codex
mcp list`, run with the call's table, still shows the server on); with the file in place the call goes through, starting
none. Then every feature `codex features list` names but shell_zsh_fork, turned on in a profile the home config selects:
a control, the bridge's call without its look and every feature setting, is offered the tools of the features case's
control and runs the shell files, the Node and the home's hook; the bridge's call is refused with the message that names
each feature the look reads. For each feature the look reads (each the bridge's settings turn off but unified_exec),
that one alone turned on in a selected profile: the call is refused with the message that names exactly it, the case's
time is printed, and the notes of the case's calls, read at once after each, are read once more after the wait and must
show that none of them started, asked or ran anything; unified_exec alone so: the call goes through as a decision above
does. Every feature the call does not turn off but shell_zsh_fork, turned on in a selected profile: the call goes
through the same way. Last, every feature but shell_zsh_fork turned on in the managed configuration's [features] table:
a control and a refused call, as for the profile.

The older names' case, with the managed configuration back to its server alone, and the file moved aside, then put back
where the image has it, its controls made each way. Each older name that turns on a feature the look reads
(codex_layers.OLDER_NAMES: of the pinned codex-cli's older names, which `codex features list` prints none of), alone on
in the home config's top-level [features] table: a control, the bridge's call without its look, is offered the feature's
tool (search_tool_bm25 for connectors, apply_patch for include_apply_patch_tool and
experimental_use_freeform_apply_patch), and the bridge's call is refused with the message that names the feature (apps,
or apply_patch_freeform); the notes of the three refused calls are read once more after the wait, as in the one-feature
case. The other five older names on together there: the call goes through as a decision above does. Then the
requirements file's case: /etc/codex/requirements.toml is written with a [features] table that turns shell_tool on,
after the image's own text where the image has the file, and what was there is put back after the case, whatever its
outcome. A control, the bridge's call without its look, is offered a command tool; the bridge's call is refused with the
message that names shell_tool.

The patch case, last, with the managed configuration its server alone: the home's config.toml names a model of the
pinned codex-cli's own catalog (codex_layers.PATCH_MODEL), whose catalog entry brings Codex's file-edit tool,
apply_patch, and the stand-in model, started again for the case, is given two files, one in the call's own directory (a
relative path) and one in the temp directory (where the bridge makes each call's directory): where a request offers
apply_patch, it sends a patch that adds the next of them, each as the request before came back with the last patch's
output, then its decision. With the file moved aside, a control, the bridge's call made with its sandbox-mode argument
workspace-write, is offered apply_patch, Codex answers each patch as written, and both files are there once its exec has
returned (the call's directory is read before the call removes it). Then the bridge makes one decision with its default
sandbox mode, and one more with the file put back, each held to what the decisions above are held to, but offered
apply_patch beside those three tools: Codex must answer each patch with its refusal (codex_layers.PATCH_REFUSED), no
answer may say the file was written, the call's directory must have been read after its exec, and neither file may be
there.

The model, the servers, the listener and the planted programs are cli_stand_ins.py's: its Responses API model, which
logs each request and calls the first MCP tool it is offered, and, given files, sends a patch for each where a request
offers apply_patch, its stdio MCP server, which notes its start, its listener, which notes each request, its mark, which
notes its run (the zsh_path program is one), and its Node, which notes its run and answers --version as a Node new
enough for js_repl; the shell files note their run with the shell's own echo.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import redirect_stderr
from pathlib import Path

import cli_stand_ins as stand_ins
import codex_layers
from cli_stand_ins import fail

PORT = 18081
OTEL_PORT = 18084
NOTES = stand_ins.Notes(Path("/data/bridge-check"))
HOME = NOTES.work / "home"
# Where the bridge makes each call's directory (tempfile's), made a trusted project root.
PROJECT = Path(tempfile.gettempdir())
LAYERS = {
    "sys": codex_layers.SYSTEM / "config.toml",
    "mgd": codex_layers.SYSTEM / "managed_config.toml",
    "proj": PROJECT / ".codex" / "config.toml",
}
STARTED_WITHOUT_THE_BRIDGE = (codex_layers.HOME_SERVERS[0], *LAYERS)
SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}
HOOKS = {"home": HOME / ".codex", "sys": codex_layers.SYSTEM, "proj": PROJECT / ".codex"}
RAN_WITHOUT_THE_BRIDGE = ("notify ran", *(f"{label} hook ran" for label in HOOKS))
# The two AGENTS.md (the temp directory's, the call directory's), as the model's request holds them.
PARENT_DOC = "planted-project-doc-of-the-temp-directory"
IN_THE_REQUEST = (PARENT_DOC, codex_layers.CALL_DOC)
# Where Codex writes its own bundled skills as it starts, and where the control notes what it left there.
BUNDLED_SKILLS_DIR = HOME / ".codex" / "skills" / ".system"
SKILLS_FILE = NOTES.work / "bundled-skills"
DECISION_HEADING = "=== the bridge's decision"
# What the features case's control's and decisions' headings add.
FEATURES = ", with every feature but shell_zsh_fork on in the home config's top-level [features] table"
ZSH_PATH = ", with every feature on in the home config's top-level [features] table and a planted zsh_path"
# What the later cases' headings add.
MANAGED_ON = f", with the managed server {codex_layers.MANAGED_ON} naming enabled = true"
IN_A_PROFILE = ", with every feature but shell_zsh_fork on in a profile the home config selects"
IN_THE_MANAGED_TABLE = ", with every feature but shell_zsh_fork on in the managed configuration's [features] table"
OTHER_FEATURES = ", with every feature the call does not turn off but shell_zsh_fork on in a selected profile"
OLDER_NAME_ON = ", with {} = true in the home config's top-level [features] table"
OTHER_OLDER_NAMES = ", with the other older names of features on in the home config's top-level [features] table"
REQUIRED = f", with {codex_layers.REQUIRED_ON} = true in a [features] table of {codex_layers.REQUIREMENTS}"
# The status of a call the bridge refuses before its exec, written out.
REFUSED = 409
# What the patch case's headings add, and the files the stand-in model's patches add there: the second in the temp
# directory, where the bridge makes each call's directory and which Codex's workspace-write sandbox lets its call write.
PATCH = f", with {codex_layers.PATCH_MODEL} as the home config's model"
PATCH_TARGETS = codex_layers.patch_targets(PROJECT)


def plant() -> None:
    (HOME / ".codex").mkdir(parents=True, exist_ok=True)
    config = codex_layers.provider(PORT, PROJECT, codex_layers.extras(NOTES, OTEL_PORT))
    servers = "".join(codex_layers.server_table(label, NOTES) for label in codex_layers.HOME_SERVERS)
    (HOME / ".codex" / "config.toml").write_text(config + servers, encoding="utf-8")
    (HOME / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "placeholder"}\n', encoding="utf-8")
    (PROJECT / ".git").mkdir(exist_ok=True)
    for label, path in LAYERS.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(codex_layers.server_table(label, NOTES), encoding="utf-8")
    for label, folder in HOOKS.items():
        (folder / "hooks.json").write_text(codex_layers.hooks(label, NOTES), encoding="utf-8")
    (PROJECT / "AGENTS.md").write_text(PARENT_DOC + "\n", encoding="utf-8")


def _load_bridge():
    path = Path(__file__).resolve().parent.parent / "codex_host_bridge.py"
    spec = importlib.util.spec_from_file_location("codex_host_bridge", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def control(bridge, codex: str) -> float:
    """The bridge's exec without its two MCP settings, its command-tool settings and its extras settings, in a
    directory under the trusted project: the servers of every layer start and offer their tools, a command tool is
    offered, and the extras run, send and reach the model. Returns how long a decision waits for its notes."""
    NOTES.fresh()
    taken_out = ("project_root_markers=[]", *codex_layers.COMMAND_TOOL_SETTINGS, *codex_layers.EXTRAS_SETTINGS)
    flags = [word for override in bridge.CONFIG_OVERRIDES if override not in taken_out for word in ("-c", override)]
    with tempfile.TemporaryDirectory(dir=PROJECT) as where:
        codex_layers.plant_call_doc(where)
        command = [codex, *flags, "exec", "--skip-git-repo-check", "-s", "read-only", "--cd", where, "--ephemeral", "-"]
        done = subprocess.run(
            command,
            input=b"decide",
            capture_output=True,
            timeout=stand_ins.CALL_TIMEOUT_SECONDS,
            env=bridge.codex_environment(),
        )
    started_marks = [f"{label} started" for label in STARTED_WITHOUT_THE_BRIDGE]
    landed = stand_ins.wait_for(
        {NOTES.marker: started_marks, NOTES.extras: RAN_WITHOUT_THE_BRIDGE, NOTES.telemetry: codex_layers.TELEMETRY}
    )
    requests = stand_ins.logged(NOTES.model_log)
    started, offered = stand_ins.noted(NOTES.marker), stand_ins.mcp_tools(requests)
    ran, sent, held = codex_layers.extras_seen(NOTES, requests, IN_THE_REQUEST)
    print("=== control: codex exec without the bridge's MCP, command-tool and extras settings")
    print(f"exit {done.returncode}; servers: {started}; MCP tools offered: {offered}")
    print(f"command tools offered: {codex_layers.command_tools(requests)}")
    codex_layers.show(ran, sent, held, BUNDLED_SKILLS_DIR)
    wait = stand_ins.settle_seconds(landed)
    stderr_tail = done.stderr.decode("utf-8", "replace")[-1500:]
    if not set(started_marks) <= set(started):
        print(stderr_tail)
        fail("the planted servers did not all start without the bridge's settings: the check would prove nothing")
    codex_layers.offered_in_the_control(requests, STARTED_WITHOUT_THE_BRIDGE, "the bridge's settings")
    if (
        set(ran) != set(RAN_WITHOUT_THE_BRIDGE)
        or set(sent) != set(codex_layers.TELEMETRY)
        or held != list(IN_THE_REQUEST)
    ):
        print(stderr_tail)
        fail("the planted extras did not all run, send or reach the model without the bridge's settings")
    codex_layers.skills_left(BUNDLED_SKILLS_DIR, SKILLS_FILE, "the bridge's settings")
    return wait


def _planted(bridge):
    class Planted(bridge.CodexBridgeService):
        """The bridge, with an AGENTS.md planted in its call's directory before each command it runs."""

        def _run_codex(self, command: list[str], prompt: bytes, **kwargs: object):
            codex_layers.plant_call_doc(kwargs["cwd"])
            return super()._run_codex(command, prompt, **kwargs)

    return Planted


def _decide(service) -> dict:
    return service.handle_openai_decide({"prompt": "decide the next browser action", "schema": SCHEMA})


def plant_features(names: list[str], *, zsh_fork: bool = False, zsh_path: bool = False) -> None:
    """The features case's planting: the home's config.toml with its servers and, in place of the extras' keys, the
    top-level keys that turn on each of ``names`` but shell_zsh_fork (that one too where ``zsh_fork``) and name a
    planted program as zsh_path where ``zsh_path``; the shell files in HOME, and the Node as js_repl_node_path."""
    zsh = codex_layers.zsh_program(NOTES) if zsh_path else None
    node = codex_layers.plant_programs(HOME, NOTES)
    keys = codex_layers.features_on(names, node, zsh_fork=zsh_fork, zsh_path=zsh)
    servers = "".join(codex_layers.server_table(label, NOTES) for label in codex_layers.HOME_SERVERS)
    (HOME / ".codex" / "config.toml").write_text(codex_layers.provider(PORT, PROJECT, keys) + servers, encoding="utf-8")


def _without(service_class, taken_out):
    class Without(service_class):
        """The bridge, with each -c setting ``taken_out`` names taken out of each command it runs."""

        def _run_codex(self, command: list[str], prompt: bytes, **kwargs: object):
            return super()._run_codex(codex_layers.without(command, taken_out), prompt, **kwargs)

    return Without


def _without_the_look(service_class):
    class WithoutTheLook(service_class):
        """The bridge, with its look at `codex features list` taken out: a control's call, whose settings are taken
        out, would be refused there."""

        def _look_at_features(self, tempdir: str, deadline: float) -> None:
            return None

    return WithoutTheLook


def _without_the_lists(service_class):
    class WithoutTheLists(service_class):
        """The bridge, with its `codex mcp list` runs taken out: its exec turns off no server."""

        def _mcp_servers_off(self, tempdir: str, deadline: float) -> list[str]:
            return []

    return WithoutTheLists


def features_control(bridge, codex: str, first: float) -> float:
    """The bridge's call without its look and its feature settings, on the features case's planting. Returns how long
    a decision of the features case waits for its notes: the longer of this control's measure and ``first``, the first
    control's, as the first control's servers and extras are still planted."""
    without = _without(_without_the_look(_planted(bridge)), codex_layers.FEATURE_SETTINGS.__contains__)
    NOTES.fresh()
    service = without(codex_path=codex, request_timeout_seconds=stand_ins.CALL_TIMEOUT_SECONDS)
    try:
        outcome = f"reply: {_decide(service)}"
    except bridge.BridgeError as exc:
        outcome = f"refused: {exc.message}"
    landed = stand_ins.wait_for({NOTES.extras: codex_layers.RAN_WITH_THE_FEATURES})
    requests, ran = stand_ins.logged(NOTES.model_log), stand_ins.noted(NOTES.extras)
    print(f"=== control: the bridge's call without its feature settings{FEATURES}")
    print(f"tools offered: {stand_ins.offered(requests)}; {outcome}")
    print(f"ran: {ran}")
    wait = max(stand_ins.settle_seconds(landed), first)
    print(f"the features case's decisions wait {wait:.1f} s, the longer of that and the first control's wait")
    codex_layers.features_in_the_control(requests, ran, "the bridge's feature settings")
    return wait


def zsh_path_control(bridge, codex: str) -> None:
    """The bridge's call without its look and its command-tool settings, on the zsh_path case's planting: its model is
    offered the command tool of shell_zsh_fork with a zsh_path (codex_layers.ZSH_FORK_TOOL)."""
    NOTES.fresh()
    without = _without(_without_the_look(_planted(bridge)), codex_layers.COMMAND_TOOL_SETTINGS.__contains__)
    outcome = _reply(bridge, _service(without, codex))
    requests = stand_ins.logged(NOTES.model_log)
    print(f"=== control: the bridge's call without its look and its command-tool settings{ZSH_PATH}")
    print(f"tools offered: {stand_ins.offered(requests)}; {outcome}")
    print(f"ran: {stand_ins.noted(NOTES.extras)}")
    without_them = "the bridge's look and its command-tool settings"
    codex_layers.tool_in_the_control(requests, (codex_layers.ZSH_FORK_TOOL,), without_them)


def zsh_fork_refused(bridge, codex: str, wait: float) -> None:
    """The bridge's call with shell_zsh_fork on and no zsh_path; what Codex said is read from the bridge's log, and its
    notes ``wait`` seconds after it returned."""
    NOTES.fresh()
    service = _planted(bridge)(codex_path=codex, request_timeout_seconds=stand_ins.CALL_TIMEOUT_SECONDS)
    log = io.StringIO()
    with redirect_stderr(log):
        try:
            outcome, decided = f"reply: {_decide(service)}", True
        except bridge.BridgeError as exc:
            outcome, decided = f"refused: {exc.message}", False
    time.sleep(wait)
    codex_layers.zsh_fork_refused("bridge", outcome, decided, log.getvalue(), NOTES)


def through_the_bridge(
    bridge, codex: str, heading: str, wait: float, tools: tuple[str, ...] = codex_layers.DECISION_TOOLS, planted=None
) -> None:
    """One decision of ``planted`` (_planted(bridge) where it is None), held to starting no server, offering its model
    ``tools`` and no other, running, sending and passing on none of the extras, keeping the bundled skills and coming
    back; its notes read ``wait`` seconds after it returned."""
    NOTES.fresh()
    service = (planted or _planted(bridge))(codex_path=codex, request_timeout_seconds=stand_ins.CALL_TIMEOUT_SECONDS)
    reply = _decide(service)
    time.sleep(wait)
    started, requests = stand_ins.noted(NOTES.marker), stand_ins.logged(NOTES.model_log)
    offered = stand_ins.offered(requests)
    ran, sent, held = codex_layers.extras_seen(NOTES, requests, IN_THE_REQUEST)
    print(heading)
    print(f"servers: {started or 'none started'}; tools offered: {offered}; reply: {reply}")
    codex_layers.show(ran, sent, held, BUNDLED_SKILLS_DIR)
    if started:
        fail(f"the bridge's Codex started MCP servers: {started}")
    if not requests:
        fail("the model was never asked")
    stand_ins.offered_exactly(requests, tools)
    if ran or sent or held:
        fail(f"the bridge's Codex ran, sent or passed on planted extras: {ran + sent + held}")
    codex_layers.skills_kept(BUNDLED_SKILLS_DIR, SKILLS_FILE, "bridge")
    if json.loads(reply["raw_text"]) != stand_ins.DECISION:
        fail(f"the decision did not come back: {reply}")


def plant_home(keys: str, model: str = codex_layers.STAND_IN_MODEL) -> None:
    """The home's config.toml again: ``model``, the model provider, ``keys`` (top-level keys and tables) and its
    servers."""
    servers = "".join(codex_layers.server_table(label, NOTES) for label in codex_layers.HOME_SERVERS)
    config = codex_layers.provider(PORT, PROJECT, keys, model)
    (HOME / ".codex" / "config.toml").write_text(config + servers, encoding="utf-8")


def _service(service_class, codex: str):
    return service_class(codex_path=codex, request_timeout_seconds=stand_ins.CALL_TIMEOUT_SECONDS)


def _reply(bridge, service) -> str:
    """What one decision of ``service`` came to: the BridgeError's message, or the reply."""
    try:
        return f"reply: {_decide(service)}"
    except bridge.BridgeError as exc:
        return exc.message


def server_on_control(bridge, codex: str) -> float:
    """The bridge's call without its MCP settings (its project root and its lists of servers), on the managed server
    case's planting: it starts the managed server that names enabled = true. Returns how long a decision waits for its
    notes."""
    NOTES.fresh()
    without = _without(_without_the_lists(_planted(bridge)), "project_root_markers=[]".__eq__)
    outcome = _reply(bridge, _service(without, codex))
    landed = stand_ins.wait_for({NOTES.marker: [f"{codex_layers.MANAGED_ON} started"]})
    started = stand_ins.noted(NOTES.marker)
    print(f"=== control: the bridge's call without its MCP settings{MANAGED_ON}")
    print(f"servers: {started}; {outcome}")
    wait = stand_ins.settle_seconds(landed)
    codex_layers.server_on_in_the_control(started, "the bridge's MCP settings")
    return wait


def features_on_control(bridge, codex: str, where: str) -> float:
    """The bridge's call without its look and every feature setting, on a case's planting that turns every feature on
    (``where`` says where): its model is offered the features' tools, and the shell files, the Node and the home's
    hook run. Returns how long a decision waits for its notes."""
    NOTES.fresh()
    without = _without(_without_the_look(_planted(bridge)), codex_layers.feature_setting)
    outcome = _reply(bridge, _service(without, codex))
    landed = stand_ins.wait_for({NOTES.extras: (*codex_layers.RAN_WITH_THE_FEATURES, codex_layers.HOME_HOOK_RAN)})
    requests, ran = stand_ins.logged(NOTES.model_log), stand_ins.noted(NOTES.extras)
    print(f"=== control: the bridge's call without its look and its feature settings{where}")
    print(f"tools offered: {stand_ins.offered(requests)}; {outcome}")
    print(f"ran: {ran}")
    wait = stand_ins.settle_seconds(landed)
    codex_layers.features_on_in_the_control(requests, ran, "the bridge's look and its feature settings")
    return wait


def _recording(service_class, commands: list[list[str]]):
    class Recording(service_class):
        """The bridge, noting in ``commands`` each command it runs, as it runs it."""

        def _run_codex(self, command: list[str], prompt: bytes, **kwargs: object):
            commands.append(list(command))
            return super()._run_codex(command, prompt, **kwargs)

    return Recording


def refused(
    bridge,
    codex: str,
    heading: str,
    why: str,
    expected: str,
    wait: float | None,
    children: tuple[str, ...] = codex_layers.REFUSED_AT_THE_LOOK,
) -> None:
    """One call the bridge must refuse with ``expected`` (codex_layers.refused) and status REFUSED
    (codex_layers.refused_as), having run ``children`` (by default the look alone), no exec among them; its notes
    read ``wait`` seconds after it returned, or at once and without making them fresh where ``wait`` is None."""
    if wait is not None:
        NOTES.fresh()
    commands: list[list[str]] = []
    try:
        outcome, kind = f"reply: {_decide(_service(_recording(_planted(bridge), commands), codex))}", "a reply"
    except bridge.BridgeError as exc:
        outcome, kind = exc.message, f"status {exc.status_code}"
    time.sleep(wait or 0)
    print(heading)
    codex_layers.refused("bridge", why, outcome, expected, NOTES)
    codex_layers.refused_as("bridge", why, kind, f"status {REFUSED}")
    kinds = [codex_layers.command_kind(command[1:]) for command in commands]
    print(f"commands the call ran: {', '.join(kinds)}")
    if kinds != list(children):
        fail(f"the call ran {kinds}, not {list(children)}")


def each_feature_alone(bridge, codex: str, heading: str, wait: float) -> None:
    """For each feature the look reads (each the bridge's settings turn off but unified_exec), that one alone turned on
    in a profile the home config selects: the call is refused, naming exactly it, and no exec runs, so their notes are
    read at once and once more ``wait`` seconds on, and the case's time is printed; then unified_exec alone so: the
    call goes through as a decision does."""
    node = codex_layers.node_path(NOTES)
    names = codex_layers.looked_at(bridge.CONFIG_OVERRIDES)
    NOTES.fresh()
    began = time.monotonic()
    for name in names:
        plant_home(codex_layers.features_in_a_profile([name], node))
        why, expected = f"with {name} on in a profile the home config selects", bridge.features_on_error([name])
        refused(bridge, codex, f"=== {name} alone on in a selected profile{heading}", why, expected, None)
    print(f"=== {len(names)} calls, each with one feature on, refused in {time.monotonic() - began:.1f} s")
    time.sleep(wait)
    print("=== the case's notes, read again after the wait")
    case = f"each with one feature the look reads on alone in a selected profile{heading}"
    codex_layers.none_after_the_wait("bridge", case, len(names), NOTES)
    plant_home(codex_layers.features_in_a_profile([codex_layers.NOT_LOOKED_AT], node))
    suffix = f", with {codex_layers.NOT_LOOKED_AT} alone on in a profile the home config selects{heading}"
    through_the_bridge(bridge, codex, DECISION_HEADING + suffix, wait)


def _in_place(in_the_image: bool, run: Callable[[str], object]) -> None:
    """``run(codex_layers.IN_PLACE)`` where the image has /etc/codex/requirements.toml; else the line that says so."""
    if in_the_image:
        run(codex_layers.IN_PLACE)
    else:
        print(codex_layers.NOT_IN_THE_IMAGE)


def _both_ways(in_the_image: bool, run: Callable[[str], object]) -> None:
    """``run("")`` with /etc/codex/requirements.toml moved aside, then ``run(codex_layers.IN_PLACE)`` with it in place
    where the image has it."""
    with codex_layers.requirements_aside():
        run("")
    _in_place(in_the_image, run)


def features_refused(bridge, codex: str, where: str, in_the_image: bool) -> float:
    """A case that turns every feature but shell_zsh_fork on (``where`` says where): with requirements.toml moved
    aside, its control, then the bridge's call refused, naming each feature the look reads; with it in place, the call
    refused again. Returns how long a decision waits for its notes."""
    why, expected = where.removeprefix(", "), bridge.features_on_error(codex_layers.looked_at(bridge.CONFIG_OVERRIDES))
    with codex_layers.requirements_aside():
        wait = features_on_control(bridge, codex, where)
        refused(bridge, codex, DECISION_HEADING + where, why, expected, wait)
    _in_place(in_the_image, lambda end: refused(bridge, codex, DECISION_HEADING + where + end, why, expected, wait))
    return wait


def tool_control(bridge, codex: str, where: str, tools) -> None:
    """The bridge's call without its look, on a case's planting that turns a feature on (``where`` says where): its
    model is offered one of ``tools``."""
    NOTES.fresh()
    outcome = _reply(bridge, _service(_without_the_look(_planted(bridge)), codex))
    requests = stand_ins.logged(NOTES.model_log)
    print(f"=== control: the bridge's call without its look{where}")
    print(f"tools offered: {stand_ins.offered(requests)}; {outcome}")
    codex_layers.tool_in_the_control(requests, tools, "the bridge's look")


def older_names(bridge, codex: str, heading: str, wait: float) -> None:
    """Each of codex_layers.OLDER_NAMES alone on in the home config's top-level [features] table: a control, the
    bridge's call without its look, is offered the feature's tool; then the bridge's call with each such table is
    refused, naming the feature, and no exec runs, so their notes are read at once and once more ``wait`` seconds on.
    Then codex_layers.OTHER_OLDER_NAMES on together there: the call goes through as a decision does."""
    for older, (_feature, tool) in codex_layers.OLDER_NAMES.items():
        plant_home(codex_layers.features_table([older]))
        tool_control(bridge, codex, OLDER_NAME_ON.format(older) + heading, [tool])
    NOTES.fresh()
    for older, (feature, _tool) in codex_layers.OLDER_NAMES.items():
        plant_home(codex_layers.features_table([older]))
        why, expected = OLDER_NAME_ON.format(older).removeprefix(", "), bridge.features_on_error([feature])
        heading_of_one = DECISION_HEADING + OLDER_NAME_ON.format(older) + heading
        refused(bridge, codex, heading_of_one, why, expected, None)
    time.sleep(wait)
    print("=== the case's notes, read again after the wait")
    case = f"each with one older name on alone in the home config's top-level [features] table{heading}"
    codex_layers.none_after_the_wait("bridge", case, len(codex_layers.OLDER_NAMES), NOTES)
    plant_home(codex_layers.features_table(codex_layers.OTHER_OLDER_NAMES))
    through_the_bridge(bridge, codex, DECISION_HEADING + OTHER_OLDER_NAMES + heading, wait)


def required_on(bridge, codex: str, wait: float) -> None:
    """With codex_layers.REQUIRED_ON turned on in a [features] table of /etc/codex/requirements.toml, written for the
    case and put back: a control, the bridge's call without its look, is offered a command tool; the bridge's call is
    refused, naming the feature."""
    why, expected = REQUIRED.removeprefix(", "), bridge.features_on_error([codex_layers.REQUIRED_ON])
    with codex_layers.requirements_turning_on([codex_layers.REQUIRED_ON]):
        tool_control(bridge, codex, REQUIRED, codex_layers.COMMAND_TOOLS)
        refused(bridge, codex, DECISION_HEADING + REQUIRED, why, expected, wait)


def later_cases(bridge, codex: str, names: list[str], in_the_image: bool) -> float:
    """The managed server case (with a home server that names enabled = false besides), then the features turned on
    in a profile the home config selects (every one but shell_zsh_fork; each the look reads alone, then unified_exec
    alone; every one the call does not turn off), then in the managed configuration's [features] table, each planted
    here: with /etc/codex/requirements.toml moved aside (a control first where the case has one), then with it in
    place where the image has it. The managed configuration is put back after its server's case. Returns how long a
    decision waits for its notes."""
    others = [name for name in names if name not in codex_layers.call_features(bridge.CONFIG_OVERRIDES)]
    managed = LAYERS["mgd"]
    node = codex_layers.plant_programs(HOME, NOTES)
    plant_home(codex_layers.server_off(NOTES))
    managed.write_text(codex_layers.managed_server_on(NOTES), encoding="utf-8")
    with codex_layers.requirements_aside():
        wait = server_on_control(bridge, codex)
        why, heading = MANAGED_ON.removeprefix(", "), DECISION_HEADING + MANAGED_ON
        on_error, at_the_second_list = bridge.MCP_SERVERS_ON_ERROR, codex_layers.REFUSED_AT_THE_SECOND_LIST
        refused(bridge, codex, heading, why, on_error, wait, at_the_second_list)
    _in_place(in_the_image, lambda end: through_the_bridge(bridge, codex, DECISION_HEADING + MANAGED_ON + end, wait))
    managed.write_text(codex_layers.server_table("mgd", NOTES), encoding="utf-8")
    plant_home(codex_layers.features_in_a_profile(names, node))
    wait = features_refused(bridge, codex, IN_A_PROFILE, in_the_image)
    plant_home(codex_layers.node_keys(node))  # each feature's profile is written as its call comes
    _both_ways(in_the_image, lambda end: each_feature_alone(bridge, codex, end, wait))
    plant_home(codex_layers.features_in_a_profile(others, node))
    _both_ways(
        in_the_image, lambda end: through_the_bridge(bridge, codex, DECISION_HEADING + OTHER_FEATURES + end, wait)
    )
    plant_home(codex_layers.node_keys(node))
    managed.write_text(codex_layers.managed_features(names, NOTES), encoding="utf-8")
    return features_refused(bridge, codex, IN_THE_MANAGED_TABLE, in_the_image)


def older_names_and_requirements(bridge, codex: str, in_the_image: bool, wait: float) -> None:
    """With the managed configuration back to its server alone: the older names' case, with
    /etc/codex/requirements.toml moved aside, then in place where the image has it; then, with the home config's
    features table gone, the requirements file's case."""
    LAYERS["mgd"].write_text(codex_layers.server_table("mgd", NOTES), encoding="utf-8")
    _both_ways(in_the_image, lambda end: older_names(bridge, codex, end, wait))
    plant_home("")
    required_on(bridge, codex, wait)


def _reading_the_call(planted, found: list[bool]):
    class ReadingTheCall(planted):
        """The bridge, noting in ``found``, once its exec has returned, whether its call's directory holds the file a
        patch of the patch case adds there, before the call removes the directory."""

        def _run_codex(self, command: list[str], prompt: bytes, **kwargs: object):
            answer = super()._run_codex(command, prompt, **kwargs)
            if "exec" in command:
                found.append((Path(kwargs["cwd"]) / codex_layers.PATCHED_IN_THE_CALL).exists())
            return answer

    return ReadingTheCall


def patch_control(bridge, codex: str) -> None:
    """The patch case's control: the bridge's call made with its sandbox-mode argument workspace-write is offered
    apply_patch, Codex answers each of the stand-in model's patches as written, and both files are there."""
    NOTES.fresh()
    (PROJECT / codex_layers.PATCHED_IN_TEMP).unlink(missing_ok=True)
    found: list[bool] = []
    service = _reading_the_call(_planted(bridge), found)(
        codex_path=codex,
        request_timeout_seconds=stand_ins.CALL_TIMEOUT_SECONDS,
        sandbox_mode=codex_layers.WORKSPACE_WRITE,
    )
    outcome = _reply(bridge, service)
    under = f"with the sandbox mode {codex_layers.WORKSPACE_WRITE}"
    print(f"=== control: the bridge's call {under}{PATCH}")
    print(outcome)
    written = codex_layers.patched_files(found, PROJECT)
    codex_layers.patch_control(stand_ins.logged(NOTES.model_log), written, PATCH_TARGETS, under)


def patches_held(bridge, codex: str, heading: str, wait: float) -> None:
    """One decision of the patch case, held to what through_the_bridge holds a decision to, its model offered
    apply_patch as well; then Codex answered each of the stand-in model's patches with its refusal, none as written,
    the call's directory was read after its exec, and neither file is there."""
    (PROJECT / codex_layers.PATCHED_IN_TEMP).unlink(missing_ok=True)
    found: list[bool] = []
    tools = (*codex_layers.DECISION_TOOLS, stand_ins.PATCH_TOOL)
    through_the_bridge(bridge, codex, heading, wait, tools, _reading_the_call(_planted(bridge), found))
    codex_layers.patches_held("bridge", stand_ins.logged(NOTES.model_log), found, PROJECT, PATCH_TARGETS)


def patch_case(bridge, codex: str, in_the_image: bool, wait: float) -> None:
    """The patch case: the home's config.toml names codex_layers.PATCH_MODEL as its model, and the stand-in model is
    given the case's files; with /etc/codex/requirements.toml moved aside, the control and a decision, then a decision
    with it in place where the image has it."""
    plant_home("", codex_layers.PATCH_MODEL)
    with (
        stand_ins.running("responses", PORT, NOTES.model_log, *PATCH_TARGETS),
        stand_ins.running("listen", OTEL_PORT, NOTES.telemetry),
    ):
        with codex_layers.requirements_aside():
            patch_control(bridge, codex)
            patches_held(bridge, codex, DECISION_HEADING + PATCH, wait)
        _in_place(in_the_image, lambda end: patches_held(bridge, codex, DECISION_HEADING + PATCH + end, wait))


def main() -> None:
    codex = shutil.which("codex") or fail("no codex")
    os.environ.update(HOME=str(HOME), OPENAI_API_KEY="placeholder")
    os.environ.pop("CODEX_HOME", None)
    plant()
    print(subprocess.run([codex, "--version"], capture_output=True, text=True).stdout.strip())
    bridge = _load_bridge()
    with (
        stand_ins.running("responses", PORT, NOTES.model_log),
        stand_ins.running("listen", OTEL_PORT, NOTES.telemetry),
    ):
        with codex_layers.requirements_aside() as in_the_image:
            wait = control(bridge, codex)
            through_the_bridge(bridge, codex, DECISION_HEADING, wait)
        if in_the_image:
            through_the_bridge(bridge, codex, DECISION_HEADING + codex_layers.IN_PLACE, wait)
        else:
            print(codex_layers.NOT_IN_THE_IMAGE)
        names = codex_layers.feature_names(codex, bridge.codex_environment(), bridge.CONFIG_OVERRIDES)
        plant_features(names)
        with codex_layers.requirements_aside():
            wait = features_control(bridge, codex, wait)
            through_the_bridge(bridge, codex, DECISION_HEADING + FEATURES, wait)
        if in_the_image:
            through_the_bridge(bridge, codex, DECISION_HEADING + FEATURES + codex_layers.IN_PLACE, wait)
        else:
            print(codex_layers.NOT_IN_THE_IMAGE)
        plant_features(names, zsh_fork=True, zsh_path=True)
        with codex_layers.requirements_aside():
            zsh_path_control(bridge, codex)
            through_the_bridge(bridge, codex, DECISION_HEADING + ZSH_PATH, wait)
        _in_place(in_the_image, lambda end: through_the_bridge(bridge, codex, DECISION_HEADING + ZSH_PATH + end, wait))
        plant_features(names, zsh_fork=True)
        with codex_layers.requirements_aside():
            zsh_fork_refused(bridge, codex, wait)
        wait = later_cases(bridge, codex, names, in_the_image)
        older_names_and_requirements(bridge, codex, in_the_image, wait)
    patch_case(bridge, codex, in_the_image, wait)
    print("=== all held")


if __name__ == "__main__":
    main()

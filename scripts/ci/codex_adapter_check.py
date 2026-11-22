"""A Codex call of the controller's (OpenAIAdapter, OPENAI_AUTH_MODE=cli) starts no MCP server, offers its model no tool
but Codex's own update_plan, request_user_input and view_image (and apply_patch where the home config names
codex_layers.PATCH_MODEL, a model of Codex's own catalog: neither of the two patches the stand-in model sends there,
each adding one file, writes it), and runs none of the extras a config layer names and no shell file, Node or zsh_path
program of a feature the home config's top-level [features] table turns on; it is refused, with no exec, where the
managed configuration keeps a server on, or where a selected profile, the managed configuration, an older name in the
home config's [features] table or a [features] table of /etc/codex/requirements.toml turns on a feature the call turns
off; and a decision with any one of its settings taken out shows what the check's table names for that setting, end to
end with the image's codex-cli: the agent-cli-image workflow runs it in the controller image built with
INSTALL_AGENT_CLIS=true, as root, with scripts/ at /scripts.

The call runs as the controller runs it there: the image's controller code under /app (a directory earlier on PYTHONPATH
takes its place; the first line printed names the code that ran), the image's default settings besides
OPENAI_AUTH_MODE=cli and a 180-second MODEL_REQUEST_TIMEOUT_SECONDS (so CLI_HOME /data/cli-home, the CLI sandbox on,
CODEX_SANDBOX_MODE read-only), the controller's own temp directory as its start makes it, and the controller's uid 10002
with no new privileges and no capabilities, as entrypoint.sh starts it. Each decision, and each refused call, fails
unless the children its call started, as read from the call's own spawns, are the commands of its kind, in order, and
nothing else (codex_layers.command_kind), each started by the CLI sandbox's launcher: for a decision, its look at `codex
features list`, its `codex mcp list --json`, that list again with the call's table of servers (but with the image's
requirements.toml in place, where the first shows every server off), and its `codex exec`
(codex_layers.decision_children); for a decision of the settings case, the same but the look; for a refused call, the
look alone, or where a server is kept on the look and both lists, and no exec. Only the planting below, and moving
/etc/codex/requirements.toml, run as root.

An MCP server is planted in each config layer Codex reads: /etc/codex/config.toml, /etc/codex/managed_config.toml,
CODEX_HOME's config.toml (two there, one with a name only a quoted TOML key can hold), and a trusted project root's
.codex/config.toml above the call's directory (the CLI home, given a .git; CODEX_HOME is a directory in it).

The extras: /etc/codex/config.toml also names a notify program, turns on the codex_hooks feature, sends the three
telemetry exporters to a listener, and names project_doc_max_bytes; a hooks.json with a SessionStart hook is in
CODEX_HOME, /etc/codex and the project root's .codex; an AGENTS.md is in the CLI home and, planted as each command of
the call runs, in the call's own directory.

The image's /etc/codex/requirements.toml (mcp_servers = {}) is moved aside, as the bridge check moves it: with it in
place Codex turns off every planted server by itself, so the control would start none, and the check would test that
file in place of the adapter's own settings. The first control, the features case's, the zsh_path case's, the profile's,
the managed table's, the older names' and the requirements file's controls also take out the call's look at `codex
features list`, which would refuse a call with its settings taken out; the managed server case's control, which takes
out the call's MCP settings alone, and the patch case's, which changes its sandbox mode alone, keep it, as the look
reads neither. Control: the adapter's call with its two MCP settings (-c project_root_markers=[], and its `codex mcp
list` runs with the -c mcp_servers table that turns off each server they name), its two command-tool settings and its
extras settings taken out starts the servers of every layer but the home's odd-named one (which codex-cli 0.114.0 lists
and does not start), its model is offered the tool of each server it started and a command tool, the notify program and
the three hooks run, the three exporters send, the model's request holds both AGENTS.md, and Codex writes its bundled
skills into CODEX_HOME's skills/.system; it also measures how long after its call returned every one of those notes was
there. Then the adapter makes one decision, which must start none, offer the model those three tools of Codex's own and
no other, run and send none of the extras, leave the bundled skills folder holding what the control left there (each
name in it, recursively), and come back. Then the file is put back where it was, and the adapter makes one more decision
with everything still planted, which must do the same; where the image has no such file, the check says so. A decision's
notes are read SETTLE_SECONDS after its call returned, or SETTLE_FACTOR times the control's measure where that is longer
(cli_stand_ins.py's).

The features case. The check asks Codex for its features (`codex features list`, as root, with CODEX_HOME) and writes
CODEX_HOME's config.toml again: its servers, and top-level keys that turn on each of those features but shell_zsh_fork
in the [features] table, name a planted Node as js_repl_node_path and turn on experimental_use_freeform_apply_patch; a
.bash_profile and a .bashrc are planted in the CLI home, the call's HOME. With the file moved aside again, a control,
the adapter's call without its look and its feature settings, offers the model the tools of apply_patch_freeform,
artifact, code_mode, image_generation, js_repl and request_permissions_tool besides those three, and runs both shell
files (the shell snapshot's login shell) and the Node. Then the adapter makes one decision, and one more with the file
put back, each held to what the decisions above are held to, its notes read after the longer of the two controls' waits.
Then the zsh_path case: shell_zsh_fork turned on in that table as well, and a planted program named as zsh_path. With
the file moved aside, a control, the adapter's call without its look and its command-tool settings, is offered
shell_command; then the adapter makes one decision, and one more with the file put back, each held to what the decisions
above are held to (the zsh_path program among what must not run). Last, with the zsh_path taken out of that config.toml,
the adapter's call makes no decision, starts no server, runs and sends nothing planted, its model is never asked, and
Codex says that zsh_path is not configured; the line prints the end of what Codex said.

The settings case. CODEX_HOME's config.toml is written again: its servers, the features case's keys, and keys that turn
on what the call's other settings turn off (a notify program, the project docs' size, a fallback project-doc name, web
search, the three exporters, an agent role with a marked description); the project root's .codex/config.toml gains
developer_instructions that are a mark. With the file moved aside again, for each setting of the call
(app.codex_config's CONFIG_OVERRIDES, as the code that runs has them) the check's table (SETTING_EFFECTS) names an
effect for, the adapter makes one decision with that setting alone (and the look) taken out, which must show that effect
and nothing else: the tools its model is offered besides those three, the planted programs and shell files that ran,
what the listener got and the marks its request holds, its notes polled for as a control's are (a decision whose row
names no program and nothing the listener gets reads them at once); its commands must be a decision's but the look, and
its decision must come back. A setting the table shows with another (a Pair) gets two decisions, one with both taken out
and one with the other alone, its call's doc planted under the name the Pair gives; the names of the table's NOT_SHOWN
settings are printed in one line; a setting with no row, and a row or a Pair's partner that names a setting the call
does not have, fails the check, by name, before the control's call. Once the last decision is made, the notes are read
again after the features case's wait, and must show what that decision showed. The check also fails, by its exit code,
where `codex features list` exits non-zero, and by name where it prints a line that is no row as the call's look reads
one (a name of a-z, 0-9, underscore and dot, its stage's words, then true or false), and where it has no row for a
feature the call's settings turn off.

The later cases, each with the file moved aside, then put back where the image has it. A refused call must be refused
with the code's own words as a ProviderAPIError that is not retryable, start no server, ask the stand-in model nothing,
and run and send nothing planted. The managed server case: /etc/codex/managed_config.toml also names a server with
enabled = true, and CODEX_HOME's config.toml one with enabled = false, so the call's first list shows one server off and
the others on. A control, the adapter's call without its MCP settings, starts it; the adapter's call is refused (its
second `codex mcp list`, run with the call's table, still shows the server on); with the file in place the call goes
through, starting none. Then every feature `codex features list` names but shell_zsh_fork, turned on in a profile the
home config selects: a control, the adapter's call without its look and every feature setting, is offered the tools of
the features case's control and runs the shell files, the Node and CODEX_HOME's hook; the adapter's call is refused with
the error that names each feature the look reads. For each feature the look reads (each the call's settings turn off but
unified_exec), that one alone turned on in a selected profile: the call is refused with the error that names exactly it,
the case's time is printed, and the notes of the case's calls, read at once after each, are read once more after the
wait and must show that none of them started, asked or ran anything; unified_exec alone so: the call goes through as a
decision above does. Every feature the call does not turn off but shell_zsh_fork, turned on in a selected profile: the
call goes through the same way. Last, every feature but shell_zsh_fork turned on in the managed configuration's
[features] table: a control and a refused call, as for the profile.

The older names' case, with the managed configuration back to its server alone, and the file moved aside, then put back
where the image has it, its controls made each way. Each older name that turns on a feature the look reads
(codex_layers.OLDER_NAMES: of the pinned codex-cli's older names, which `codex features list` prints none of), alone on
in CODEX_HOME's top-level [features] table: a control, the adapter's call without its look, is offered the feature's
tool (search_tool_bm25 for connectors, apply_patch for include_apply_patch_tool and
experimental_use_freeform_apply_patch), and the adapter's call is refused with the error that names the feature (apps,
or apply_patch_freeform); the notes of the three refused calls are read once more after the wait, as in the one-feature
case. The other five older names on together there: the call goes through as a decision above does. Then the
requirements file's case: /etc/codex/requirements.toml is written with a [features] table that turns shell_tool on,
after the image's own text where the image has the file, and what was there is put back after the case, whatever its
outcome. A control, the adapter's call without its look, is offered a command tool; the adapter's call is refused with
the error that names shell_tool.

The patch case, last, with the managed configuration its server alone: CODEX_HOME's config.toml names a model of the
pinned codex-cli's own catalog (codex_layers.PATCH_MODEL), whose catalog entry brings Codex's file-edit tool,
apply_patch, and the stand-in model is given two files, one in the call's own directory (a relative path) and one in
/tmp: where a request offers apply_patch, it sends a patch that adds the next of them, each as the request before came
back with the last patch's output, then its decision. With the file moved aside, a control, the adapter's call made with
CODEX_SANDBOX_MODE workspace-write, is offered apply_patch, Codex answers each patch as written, and both files are
there once its exec has returned (the call's directory is read before the call removes it). Then the adapter makes one
decision with the image's CODEX_SANDBOX_MODE, and one more with the file put back, each held to what the decisions above
are held to, but offered apply_patch beside those three tools: Codex must answer each patch with its refusal
(codex_layers.PATCH_REFUSED), no answer may say the file was written, the call's directory must have been read after its
exec, and neither file may be there.

The model, the servers, the listener and the planted programs are cli_stand_ins.py's; the shell files note their run
with the shell's own echo.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import as_controller
import cli_stand_ins as stand_ins
import codex_layers
from as_controller import CLI_HOME, NOTES
from cli_stand_ins import fail

PORT = 18082
OTEL_PORT = 18083
CODEX_HOME = CLI_HOME / "codex-home"
LAYERS = {
    "sys": codex_layers.SYSTEM / "config.toml",
    "mgd": codex_layers.SYSTEM / "managed_config.toml",
    "proj": CLI_HOME / ".codex" / "config.toml",
}
STARTED_WITHOUT_ITS_SETTINGS = (codex_layers.HOME_SERVERS[0], *LAYERS)
MCP_SETTING = "project_root_markers=[]"
HOOKS = {"home": CODEX_HOME, "sys": codex_layers.SYSTEM, "proj": CLI_HOME / ".codex"}
RAN_WITHOUT_ITS_SETTINGS = ("notify ran", *(f"{label} hook ran" for label in HOOKS))
# The two AGENTS.md (the CLI home's, the call directory's), as the model's request holds them.
PARENT_DOC = "planted-project-doc-of-the-cli-home"
IN_THE_REQUEST = (PARENT_DOC, codex_layers.CALL_DOC)
# Where Codex writes its own bundled skills as it starts.
BUNDLED_SKILLS_DIR = CODEX_HOME / "skills" / ".system"
DECISION_HEADING = "=== the adapter's decision"
# The argument of the as-cli run that makes the one decision with the image's requirements.toml in place.
WITH_REQUIREMENTS = "with-requirements"
# The argument of the as-cli runs of the features case, and what its control's and its decisions' headings add.
FEATURES_CASE = "features"
FEATURES = ", with every feature but shell_zsh_fork on in the home config's top-level [features] table"
# The argument of the as-cli runs of the features case's zsh_path case, and what its headings add.
ZSH_PATH_CASE = "zsh-path"
ZSH_PATH = ", with every feature on in the home config's top-level [features] table and a planted zsh_path"
# Where the control's run leaves how long a decision waits for its notes, for that later run's decision.
SETTLE_FILE = NOTES.work / "settle-seconds"
# Where the control's run notes what it left in BUNDLED_SKILLS_DIR, for each later decision.
SKILLS_FILE = NOTES.work / "bundled-skills"
# The argument of the as-cli run of the settings case; the names the call's doc is planted under there (AGENTS.md, and
# the home config's fallback project-doc name); the description of its agent role and the project layer's
# developer_instructions, as the model's request holds them.
SETTINGS_CASE = "settings"
AGENTS_DOC = "AGENTS.md"
FALLBACK_DOC = "FALLBACK.md"
ROLE = "planted-description-of-an-agent-role"
PROJECT_INSTRUCTIONS = "planted-developer-instructions-of-the-project-layer"
SETTINGS_MARKS = (PARENT_DOC, codex_layers.CALL_DOC, ROLE, PROJECT_INSTRUCTIONS)
MULTI_AGENT_TOOLS = ("close_agent", "resume_agent", "send_input", "spawn_agent", "spawn_agents_on_csv", "wait")
# The arguments of the as-cli runs of the later cases, and what their headings add.
MANAGED_ON_CASE = "managed-server-on"
MANAGED_ON = f", with the managed server {codex_layers.MANAGED_ON} naming enabled = true"
PROFILE_CASE = "features-in-a-profile"
IN_A_PROFILE = ", with every feature but shell_zsh_fork on in a profile the home config selects"
MANAGED_FEATURES_CASE = "managed-features"
IN_THE_MANAGED_TABLE = ", with every feature but shell_zsh_fork on in the managed configuration's [features] table"
ONE_FEATURE_CASE = "one-feature-in-a-profile"
OTHER_FEATURES_CASE = "other-features-in-a-profile"
OTHER_FEATURES = ", with every feature the call does not turn off but shell_zsh_fork on in a selected profile"
OLDER_NAMES_CASE = "older-names"
OLDER_NAME_ON = ", with {} = true in the home config's top-level [features] table"
OTHER_OLDER_NAMES = ", with the other older names of features on in the home config's top-level [features] table"
REQUIRED_CASE = "requirements-features"
REQUIRED = f", with {codex_layers.REQUIRED_ON} = true in a [features] table of {codex_layers.REQUIREMENTS}"
# How the adapter refuses a call before its exec, written out: a provider error that is not retried.
REFUSED = "ProviderAPIError retryable=False"
# The arguments of the as-cli runs of the patch case (its decisions) and of its control, which is made with
# CODEX_SANDBOX_MODE workspace-write; what their headings add; and the files the stand-in model's patches add there,
# the second in /tmp, which Codex's workspace-write sandbox lets the adapter's call write.
PATCH_CASE = "patch"
PATCH_CONTROL = "patch-control"
PATCH = f", with {codex_layers.PATCH_MODEL} as the home config's model"
TEMP = Path("/tmp")  # noqa: S108 - where Codex's sandbox lets the call write, read for the file a patch adds
PATCH_TARGETS = codex_layers.patch_targets(TEMP)


class Shown(NamedTuple):
    """What a decision of the settings case shows, and nothing else: the tools its model is offered besides
    DECISION_TOOLS, the planted programs that ran, what the listener got and the SETTINGS_MARKS its request holds."""

    tools: tuple[str, ...] = ()
    ran: tuple[str, ...] = ()
    sent: tuple[str, ...] = ()
    held: tuple[str, ...] = ()


class Pair(NamedTuple):
    """A setting shown with ``partner`` taken out as well: a decision with both taken out shows ``shown``, one with
    ``partner`` alone taken out shows ``partner_alone``; the call's doc is planted as ``doc`` for both."""

    partner: str
    shown: Shown
    partner_alone: Shown
    doc: str = AGENTS_DOC


# For each setting of the call, what a decision with it alone taken out shows on the settings case's planting; a Pair
# for one shown with another; NOT_SHOWN for one no decision of the case shows an effect of, whose name each run prints.
NOT_SHOWN = None
SETTING_EFFECTS: dict[str, Shown | Pair | None] = {
    "project_doc_fallback_filenames=[]": Pair(
        "project_doc_max_bytes=0", Shown(held=(codex_layers.CALL_DOC,)), Shown(), doc=FALLBACK_DOC
    ),
    "agents={}": NOT_SHOWN,
    "features.multi_agent=false": Shown(tools=MULTI_AGENT_TOOLS, held=(ROLE,)),
    "features.apps=false": Shown(tools=("search_tool_bm25",)),
    "features.shell_tool=false": Shown(tools=("shell",)),
    "features.unified_exec=false": Pair(
        "features.shell_tool=false", Shown(tools=("exec_command", "write_stdin")), Shown(tools=("shell",))
    ),
    'web_search="disabled"': Shown(tools=("web_search",)),
    "project_root_markers=[]": Shown(held=(PROJECT_INSTRUCTIONS,)),
    "notify=[]": Shown(ran=("notify ran",)),
    "features.codex_hooks=false": Shown(ran=("home hook ran", "sys hook ran")),
    'otel.exporter="none"': Shown(sent=("POST /v1/logs",)),
    'otel.trace_exporter="none"': Shown(sent=("POST /v1/traces",)),
    'otel.metrics_exporter="none"': Shown(sent=("POST /v1/metrics",)),
    "project_doc_max_bytes=0": Shown(held=(codex_layers.CALL_DOC,)),
    "features.shell_snapshot=false": Shown(ran=tuple(f"{name} ran" for name in codex_layers.SHELL_FILES)),
    "features.js_repl=false": Shown(tools=("js_repl", "js_repl_reset"), ran=(f"{codex_layers.NODE} ran",)),
    "features.code_mode=false": Shown(tools=("code_mode",), ran=(f"{codex_layers.NODE} ran",)),
    "features.apply_patch_freeform=false": Shown(tools=("apply_patch",)),
    "features.artifact=false": Shown(tools=("artifacts",)),
    "features.image_generation=false": Shown(tools=("image_generation",)),
    "features.request_permissions_tool=false": Shown(tools=("request_permissions",)),
}


def plant() -> None:
    for label, path in LAYERS.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        keys = codex_layers.extras(NOTES, OTEL_PORT) if label == "sys" else ""
        path.write_text(keys + codex_layers.server_table(label, NOTES), encoding="utf-8")
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    for label, folder in HOOKS.items():
        (folder / "hooks.json").write_text(codex_layers.hooks(label, NOTES), encoding="utf-8")
    (CLI_HOME / "AGENTS.md").write_text(PARENT_DOC + "\n", encoding="utf-8")
    servers = "".join(codex_layers.server_table(label, NOTES) for label in codex_layers.HOME_SERVERS)
    (CODEX_HOME / "config.toml").write_text(codex_layers.provider(PORT, CLI_HOME) + servers, encoding="utf-8")
    (CODEX_HOME / "auth.json").write_text('{"OPENAI_API_KEY": "placeholder"}\n', encoding="utf-8")
    (CLI_HOME / ".git").mkdir(exist_ok=True)
    NOTES.work.mkdir(exist_ok=True)
    as_controller.chown_home()


def feature_names() -> list[str]:
    """The features `codex features list` names; the check fails where it has no row for one the call's settings (as
    the code that runs has them) turn off."""
    codex = shutil.which("codex") or fail("no codex")
    sys.path.append(as_controller.CONTROLLER)  # after PYTHONPATH's directories, as the as-cli runs load the code
    from app.codex_config import CONFIG_OVERRIDES

    environment = {**os.environ, "HOME": str(CLI_HOME), "CODEX_HOME": str(CODEX_HOME)}
    return codex_layers.feature_names(codex, environment, CONFIG_OVERRIDES)


def plant_features(names: list[str], *, zsh_fork: bool = False, zsh_path: bool = False) -> None:
    """The features case's planting: CODEX_HOME's config.toml with its servers and the top-level keys that turn on each
    of ``names`` but shell_zsh_fork (that one too where ``zsh_fork``) and name a planted program as zsh_path where
    ``zsh_path``; the shell files in the CLI home, and the Node as js_repl_node_path."""
    zsh = codex_layers.zsh_program(NOTES) if zsh_path else None
    node = codex_layers.plant_programs(CLI_HOME, NOTES)
    keys = codex_layers.features_on(names, node, zsh_fork=zsh_fork, zsh_path=zsh)
    servers = "".join(codex_layers.server_table(label, NOTES) for label in codex_layers.HOME_SERVERS)
    (CODEX_HOME / "config.toml").write_text(codex_layers.provider(PORT, CLI_HOME, keys) + servers, encoding="utf-8")
    as_controller.chown_home()


def plant_settings(names: list[str]) -> None:
    """The settings case's planting: CODEX_HOME's config.toml with its servers, the features case's keys, and keys that
    turn on what the call's other settings turn off (a notify program, the project docs' size, FALLBACK_DOC as a
    fallback project-doc name, web search, the three exporters, an agent role described as ROLE); the project root's
    .codex/config.toml with its server and PROJECT_INSTRUCTIONS as developer_instructions."""
    keys = (
        codex_layers.notify(NOTES)
        + f"project_doc_max_bytes = 32768\nproject_doc_fallback_filenames = [{json.dumps(FALLBACK_DOC)}]\n"
        + 'web_search = "live"\n'
        + codex_layers.features_on(names, codex_layers.plant_programs(CLI_HOME, NOTES))
        + codex_layers.exporters(OTEL_PORT)
        + f"[agents.planted]\ndescription = {json.dumps(ROLE)}\n"
    )
    servers = "".join(codex_layers.server_table(label, NOTES) for label in codex_layers.HOME_SERVERS)
    (CODEX_HOME / "config.toml").write_text(codex_layers.provider(PORT, CLI_HOME, keys) + servers, encoding="utf-8")
    instructions = f"developer_instructions = {json.dumps(PROJECT_INSTRUCTIONS)}\n"
    LAYERS["proj"].write_text(instructions + codex_layers.server_table("proj", NOTES), encoding="utf-8")
    as_controller.chown_home()


def write_home(keys: str, model: str = codex_layers.STAND_IN_MODEL) -> None:
    """CODEX_HOME's config.toml: ``model``, the model provider, ``keys`` (top-level keys and tables) and its servers."""
    servers = "".join(codex_layers.server_table(label, NOTES) for label in codex_layers.HOME_SERVERS)
    config = codex_layers.provider(PORT, CLI_HOME, keys, model)
    (CODEX_HOME / "config.toml").write_text(config + servers, encoding="utf-8")


def plant_home(keys: Callable[[Path], str]) -> None:
    """A later case's planting: the shell files in the CLI home, the Node, and CODEX_HOME's config.toml with
    ``keys(the Node)``."""
    write_home(keys(codex_layers.plant_programs(CLI_HOME, NOTES)))
    as_controller.chown_home()


def _taken_out(value: str) -> bool:
    return value == MCP_SETTING or value in codex_layers.COMMAND_TOOL_SETTINGS or value in codex_layers.EXTRAS_SETTINGS


def _planted(adapter_class):
    class Planted(adapter_class):
        """The adapter, with an AGENTS.md planted in its call's directory before each command it runs there."""

        async def run_cli(self, *, command: list[str], cwd: str, **kwargs: object):
            codex_layers.plant_call_doc(cwd)
            return await super().run_cli(command=command, cwd=cwd, **kwargs)

    return Planted


def _without(adapter_class, taken_out):
    class Without(_planted(adapter_class)):
        """The adapter, with each -c setting ``taken_out`` names taken out of each command it runs."""

        async def run_cli(self, *, command: list[str], **kwargs: object):
            return await super().run_cli(command=codex_layers.without(command, taken_out), **kwargs)

    return Without


def _without_the_look(adapter_class):
    class WithoutTheLook(adapter_class):
        """The adapter, with its look at `codex features list` taken out: a control's call, whose settings are taken
        out, would be refused there."""

        async def _look_at_features(self, tempdir: str, deadline: float) -> None:
            return None

    return WithoutTheLook


def _without_the_lists(adapter_class):
    class WithoutTheLists(adapter_class):
        """The adapter, with its `codex mcp list` runs taken out: its exec turns off no server."""

        async def _mcp_servers_off(self, tempdir: str, deadline: float) -> list[str]:
            return []

    return WithoutTheLists


async def control(adapter_class, settings) -> None:
    """The adapter's call without its look, its MCP lists and settings, and its command-tool and extras settings."""
    NOTES.fresh()
    unguarded = _without_the_lists(_without_the_look(adapter_class))
    try:
        outcome = (await as_controller.decide(_without(unguarded, _taken_out)(settings))).decision.action
    except Exception as exc:  # noqa: BLE001 - the control's servers and tools are what it is read for
        outcome = f"raised {type(exc).__name__}: {exc}"
    started_marks = [f"{label} started" for label in STARTED_WITHOUT_ITS_SETTINGS]
    landed = stand_ins.wait_for(
        {NOTES.marker: started_marks, NOTES.extras: RAN_WITHOUT_ITS_SETTINGS, NOTES.telemetry: codex_layers.TELEMETRY}
    )
    started, requests = stand_ins.noted(NOTES.marker), stand_ins.logged(NOTES.model_log)
    ran, sent, held = codex_layers.extras_seen(NOTES, requests, IN_THE_REQUEST)
    print("=== control: the adapter's call without its MCP, command-tool and extras settings")
    print(f"servers: {started}; MCP tools offered: {stand_ins.mcp_tools(requests)}")
    print(f"command tools offered: {codex_layers.command_tools(requests)}; decision: {outcome}")
    codex_layers.show(ran, sent, held, BUNDLED_SKILLS_DIR)
    SETTLE_FILE.write_text(str(stand_ins.settle_seconds(landed)), encoding="utf-8")
    if not set(started_marks) <= set(started):
        fail("the planted servers did not all start without the adapter's settings: the check would prove nothing")
    codex_layers.offered_in_the_control(requests, STARTED_WITHOUT_ITS_SETTINGS, "the adapter's settings")
    if (
        set(ran) != set(RAN_WITHOUT_ITS_SETTINGS)
        or set(sent) != set(codex_layers.TELEMETRY)
        or held != list(IN_THE_REQUEST)
    ):
        fail("the planted extras did not all run, send or reach the model without the adapter's settings")
    codex_layers.skills_left(BUNDLED_SKILLS_DIR, SKILLS_FILE, "the adapter's settings")


async def features_control(adapter_class, settings) -> None:
    """The adapter's call without its look and its feature settings, on the features case's planting."""
    NOTES.fresh()
    without = _without(_without_the_look(adapter_class), codex_layers.FEATURE_SETTINGS.__contains__)
    try:
        outcome = (await as_controller.decide(without(settings))).decision.action
    except Exception as exc:  # noqa: BLE001 - the control's tools and programs are what it is read for
        outcome = f"raised {type(exc).__name__}: {exc}"
    landed = stand_ins.wait_for({NOTES.extras: codex_layers.RAN_WITH_THE_FEATURES})
    requests, ran = stand_ins.logged(NOTES.model_log), stand_ins.noted(NOTES.extras)
    print(f"=== control: the adapter's call without its feature settings{FEATURES}")
    print(f"tools offered: {stand_ins.offered(requests)}; decision: {outcome}")
    print(f"ran: {ran}")
    first = float(SETTLE_FILE.read_text(encoding="utf-8"))
    seconds = max(stand_ins.settle_seconds(landed), first)
    print(f"the features case's decisions wait {seconds:.1f} s, the longer of that and the first control's wait")
    SETTLE_FILE.write_text(str(seconds), encoding="utf-8")
    codex_layers.features_in_the_control(requests, ran, "the adapter's feature settings")


async def zsh_path_control(adapter_class, settings) -> None:
    """The adapter's call without its look and its command-tool settings, on the zsh_path case's planting: its model is
    offered the command tool of shell_zsh_fork with a zsh_path (codex_layers.ZSH_FORK_TOOL)."""
    NOTES.fresh()
    without = _without(_without_the_look(adapter_class), codex_layers.COMMAND_TOOL_SETTINGS.__contains__)
    try:
        outcome = (await as_controller.decide(without(settings))).decision.action
    except Exception as exc:  # noqa: BLE001 - the control's tools are what it is read for
        outcome = f"raised {type(exc).__name__}: {exc}"
    requests = stand_ins.logged(NOTES.model_log)
    print(f"=== control: the adapter's call without its look and its command-tool settings{ZSH_PATH}")
    print(f"tools offered: {stand_ins.offered(requests)}; decision: {outcome}")
    print(f"ran: {stand_ins.noted(NOTES.extras)}")
    without_them = "the adapter's look and its command-tool settings"
    codex_layers.tool_in_the_control(requests, (codex_layers.ZSH_FORK_TOOL,), without_them)


async def zsh_fork_refused(adapter_class, settings) -> None:
    """The adapter's call with shell_zsh_fork on and no zsh_path; its notes read SETTLE_FILE's wait after it
    returned."""
    NOTES.fresh()
    try:
        result = await as_controller.decide(_planted(adapter_class)(settings))
        outcome, decided, said = f"decision: {result.raw_text.strip()}", True, ""
    except Exception as exc:  # noqa: BLE001 - what Codex said is what the line is read for
        outcome, decided, said = f"raised {type(exc).__name__}", False, str(exc)
    await asyncio.sleep(float(SETTLE_FILE.read_text(encoding="utf-8")))
    codex_layers.zsh_fork_refused("adapter", outcome, decided, said, NOTES)


async def through_the_adapter(
    adapter_class, settings, heading: str, tools: tuple[str, ...] = codex_layers.DECISION_TOOLS
) -> None:
    """One decision, held to starting the commands of a decision (codex_layers.decision_children), each under the
    launcher, starting no server, offering its model ``tools`` and no other, running, sending and passing on none of
    the extras, keeping the bundled skills and coming back."""
    NOTES.fresh()
    with as_controller.children_started() as children:
        result = await as_controller.decide(_planted(adapter_class)(settings))
    await asyncio.sleep(float(SETTLE_FILE.read_text(encoding="utf-8")))
    started, requests = stand_ins.noted(NOTES.marker), stand_ins.logged(NOTES.model_log)
    ran, sent, held = codex_layers.extras_seen(NOTES, requests, IN_THE_REQUEST)
    offered = stand_ins.offered(requests)
    print(heading)
    print(f"servers: {started or 'none started'}; tools offered: {offered}; decision: {result.raw_text.strip()}")
    codex_layers.show(ran, sent, held, BUNDLED_SKILLS_DIR)
    expected = codex_layers.decision_children()
    as_controller.under_the_launcher(children, settings.openai_cli_path, expected, codex_layers.command_kind)
    if started:
        fail(f"the adapter's Codex started MCP servers: {started}")
    if not requests:
        fail("the model was never asked")
    stand_ins.offered_exactly(requests, tools)
    if ran or sent or held:
        fail(f"the adapter's Codex ran, sent or passed on planted extras: {ran + sent + held}")
    codex_layers.skills_kept(BUNDLED_SKILLS_DIR, SKILLS_FILE, "adapter")
    if not _came_back(result):
        fail(f"the decision did not come back: {result.decision.action}; its text: {result.raw_text}")


def _came_back(result) -> bool:
    """Whether a call's result is the stand-in model's decision: the decision the adapter returned, and the text it
    read it from."""
    returned = result.decision.model_dump(mode="json", include=set(stand_ins.DECISION))
    return returned == stand_ins.DECISION and json.loads(result.raw_text) == stand_ins.DECISION


def setting_rows() -> dict[str, Shown | Pair | None]:
    """Each setting of the call (app.codex_config's CONFIG_OVERRIDES, as the code that runs has them) with its row in
    SETTING_EFFECTS; the check fails, naming each setting that has none, and each setting a row or a Pair's partner
    names that the call does not have."""
    from app.codex_config import CONFIG_OVERRIDES

    if missing := [setting for setting in CONFIG_OVERRIDES if setting not in SETTING_EFFECTS]:
        fail(f"the check's table names no effect for {missing}")
    named = [*SETTING_EFFECTS, *(row.partner for row in SETTING_EFFECTS.values() if isinstance(row, Pair))]
    if extra := [setting for setting in dict.fromkeys(named) if setting not in CONFIG_OVERRIDES]:
        fail(f"the check's table names effects of settings the call does not have: {extra}")
    return {setting: SETTING_EFFECTS[setting] for setting in CONFIG_OVERRIDES}


def _seen() -> Shown:
    """What the notes of a decision of the settings case show, as Shown names it."""
    requests = stand_ins.logged(NOTES.model_log)
    return Shown(
        tools=tuple(name for name in stand_ins.offered(requests) if name not in codex_layers.DECISION_TOOLS),
        ran=tuple(dict.fromkeys(stand_ins.noted(NOTES.extras))),
        sent=tuple(dict.fromkeys(stand_ins.noted(NOTES.telemetry))),
        held=tuple(stand_ins.held(requests, SETTINGS_MARKS)),
    )


async def shows(adapter_class, settings, setting: str, taken_out: tuple[str, ...], expected: Shown, doc: str) -> Shown:
    """One decision with each of ``taken_out`` taken out of each command it runs, its look taken out (each setting's
    effect is its exec's), and the call's doc planted as ``doc``; its notes are polled for, as a control's are. Fails,
    naming ``setting``, unless it shows ``expected`` and nothing else, its model offered each of DECISION_TOOLS;
    fails also unless its commands are a decision's but the look, each under the launcher, and the decision came back.
    Returns what it showed."""

    class TakenOut(_without_the_look(adapter_class)):
        async def run_cli(self, *, command: list[str], cwd: str, **kwargs: object):
            codex_layers.plant_call_doc(cwd, doc)
            kept = codex_layers.without(command, taken_out.__contains__)
            return await super().run_cli(command=kept, cwd=cwd, **kwargs)

    NOTES.fresh()
    out = " and ".join(taken_out)
    with as_controller.children_started() as children:
        try:
            result = await as_controller.decide(TakenOut(settings))
            outcome, came_back = result.decision.action, _came_back(result)
        except Exception as exc:  # noqa: BLE001 - what the decision shows is what it is read for
            outcome, came_back = f"raised {type(exc).__name__}: {exc}", False
    stand_ins.wait_for({NOTES.extras: expected.ran, NOTES.telemetry: expected.sent})
    offered, seen = stand_ins.offered(stand_ins.logged(NOTES.model_log)), _seen()
    planted = "" if doc == AGENTS_DOC else f", the call's doc planted as {doc}"
    print(f"=== {out} taken out{planted}: {seen}; decision: {outcome}")
    without_the_look = codex_layers.decision_children()[1:]
    as_controller.under_the_launcher(children, settings.openai_cli_path, without_the_look, codex_layers.command_kind)
    if any(name not in offered for name in codex_layers.DECISION_TOOLS) or any(
        set(got) != set(named) for got, named in zip(seen, expected)
    ):
        fail(f"with {out} taken out, a decision did not show what the table names for {setting}: {expected}")
    if not came_back:
        fail(f"with {out} taken out, the decision did not come back: {outcome}")
    return seen


async def each_setting_taken_out(adapter_class, settings, rows: dict[str, Shown | Pair | None]) -> None:
    """The settings case: for each setting of the call, one decision with it alone taken out, or the two of its Pair,
    each held to what its row names; first, the names of the settings whose row is NOT_SHOWN, in one line. Last, the
    notes read once more SETTLE_FILE's wait after the last decision, which must show what that decision showed."""
    print(f"=== no decision of the settings case shows an effect of: {[s for s, r in rows.items() if r is NOT_SHOWN]}")
    last = None
    for setting, row in rows.items():
        if isinstance(row, Pair):
            await shows(adapter_class, settings, setting, (setting, row.partner), row.shown, row.doc)
            last = await shows(adapter_class, settings, setting, (row.partner,), row.partner_alone, row.doc)
        elif row is not NOT_SHOWN:
            last = await shows(adapter_class, settings, setting, (setting,), row, AGENTS_DOC)
    wait = float(SETTLE_FILE.read_text(encoding="utf-8"))
    await asyncio.sleep(wait)
    again = _seen()
    print(f"=== the settings case's notes, read again {wait:.1f} s after its last decision: {again}")
    if again != last:
        fail(f"the settings case's notes changed in the {wait:.1f} s after its last decision: {last}, then {again}")


async def server_on_control(adapter_class, settings) -> None:
    """The adapter's call without its MCP settings (its project root and its lists of servers), on the managed server
    case's planting: it starts the managed server that names enabled = true."""
    NOTES.fresh()
    without = _without(_without_the_lists(adapter_class), MCP_SETTING.__eq__)
    try:
        outcome = (await as_controller.decide(without(settings))).decision.action
    except Exception as exc:  # noqa: BLE001 - the control's servers are what it is read for
        outcome = f"raised {type(exc).__name__}: {exc}"
    landed = stand_ins.wait_for({NOTES.marker: [f"{codex_layers.MANAGED_ON} started"]})
    started = stand_ins.noted(NOTES.marker)
    print(f"=== control: the adapter's call without its MCP settings{MANAGED_ON}")
    print(f"servers: {started}; decision: {outcome}")
    SETTLE_FILE.write_text(str(stand_ins.settle_seconds(landed)), encoding="utf-8")
    codex_layers.server_on_in_the_control(started, "the adapter's MCP settings")


async def features_on_control(adapter_class, settings, where: str) -> None:
    """The adapter's call without its look and every feature setting, on a case's planting that turns every feature
    on (``where`` says where): its model is offered the features' tools, and the shell files, the Node and CODEX_HOME's
    hook run."""
    NOTES.fresh()
    without = _without(_without_the_look(adapter_class), codex_layers.feature_setting)
    try:
        outcome = (await as_controller.decide(without(settings))).decision.action
    except Exception as exc:  # noqa: BLE001 - the control's tools and programs are what it is read for
        outcome = f"raised {type(exc).__name__}: {exc}"
    landed = stand_ins.wait_for({NOTES.extras: (*codex_layers.RAN_WITH_THE_FEATURES, codex_layers.HOME_HOOK_RAN)})
    requests, ran = stand_ins.logged(NOTES.model_log), stand_ins.noted(NOTES.extras)
    print(f"=== control: the adapter's call without its look and its feature settings{where}")
    print(f"tools offered: {stand_ins.offered(requests)}; decision: {outcome}")
    print(f"ran: {ran}")
    SETTLE_FILE.write_text(str(stand_ins.settle_seconds(landed)), encoding="utf-8")
    codex_layers.features_on_in_the_control(requests, ran, "the adapter's look and its feature settings")


async def refused(
    adapter_class,
    settings,
    heading: str,
    why: str,
    expected: str,
    *,
    settle: bool = True,
    children: tuple[str, ...] = codex_layers.REFUSED_AT_THE_LOOK,
) -> None:
    """One call the adapter must refuse with ``expected`` (codex_layers.refused) as a ProviderAPIError that is not
    retryable (codex_layers.refused_as), having started ``children`` (by default the look alone), no exec among them,
    each under the launcher; its notes read SETTLE_FILE's wait after it returned, or at once where not ``settle``."""
    if settle:
        NOTES.fresh()
    with as_controller.children_started() as started:
        try:
            result = await as_controller.decide(_planted(adapter_class)(settings))
            outcome, kind = f"decision: {result.raw_text.strip()}", "a decision"
        except Exception as exc:  # noqa: BLE001 - the refusal is what the call is read for
            outcome = getattr(exc, "message", f"raised {type(exc).__name__}: {exc}")
            kind = f"{type(exc).__name__} retryable={getattr(exc, 'retryable', None)}"
    if settle:
        await asyncio.sleep(float(SETTLE_FILE.read_text(encoding="utf-8")))
    print(heading)
    codex_layers.refused("adapter", why, outcome, expected, NOTES)
    codex_layers.refused_as("adapter", why, kind, REFUSED)
    as_controller.under_the_launcher(started, settings.openai_cli_path, children, codex_layers.command_kind)


async def each_feature_alone(adapter_class, settings, heading: str) -> None:
    """For each feature the look reads (each the call's settings turn off but unified_exec), that one alone turned on
    in a profile the home config selects: the call is refused, naming exactly it, and no exec runs, so their notes are
    read at once and once more after SETTLE_FILE's wait, and the case's time is printed; then unified_exec alone so:
    the call goes through as a decision does. CODEX_HOME's config.toml is written again for each, as this uid."""
    from app.codex_config import CONFIG_OVERRIDES, features_on_error

    node = codex_layers.node_path(NOTES)
    names = codex_layers.looked_at(CONFIG_OVERRIDES)
    NOTES.fresh()
    began = time.monotonic()
    for name in names:
        write_home(codex_layers.features_in_a_profile([name], node))
        why, expected = f"with {name} on in a profile the home config selects", features_on_error([name])
        heading_of_one = f"=== {name} alone on in a selected profile{heading}"
        await refused(adapter_class, settings, heading_of_one, why, expected, settle=False)
    print(f"=== {len(names)} calls, each with one feature on, refused in {time.monotonic() - began:.1f} s")
    await asyncio.sleep(float(SETTLE_FILE.read_text(encoding="utf-8")))
    print("=== the case's notes, read again after the wait")
    case = f"each with one feature the look reads on alone in a selected profile{heading}"
    codex_layers.none_after_the_wait("adapter", case, len(names), NOTES)
    write_home(codex_layers.features_in_a_profile([codex_layers.NOT_LOOKED_AT], node))
    suffix = f", with {codex_layers.NOT_LOOKED_AT} alone on in a profile the home config selects{heading}"
    await through_the_adapter(adapter_class, settings, DECISION_HEADING + suffix)


async def tool_control(adapter_class, settings, where: str, tools) -> None:
    """The adapter's call without its look, on a case's planting that turns a feature on (``where`` says where): its
    model is offered one of ``tools``."""
    NOTES.fresh()
    try:
        outcome = (await as_controller.decide(_planted(_without_the_look(adapter_class))(settings))).decision.action
    except Exception as exc:  # noqa: BLE001 - the control's tools are what it is read for
        outcome = f"raised {type(exc).__name__}: {exc}"
    requests = stand_ins.logged(NOTES.model_log)
    print(f"=== control: the adapter's call without its look{where}")
    print(f"tools offered: {stand_ins.offered(requests)}; decision: {outcome}")
    codex_layers.tool_in_the_control(requests, tools, "the adapter's look")


async def older_names(adapter_class, settings, heading: str) -> None:
    """Each of codex_layers.OLDER_NAMES alone on in CODEX_HOME's top-level [features] table, written again as this
    uid: a control, the adapter's call without its look, is offered the feature's tool; then the adapter's call with
    each such table is refused, naming the feature, and no exec runs, so their notes are read at once and once more
    after SETTLE_FILE's wait. Then codex_layers.OTHER_OLDER_NAMES on together there: the call goes through as a
    decision does."""
    from app.codex_config import features_on_error

    for older, (_feature, tool) in codex_layers.OLDER_NAMES.items():
        write_home(codex_layers.features_table([older]))
        await tool_control(adapter_class, settings, OLDER_NAME_ON.format(older) + heading, [tool])
    NOTES.fresh()
    for older, (feature, _tool) in codex_layers.OLDER_NAMES.items():
        write_home(codex_layers.features_table([older]))
        why, expected = OLDER_NAME_ON.format(older).removeprefix(", "), features_on_error([feature])
        heading_of_one = DECISION_HEADING + OLDER_NAME_ON.format(older) + heading
        await refused(adapter_class, settings, heading_of_one, why, expected, settle=False)
    await asyncio.sleep(float(SETTLE_FILE.read_text(encoding="utf-8")))
    print("=== the case's notes, read again after the wait")
    case = f"each with one older name on alone in the home config's top-level [features] table{heading}"
    codex_layers.none_after_the_wait("adapter", case, len(codex_layers.OLDER_NAMES), NOTES)
    write_home(codex_layers.features_table(codex_layers.OTHER_OLDER_NAMES))
    await through_the_adapter(adapter_class, settings, DECISION_HEADING + OTHER_OLDER_NAMES + heading)


async def required_on(adapter_class, settings) -> None:
    """With codex_layers.REQUIRED_ON turned on in a [features] table of /etc/codex/requirements.toml: a control, the
    adapter's call without its look, is offered a command tool; the adapter's call is refused, naming the feature."""
    from app.codex_config import features_on_error

    await tool_control(adapter_class, settings, REQUIRED, codex_layers.COMMAND_TOOLS)
    why, expected = REQUIRED.removeprefix(", "), features_on_error([codex_layers.REQUIRED_ON])
    await refused(adapter_class, settings, DECISION_HEADING + REQUIRED, why, expected)


def _reading_the_call(adapter_class, found: list[bool]):
    class ReadingTheCall(adapter_class):
        """The adapter, noting in ``found``, once its exec has returned, whether its call's directory holds the file a
        patch of the patch case adds there, before the call removes the directory."""

        async def run_cli(self, *, command: list[str], cwd: str, **kwargs: object):
            result = await super().run_cli(command=command, cwd=cwd, **kwargs)
            if "exec" in command:
                found.append((Path(cwd) / codex_layers.PATCHED_IN_THE_CALL).exists())
            return result

    return ReadingTheCall


async def patch_control(adapter_class, settings) -> None:
    """The patch case's control: the adapter's call made with CODEX_SANDBOX_MODE workspace-write (its as-cli run's
    settings) is offered apply_patch, Codex answers each of the stand-in model's patches as written, and both files are
    there."""
    NOTES.fresh()
    (TEMP / codex_layers.PATCHED_IN_TEMP).unlink(missing_ok=True)
    found: list[bool] = []
    control = _planted(_reading_the_call(adapter_class, found))
    try:
        outcome = (await as_controller.decide(control(settings))).decision.action
    except Exception as exc:  # noqa: BLE001 - what the control's patches wrote is what it is read for
        outcome = f"raised {type(exc).__name__}: {exc}"
    under = f"with CODEX_SANDBOX_MODE={settings.codex_sandbox_mode}"
    print(f"=== control: the adapter's call {under}{PATCH}")
    print(f"decision: {outcome}")
    written = codex_layers.patched_files(found, TEMP)
    codex_layers.patch_control(stand_ins.logged(NOTES.model_log), written, PATCH_TARGETS, under)


async def patches_held(adapter_class, settings, heading: str) -> None:
    """One decision of the patch case, held to what through_the_adapter holds a decision to, its model offered
    apply_patch as well; then Codex answered each of the stand-in model's patches with its refusal, none as written,
    the call's directory was read after its exec, and neither file is there."""
    (TEMP / codex_layers.PATCHED_IN_TEMP).unlink(missing_ok=True)
    found: list[bool] = []
    tools = (*codex_layers.DECISION_TOOLS, stand_ins.PATCH_TOOL)
    await through_the_adapter(_reading_the_call(adapter_class, found), settings, heading, tools)
    codex_layers.patches_held("adapter", stand_ins.logged(NOTES.model_log), found, TEMP, PATCH_TARGETS)


async def later_case(adapter_class, settings, case: list[str]) -> None:
    """The calls of an as-cli run of a later case, which ``case`` names: the case's control first where it has one,
    then its calls. Where ``case`` names WITH_REQUIREMENTS too, /etc/codex/requirements.toml is in place: the managed
    server case's call is then a decision, the profile's and the managed table's refused calls come with no control,
    and the older names' case makes its three controls again."""
    from app.codex_config import CONFIG_OVERRIDES, features_on_error
    from app.providers.openai_adapter import MCP_SERVERS_ON_ERROR

    in_place = codex_layers.IN_PLACE if case[1:] == [WITH_REQUIREMENTS] else ""
    every_feature = features_on_error(codex_layers.looked_at(CONFIG_OVERRIDES))
    if case[0] == MANAGED_ON_CASE and in_place:
        await through_the_adapter(adapter_class, settings, DECISION_HEADING + MANAGED_ON + in_place)
    elif case[0] == MANAGED_ON_CASE:
        await server_on_control(adapter_class, settings)
        why, heading = MANAGED_ON.removeprefix(", "), DECISION_HEADING + MANAGED_ON
        await refused(
            adapter_class,
            settings,
            heading,
            why,
            MCP_SERVERS_ON_ERROR,
            children=codex_layers.REFUSED_AT_THE_SECOND_LIST,
        )
    elif case[0] in (PROFILE_CASE, MANAGED_FEATURES_CASE):
        where = IN_A_PROFILE if case[0] == PROFILE_CASE else IN_THE_MANAGED_TABLE
        if not in_place:
            await features_on_control(adapter_class, settings, where)
        why = where.removeprefix(", ")
        await refused(adapter_class, settings, DECISION_HEADING + where + in_place, why, every_feature)
    elif case[0] == ONE_FEATURE_CASE:
        await each_feature_alone(adapter_class, settings, in_place)
    elif case[0] == OLDER_NAMES_CASE:
        await older_names(adapter_class, settings, in_place)
    elif case[0] == REQUIRED_CASE:
        await required_on(adapter_class, settings)
    else:
        await through_the_adapter(adapter_class, settings, DECISION_HEADING + OTHER_FEATURES + in_place)


async def as_cli(case: list[str]) -> None:
    """The calls of one as-cli run, which ``case`` (its arguments) names."""
    environment = {"OPENAI_API_KEY": "placeholder", "CODEX_HOME": str(CODEX_HOME)}
    mode = {"CODEX_SANDBOX_MODE": codex_layers.WORKSPACE_WRITE} if case == [PATCH_CONTROL] else {}
    adapter_class, settings = as_controller.controller(
        "openai_adapter.OpenAIAdapter", environment, OPENAI_AUTH_MODE="cli", **mode
    )
    as_controller.show_the_code(settings.openai_cli_path)
    rows = setting_rows()
    targets = PATCH_TARGETS if case[:1] in ([PATCH_CASE], [PATCH_CONTROL]) else ()
    with (
        stand_ins.running("responses", PORT, NOTES.model_log, *targets),
        stand_ins.running("listen", OTEL_PORT, NOTES.telemetry),
    ):
        if case == [SETTINGS_CASE]:
            await each_setting_taken_out(adapter_class, settings, rows)
        elif case == [WITH_REQUIREMENTS]:
            await through_the_adapter(adapter_class, settings, DECISION_HEADING + codex_layers.IN_PLACE)
        elif case == [FEATURES_CASE]:
            await features_control(adapter_class, settings)
            await through_the_adapter(adapter_class, settings, DECISION_HEADING + FEATURES)
        elif case == [FEATURES_CASE, WITH_REQUIREMENTS]:
            await through_the_adapter(adapter_class, settings, DECISION_HEADING + FEATURES + codex_layers.IN_PLACE)
        elif case == [ZSH_PATH_CASE]:
            await zsh_path_control(adapter_class, settings)
            await through_the_adapter(adapter_class, settings, DECISION_HEADING + ZSH_PATH)
        elif case == [ZSH_PATH_CASE, WITH_REQUIREMENTS]:
            await through_the_adapter(adapter_class, settings, DECISION_HEADING + ZSH_PATH + codex_layers.IN_PLACE)
        elif case == [codex_layers.ZSH_FORK]:
            await zsh_fork_refused(adapter_class, settings)
        elif case == [PATCH_CONTROL]:
            await patch_control(adapter_class, settings)
        elif case == [PATCH_CASE]:
            await patches_held(adapter_class, settings, DECISION_HEADING + PATCH)
        elif case == [PATCH_CASE, WITH_REQUIREMENTS]:
            await patches_held(adapter_class, settings, DECISION_HEADING + PATCH + codex_layers.IN_PLACE)
        elif case[:1] in ([MANAGED_ON_CASE], [PROFILE_CASE], [MANAGED_FEATURES_CASE], [ONE_FEATURE_CASE],
                          [OTHER_FEATURES_CASE], [OLDER_NAMES_CASE], [REQUIRED_CASE]):  # fmt: skip
            await later_case(adapter_class, settings, case)
        else:
            await control(adapter_class, settings)
            await through_the_adapter(adapter_class, settings, DECISION_HEADING)


def _as_cli(*case: str) -> None:
    if code := as_controller.run_as_cli(__file__, *case):
        raise SystemExit(code)


def _both_ways(case: str, in_the_image: bool) -> None:
    """The as-cli run of ``case`` with /etc/codex/requirements.toml moved aside, then the one with it in place where
    the image has it."""
    with codex_layers.requirements_aside():
        _as_cli(case)
    if not in_the_image:
        print(codex_layers.NOT_IN_THE_IMAGE)
    else:
        _as_cli(case, WITH_REQUIREMENTS)


def later_cases(names: list[str], in_the_image: bool) -> None:
    """The managed server case (with a home server that names enabled = false besides), then the features turned on
    in a profile the home config selects (every one but shell_zsh_fork; each the look reads alone, then unified_exec
    alone; every one the call does not turn off), then in the managed configuration's [features] table: each planted
    here, the managed configuration put back after its server's case."""
    from app.codex_config import CONFIG_OVERRIDES

    others = [name for name in names if name not in codex_layers.call_features(CONFIG_OVERRIDES)]
    managed = LAYERS["mgd"]
    plant_home(lambda node: codex_layers.server_off(NOTES))
    managed.write_text(codex_layers.managed_server_on(NOTES), encoding="utf-8")
    _both_ways(MANAGED_ON_CASE, in_the_image)
    managed.write_text(codex_layers.server_table("mgd", NOTES), encoding="utf-8")
    plant_home(lambda node: codex_layers.features_in_a_profile(names, node))
    _both_ways(PROFILE_CASE, in_the_image)
    plant_home(codex_layers.node_keys)  # each feature's profile is written by the as-cli run
    _both_ways(ONE_FEATURE_CASE, in_the_image)
    plant_home(lambda node: codex_layers.features_in_a_profile(others, node))
    _both_ways(OTHER_FEATURES_CASE, in_the_image)
    plant_home(codex_layers.node_keys)
    managed.write_text(codex_layers.managed_features(names, NOTES), encoding="utf-8")
    _both_ways(MANAGED_FEATURES_CASE, in_the_image)


def older_names_and_requirements(in_the_image: bool) -> None:
    """With the managed configuration back to its server alone: the older names' case (each table written by the
    as-cli run), with /etc/codex/requirements.toml moved aside, then in place where the image has it; then the
    requirements file's, /etc/codex/requirements.toml written for it and put back."""
    LAYERS["mgd"].write_text(codex_layers.server_table("mgd", NOTES), encoding="utf-8")
    plant_home(lambda node: "")
    _both_ways(OLDER_NAMES_CASE, in_the_image)
    plant_home(lambda node: "")
    with codex_layers.requirements_turning_on([codex_layers.REQUIRED_ON]):
        _as_cli(REQUIRED_CASE)


def patch_case(in_the_image: bool) -> None:
    """The patch case: CODEX_HOME's config.toml names codex_layers.PATCH_MODEL as its model; its control's as-cli run
    with /etc/codex/requirements.toml moved aside, then its decisions' both ways."""
    write_home("", codex_layers.PATCH_MODEL)
    as_controller.chown_home()
    with codex_layers.requirements_aside():
        _as_cli(PATCH_CONTROL)
    _both_ways(PATCH_CASE, in_the_image)


def main() -> None:
    as_controller.require_root("it plants /etc/codex's layers, then makes the call as uid 10002")
    plant()
    with codex_layers.requirements_aside() as in_the_image:
        _as_cli()
    if not in_the_image:
        print(codex_layers.NOT_IN_THE_IMAGE)
    else:
        _as_cli(WITH_REQUIREMENTS)
    names = feature_names()
    plant_features(names)
    with codex_layers.requirements_aside():
        _as_cli(FEATURES_CASE)
    if not in_the_image:
        print(codex_layers.NOT_IN_THE_IMAGE)
    else:
        _as_cli(FEATURES_CASE, WITH_REQUIREMENTS)
    plant_features(names, zsh_fork=True, zsh_path=True)
    _both_ways(ZSH_PATH_CASE, in_the_image)
    plant_features(names, zsh_fork=True)
    with codex_layers.requirements_aside():
        _as_cli(codex_layers.ZSH_FORK)
    plant_settings(names)
    with codex_layers.requirements_aside():
        _as_cli(SETTINGS_CASE)
    later_cases(names, in_the_image)
    older_names_and_requirements(in_the_image)
    patch_case(in_the_image)
    print("=== all held")


if __name__ == "__main__":
    if sys.argv[1:2] == [as_controller.AS_CLI_ROLE]:
        asyncio.run(as_cli(sys.argv[2:]))
    else:
        main()

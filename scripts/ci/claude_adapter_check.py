"""A Claude call of the controller's (ClaudeAdapter, CLAUDE_AUTH_MODE=cli) loads no MCP server, offers its model no
tool but the structured-output one, runs no hook and passes on no instruction file, end to end with the image's Claude
Code: the agent-cli-image workflow runs it in the controller image built with INSTALL_AGENT_CLIS=true, as root, with
scripts/ at /scripts.

The call runs as the controller runs it there: the image's controller code under /app (a directory earlier on
PYTHONPATH takes its place; the first line printed names the code that ran), the image's default settings besides
CLAUDE_AUTH_MODE=cli and a 180-second MODEL_REQUEST_TIMEOUT_SECONDS (so CLI_HOME /data/cli-home, the CLI sandbox on),
the controller's own temp directory as its start makes it, and the controller's uid 10002 with no new privileges and
no capabilities, as entrypoint.sh starts it. The decision fails unless the CLI sandbox's launcher started the child
its call started, as read from the call's own spawn. Only the planting below runs as root, and the plugin's install as
uid 10002. The model is reached through ANTHROPIC_BASE_URL in the env block of the home's .claude/settings.json, as an
operator's own settings name it.

MCP servers are planted where Claude Code reads them for a call in the CLI home, its working directory: user scope
("user") and the home's local scope ("local") in the home's .claude.json, the home's .mcp.json ("project"), approved
through enableAllProjectMcpServers in the home's .claude/settings.json, and a plugin's .mcp.json ("plugin"). The
plugin, installed with `claude plugin install` at user scope, also brings a SessionStart hook and a language server.
A SessionStart hook is in the home's .claude/settings.json and .claude/settings.local.json too. Instruction files: a
CLAUDE.md in the home's .claude and in the call's directory, a CLAUDE.local.md there, a rules file in the home's
.claude/rules and the auto memory's MEMORY.md, each holding a sentence the model's request is searched for. The home's
settings also set CLAUDE_CODE_SYNC_PLUGIN_INSTALL, so Claude Code readies its plugins before it makes the tool list,
and the language server's LSP tool is in it.

Control: the adapter's call with --strict-mcp-config and its --settings value taken out starts all four servers, its
model is offered the tool of each and the LSP tool, the three hooks run, the language server runs once the model calls
the LSP tool (on a .py file planted in the CLI home), and the request holds every planted sentence; it also measures
how long after its call returned every one of those notes was there. Then the adapter makes one decision, which must
start one child, `claude --print`, under the launcher, start no server, offer the model StructuredOutput (the one
--json-schema adds) and no other tool, run no hook and no language server, pass on no planted sentence, and come back.
Its notes are read SETTLE_SECONDS after its call returned, or SETTLE_FACTOR times the control's measure where that is
longer (cli_stand_ins.py's). The model, the servers, the planted program and the language server are
cli_stand_ins.py's.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import as_controller
import cli_stand_ins as stand_ins
from as_controller import CLI_HOME, NOTES
from cli_stand_ins import fail

PORT = 18083
CLAUDE_DIR = CLI_HOME / ".claude"
SERVERS = ("user", "local", "project", "plugin")
MCP_FLAG = "--strict-mcp-config"
# The child a decision's call starts, by the first of its words after the CLI.
CHILDREN = ("--print",)
SETTINGS_FLAG = "--settings"
LSP_TOOL = stand_ins.LSP_TOOL
MARKET = CLI_HOME / "plugin-market"
PLUGIN = "planted-plugin"
# The tool of each planted server as Claude Code offers it to the model: a plugin's server's under the plugin's name.
SERVER_TOOLS = tuple(
    stand_ins.server_tool(f"plugin_{PLUGIN}_plugin" if label == "plugin" else label) for label in SERVERS
)
SETTINGS_FILES = {"user": CLAUDE_DIR / "settings.json", "local": CLAUDE_DIR / "settings.local.json"}
RAN_WITHOUT_ITS_SETTINGS = tuple(f"{label} hook ran" for label in (*SETTINGS_FILES, "plugin"))
LANGUAGE_SERVER_RAN = "plugin language server ran"
# Each planted instruction file and the sentence it holds, which the model's request is searched for.
INSTRUCTIONS = {
    CLAUDE_DIR / "CLAUDE.md": "planted-claude-md-of-the-home",
    CLI_HOME / "CLAUDE.md": "planted-claude-md-of-the-call-directory",
    CLI_HOME / "CLAUDE.local.md": "planted-claude-local-md-of-the-call-directory",
    CLAUDE_DIR / "rules" / "planted.md": "planted-rules-file-of-the-home",
    # The auto memory's folder for the call's directory: its path, each / a -.
    CLAUDE_DIR / "projects" / str(CLI_HOME).replace("/", "-") / "memory" / "MEMORY.md": "planted-auto-memory",
}


def _stdio(label: str) -> dict:
    command, *args = stand_ins.server_command(label, NOTES.marker)
    return {"type": "stdio", "command": command, "args": args}


def _hooks(label: str) -> dict:
    command = shlex.join(stand_ins.mark_command(f"{label} hook", NOTES.extras))
    return {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}


def _write(path: Path, data: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _plant_plugin() -> None:
    """A marketplace in the CLI home with one plugin: a SessionStart hook, an MCP server and a language server."""
    plugin = MARKET / PLUGIN
    entry = {"name": PLUGIN, "source": f"./{PLUGIN}", "description": "planted"}
    _write(
        MARKET / ".claude-plugin" / "marketplace.json",
        {"name": "planted", "owner": {"name": "planted"}, "plugins": [entry]},
    )
    command, *args = stand_ins.language_server_command("plugin language server", NOTES.extras)
    server = {"command": command, "args": args, "extensionToLanguage": {".py": "python"}}
    _write(
        plugin / ".claude-plugin" / "plugin.json",
        {"name": PLUGIN, "version": "1.0.0", "lspServers": {"planted": server}},
    )
    _write(plugin / "hooks" / "hooks.json", {"hooks": _hooks("plugin")})
    _write(plugin / ".mcp.json", {"mcpServers": {"plugin": _stdio("plugin")}})


def plant() -> None:
    _write(
        CLI_HOME / ".claude.json",
        {
            "hasCompletedOnboarding": True,
            "mcpServers": {"user": _stdio("user")},
            "projects": {str(CLI_HOME): {"mcpServers": {"local": _stdio("local")}}},
        },
    )
    _write(CLI_HOME / ".mcp.json", {"mcpServers": {"project": _stdio("project")}})
    env = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{PORT}", "CLAUDE_CODE_SYNC_PLUGIN_INSTALL": "1"}
    _write(SETTINGS_FILES["user"], {"enableAllProjectMcpServers": True, "env": env, "hooks": _hooks("user")})
    _write(SETTINGS_FILES["local"], {"hooks": _hooks("local")})
    for path, sentence in INSTRUCTIONS.items():
        _write(path, sentence + "\n")
    _write(CLI_HOME / stand_ins.LSP_FILE, "planted = 1\n")
    _plant_plugin()
    NOTES.work.mkdir(exist_ok=True)
    as_controller.chown_home()
    for words in (("marketplace", "add", str(MARKET)), ("install", f"{PLUGIN}@planted")):
        try:
            installed = subprocess.run(
                [*as_controller.AS_CLI, "claude", "plugin", *words],
                env={**os.environ, "HOME": str(CLI_HOME)},
                cwd=CLI_HOME,
                capture_output=True,
                text=True,
                timeout=stand_ins.CALL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            fail(f"claude plugin {' '.join(words)}: no answer in {stand_ins.CALL_TIMEOUT_SECONDS:.0f} seconds")
        if installed.returncode:
            fail(f"claude plugin {' '.join(words)}: {installed.stdout}{installed.stderr}")
    as_controller.chown_home()


async def control(adapter_class, settings) -> float:
    """The adapter's call without its MCP flag and its settings; returns how long a decision waits for its notes."""

    class WithoutItsSettings(adapter_class):
        """The adapter, with --strict-mcp-config and its --settings value taken out of the command it runs."""

        async def run_cli(self, *, command: list[str], **kwargs: object):
            kept, words = [], iter(command)
            for word in words:
                if word == SETTINGS_FLAG:
                    next(words)
                elif word != MCP_FLAG:
                    kept.append(word)
            return await super().run_cli(command=kept, **kwargs)

    NOTES.fresh()
    try:
        outcome = (await as_controller.decide(WithoutItsSettings(settings))).decision.action
    except Exception as exc:  # noqa: BLE001 - the control's servers and tools are what it is read for
        outcome = f"raised {type(exc).__name__}: {exc}"
    landed = stand_ins.wait_for(
        {
            NOTES.marker: [f"{label} started" for label in SERVERS],
            NOTES.extras: (*RAN_WITHOUT_ITS_SETTINGS, LANGUAGE_SERVER_RAN),
        }
    )
    started, requests = stand_ins.noted(NOTES.marker), stand_ins.logged(NOTES.model_log)
    offered = stand_ins.offered(requests)
    ran, held = stand_ins.noted(NOTES.extras), stand_ins.held(requests, INSTRUCTIONS.values())
    print(f"=== control: the adapter's call without {MCP_FLAG} and its {SETTINGS_FLAG} value")
    print(f"servers: {started}; tools offered: {offered}; decision: {outcome}")
    print(f"ran: {ran}; in the request: {held}")
    wait = stand_ins.settle_seconds(landed)
    if not {f"{label} started" for label in SERVERS} <= set(started) or not set(SERVER_TOOLS) <= set(offered):
        fail(f"without {MCP_FLAG} the planted servers did not all start and offer tools: it would prove nothing")
    if LSP_TOOL not in offered:
        fail(f"without its {SETTINGS_FLAG} value the model was not offered the plugin's {LSP_TOOL} tool")
    if LANGUAGE_SERVER_RAN not in ran:
        fail(f"without its {SETTINGS_FLAG} value the {LSP_TOOL} call ran no language server: it would prove nothing")
    if not set(RAN_WITHOUT_ITS_SETTINGS) <= set(ran) or held != list(INSTRUCTIONS.values()):
        fail(f"without its {SETTINGS_FLAG} value the planted hooks did not all run or files did not reach the model")
    return wait


async def through_the_adapter(adapter_class, settings, wait: float) -> None:
    NOTES.fresh()
    with as_controller.children_started() as children:
        result = await as_controller.decide(adapter_class(settings))
    await asyncio.sleep(wait)
    started, requests = stand_ins.noted(NOTES.marker), stand_ins.logged(NOTES.model_log)
    offered = stand_ins.offered(requests)
    ran, held = stand_ins.noted(NOTES.extras), stand_ins.held(requests, INSTRUCTIONS.values())
    print("=== the adapter's decision")
    print(f"servers: {started or 'none started'}; tools offered: {offered}; decision: {result.decision.action}")
    print(f"ran: {ran}; in the request: {held}")
    as_controller.under_the_launcher(children, settings.claude_cli_path, CHILDREN)
    if started:
        fail(f"the adapter's Claude Code started MCP servers: {started}")
    if not requests:
        fail("the model was never asked")
    stand_ins.offered_exactly(requests, (stand_ins.STRUCTURED_OUTPUT,))
    if ran or held:
        fail(f"the adapter's Claude Code ran or passed on planted extras: {ran + held}")
    if result.decision.model_dump(mode="json", include=set(stand_ins.DECISION)) != stand_ins.DECISION:
        fail(f"the decision did not come back: {result.raw_text}")


async def as_cli() -> None:
    environment = {"ANTHROPIC_API_KEY": "placeholder"}
    adapter_class, settings = as_controller.controller(
        "claude_adapter.ClaudeAdapter", environment, CLAUDE_AUTH_MODE="cli"
    )
    as_controller.show_the_code(settings.claude_cli_path)
    if "ANTHROPIC_BASE_URL" in adapter_class(settings).cli_environment():
        fail("ANTHROPIC_BASE_URL is in the call's environment: the model would not be reached through the settings")
    with stand_ins.running("messages", PORT, NOTES.model_log):
        wait = await control(adapter_class, settings)
        await through_the_adapter(adapter_class, settings, wait)


def main() -> None:
    as_controller.require_root("it plants the CLI home for uid 10002, then makes the call as that uid")
    plant()
    if code := as_controller.run_as_cli(__file__):
        raise SystemExit(code)
    print("=== all held")


if __name__ == "__main__":
    if sys.argv[1:2] == [as_controller.AS_CLI_ROLE]:
        asyncio.run(as_cli())
    else:
        main()

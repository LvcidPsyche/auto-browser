"""The frame of the two checks of scripts/ci that make a call of the controller's own (codex_adapter_check.py,
claude_adapter_check.py) as the controller makes it in the image built with INSTALL_AGENT_CLIS=true: the planting as
root, then the check again as the controller's uid 10002 with no new privileges and no capabilities, as entrypoint.sh
starts it, which loads the controller's code as the image starts it, makes the call, and reads from each child the
call started what it is and whether the CLI sandbox's launcher started it.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import cli_stand_ins as stand_ins

CLI_UID = 10002
# As entrypoint.sh starts the controller: its uid and gid, no other group, no capability in any set, no new privileges.
AS_CLI = (
    "setpriv",
    f"--reuid={CLI_UID}",
    f"--regid={CLI_UID}",
    "--clear-groups",
    "--inh-caps=-all",
    "--ambient-caps=-all",
    "--no-new-privs",
    "--bounding-set=-all",
)
CONTROLLER_HOME = "/home/controller"
CONTROLLER = "/app"
CLI_HOME = Path("/data/cli-home")  # the image's default CLI_HOME
# In the CLI home, which the CLI sandbox lets the planted servers and programs write.
NOTES = stand_ins.Notes(CLI_HOME / "adapter-check")
AS_CLI_ROLE = "as-cli"


def require_root(why: str) -> None:
    if os.getuid() != 0:
        stand_ins.fail(f"run it as root: {why}")


def chown_home() -> None:
    for path in (CLI_HOME, *CLI_HOME.rglob("*")):
        os.chown(path, CLI_UID, CLI_UID, follow_symlinks=False)  # a link itself, as rglob lists it, not its target


def run_as_cli(script: str, *args: str) -> int:
    """``script`` run again with AS_CLI_ROLE and ``args``, as the controller's uid as entrypoint.sh starts it; its
    exit code."""
    command = [*AS_CLI, sys.executable, str(Path(script).resolve()), AS_CLI_ROLE, *args]
    return subprocess.run(command, env={**os.environ, "HOME": CONTROLLER_HOME}).returncode


def controller(adapter: str, environment: dict[str, str], **settings: object) -> tuple[type, object]:
    """The controller's code as the image starts it, for one call: the image's code under /app (a directory earlier on
    PYTHONPATH takes its place), its working directory and its own temp directory; then ``environment``, and the
    image's default settings besides ``settings`` and a CALL_TIMEOUT_SECONDS MODEL_REQUEST_TIMEOUT_SECONDS. Returns the
    adapter class ``adapter`` names (module.Class in app.providers) and the settings."""
    sys.path.append(CONTROLLER)  # after PYTHONPATH's directories
    import app
    from app import process_hardening
    from app.config import Settings

    module, name = adapter.split(".")
    adapter_class = getattr(importlib.import_module(f"app.providers.{module}"), name)
    os.chdir(Path(app.__file__).parents[1])  # where the image starts uvicorn
    process_hardening.use_private_temp_directory()  # the start's first step
    os.environ.update(environment)
    timeout = stand_ins.CALL_TIMEOUT_SECONDS
    return adapter_class, Settings(_env_file=None, MODEL_REQUEST_TIMEOUT_SECONDS=timeout, **settings)


def show_the_code(cli: str) -> None:
    """Prints the code that runs, the uid and ``cli``'s version."""
    import app

    version = subprocess.run([cli, "--version"], capture_output=True, text=True).stdout.strip()
    print(f"app: {app.__file__} | uid {os.getuid()} | {version}")


@contextmanager
def children_started() -> Iterator[list[list[str]]]:
    """Yields a list to which the command line of each child the provider's run_cli starts in the block is added as
    it is started: the spawn it makes, asyncio's create_subprocess_exec, is replaced by a wrap of it in the asyncio
    module itself, so for every caller in the process, for the block; what it starts is unchanged."""
    from app.providers import base

    children: list[list[str]] = []
    spawn = base.asyncio.create_subprocess_exec

    async def recorded(*command: str, **kwargs: object):
        children.append([str(word) for word in command])
        return await spawn(*command, **kwargs)

    base.asyncio.create_subprocess_exec = recorded
    try:
        yield children
    finally:
        base.asyncio.create_subprocess_exec = spawn


def _first_word(words: list[str]) -> str:
    return words[0] if words else ""


def under_the_launcher(
    children: list[list[str]],
    cli: str,
    expected: Sequence[str],
    kind: Callable[[list[str]], str] = _first_word,
) -> None:
    """Prints the children a call started, each named by ``kind`` from its words after ``cli`` (by default the first
    of them), and whether the CLI sandbox's launcher started each: a child counts as launched only where its words
    begin as cli_sandbox.launch_command's do (this interpreter, -I, -S and the launcher's path) and ``cli`` follows
    their first --. Fails unless the call started each of ``expected``, in that order, and nothing else, each so."""
    from app import cli_sandbox

    start = [sys.executable, "-I", "-S", cli_sandbox.LAUNCHER]

    def cli_words(command: list[str]) -> list[str] | None:
        if command[: len(start)] != start or "--" not in command:
            return None
        end = command.index("--")
        return command[end + 2 :] if command[end + 1 : end + 2] == [cli] else None

    words = [cli_words(command) for command in children]
    outside = [command for command, said in zip(children, words) if said is None]
    kinds = [kind(said) for said in words if said is not None]
    print(
        f"children the call started: {len(children)} ({', '.join(kinds)}) | under the launcher: "
        f"{bool(children) and not outside}"
    )
    if not children:
        stand_ins.fail("the check saw no child the call started")
    if outside:
        stand_ins.fail(f"the call started a child without the CLI sandbox's launcher: {outside}")
    if kinds != list(expected):
        stand_ins.fail(f"the call started {kinds}, not {list(expected)}")


async def decide(adapter) -> object:
    return await adapter.decide(goal="decide the next browser action", observation={"url": "https://example.com"})

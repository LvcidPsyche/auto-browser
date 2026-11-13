"""Whether a provider CLI starts under the Landlock launcher (cli_sandbox_launcher.py), and what is said where the
launcher cannot keep it from the controller's files.

A provider CLI runs as the controller's user, and a page the model was shown can talk it into reading what that user
can: the API token, the takeover token, the auth profiles, the witness and mesh keys. Started under the launcher it
can open nothing in PROTECTED_ROOT but its own home and the screenshot of its call.

Three things leave a secret open to it all the same:

- the launcher cannot restrict a process here: this is not Linux, the kernel has no Landlock, or a probe of the
  launcher failed. The CLI is started as it always was, without the launcher.
- the CLI home is PROTECTED_ROOT or holds it: a rule on that home would open the root, and the launcher refuses it.
  The CLI is started without the launcher.
- a setting of SECRET_PATH_SETTINGS names a place that is, once resolved, outside PROTECTED_ROOT or inside the CLI
  home. The CLI is started under the launcher, which does not close that place.

Each is one WARNING for the process that names the condition or the settings, never a value, and the CLI runs. With
CLI_SANDBOX_REQUIRED=true each is one ERROR and SandboxRequired: no CLI runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from .cli_sandbox_launcher import EXIT_NOT_APPLIED, MARKER
from .config import Settings
from .utils import error_text_for_log, quoted_for_log

logger = logging.getLogger(__name__)

# The controller's data directory, as the image and compose have it.
PROTECTED_ROOT = "/data"
LAUNCHER = str(Path(__file__).with_name("cli_sandbox_launcher.py"))
# The settings that say where a secret of the controller's is kept, by the name each has in the environment.
SECRET_PATH_SETTINGS = {
    "api_token_file": "API_TOKEN_FILE",
    "takeover_token_file": "TAKEOVER_TOKEN_FILE",
    "auth_root": "AUTH_ROOT",
    "witness_root": "WITNESS_ROOT",
    "mesh_identity_dir": "MESH_IDENTITY_DIR",
    "isolated_tunnel_key_path": "ISOLATED_TUNNEL_KEY_PATH",
    "proxy_persona_file": "PROXY_PERSONA_FILE",
    "browser_ws_endpoint_file": "BROWSER_WS_ENDPOINT_FILE",
}
PROBE_TIMEOUT_SECONDS = 30
# What the caller of a refused call is told. Neither names a path: the detail is in the controller log.
REQUIRED_MESSAGE = "CLI_SANDBOX_REQUIRED is set and the CLI sandbox would leave the controller's files open to the CLI"
NOT_APPLIED_MESSAGE = "the CLI sandbox could not be applied, and the CLI was not run"

_LINUX = sys.platform.startswith("linux")
_probe_lock = threading.Lock()
# The probe's answer once it has run: the launcher and the kernel stay what they are for as long as the process does.
_probed: list[str | None] = []
# What has been written already, so that each thing found is one line for the process.
_reported: set[tuple[bool, str]] = set()


class SandboxRequired(Exception):
    """CLI_SANDBOX_REQUIRED is set and the sandbox would leave a secret open: the CLI is not to be started."""


def _probe(env: dict[str, str]) -> str | None:
    """Why the launcher cannot restrict a process here, as one line of a log; None where a probe of it just did.

    The probe is the launcher itself, started as a CLI's is, applying the ruleset for PROTECTED_ROOT and exiting.
    """
    if not _LINUX:
        return "Landlock is the Linux kernel's, and this is not Linux"
    command = [sys.executable, "-I", "-S", LAUNCHER, "--protect", PROTECTED_ROOT, "--probe"]
    try:
        done = subprocess.run(  # noqa: S603 - this interpreter and the launcher beside this file
            command, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "the launcher's probe did not run: " + error_text_for_log(exc)
    if done.returncode != 0:
        said = quoted_for_log(done.stderr.decode("utf-8", errors="replace").strip())
        return "the launcher's probe exited %d: %s" % (done.returncode, said)
    return None


def _landlock_missing(env: dict[str, str]) -> str | None:
    """_probe's answer, asked for once however many calls start together."""
    with _probe_lock:
        if not _probed:
            _probed.append(_probe(env))
        return _probed[0]


def _within(path: str, directory: str) -> bool:
    """Whether ``path`` is, once resolved, ``directory`` or a place in it."""
    return Path(os.path.realpath(path)).is_relative_to(directory)


def _open_settings(settings: Settings, cli_home: str) -> list[str]:
    """The names of the settings whose secret the ruleset leaves open: each one that is set and resolves to a place
    outside PROTECTED_ROOT as it is written (a root that is a link holds nothing itself), or inside the CLI home."""
    home = os.path.realpath(cli_home)
    return [
        name
        for attribute, name in SECRET_PATH_SETTINGS.items()
        if (value := getattr(settings, attribute)) and (not _within(value, PROTECTED_ROOT) or _within(value, home))
    ]


def _report(required: bool, found: str) -> None:
    if (required, found) in _reported:
        return
    _reported.add((required, found))
    if required:
        logger.error("CLI_SANDBOX_REQUIRED is set, and no provider CLI is run: %s", found)
    else:
        logger.warning("%s. The CLI runs all the same: CLI_SANDBOX_REQUIRED=true refuses to run it instead", found)


async def launch_command(
    settings: Settings, command: list[str], *, cli_home: str, image_path: str | None, env: dict[str, str]
) -> list[str]:
    """``command`` as run_cli is to start it: under the launcher wherever the launcher can restrict a process, with
    ``cli_home`` to read and write and ``image_path``, where the call has one, to read; ``command`` itself elsewhere.

    ``env`` is the CLI's, which the probe is started with too. SandboxRequired where a secret would stay open and
    CLI_SANDBOX_REQUIRED is set.
    """
    missing = _probed[0] if _probed else await asyncio.to_thread(_landlock_missing, env)
    if missing is None and _within(PROTECTED_ROOT, os.path.realpath(cli_home)):
        missing = "CLI_HOME is %s or holds it, which the launcher refuses" % PROTECTED_ROOT
    if missing:
        found = "provider CLIs start without the Landlock sandbox and can read the controller's files: %s" % missing
    elif names := _open_settings(settings, cli_home):
        found = "the Landlock sandbox of provider CLIs does not close what these settings name, which lies outside "
        found += "%s or inside CLI_HOME: %s" % (PROTECTED_ROOT, ", ".join(names))
    else:
        found = ""
    if found:
        _report(settings.cli_sandbox_required, found)
        if settings.cli_sandbox_required:
            raise SandboxRequired
    if missing:
        return command
    grants = ["--rw", os.path.abspath(cli_home)]
    if image_path:
        grants += ["--ro-file", os.path.abspath(image_path)]
    return [sys.executable, "-I", "-S", LAUNCHER, "--protect", PROTECTED_ROOT, *grants, "--", *command]


def launcher_refused(returncode: int | None, stderr: str) -> bool:
    """Whether a child started under the launcher ended as the launcher does when it could not apply the ruleset:
    the CLI was not run."""
    return returncode == EXIT_NOT_APPLIED and stderr.startswith(MARKER)

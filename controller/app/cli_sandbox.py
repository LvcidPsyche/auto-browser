"""Whether a provider CLI starts under the Landlock launcher (cli_sandbox_launcher.py), and what is said where the
launcher cannot keep it from the controller's files.

A provider CLI runs as the controller's user, and a page the model was shown can talk it into reading what that user
can: the API token, the takeover token, the auth profiles, the witness and mesh keys. Started under the launcher it
can open nothing in PROTECTED_ROOT but its own home and the screenshot of its call.

Four things leave a secret open to it all the same:

- CLI_SANDBOX_ENABLED is false: the CLI is started without the launcher, and no probe is run.
- the launcher cannot restrict a process here: this is not Linux, the kernel has no Landlock, or a probe of the
  launcher failed. The CLI is started as it always was, without the launcher. The launcher's own answer is kept for
  the process; a probe that timed out, could not be started or ended any other way is run again at the next call.
- the CLI home is PROTECTED_ROOT or holds it: a rule on that home would open the root, and the launcher refuses it.
  The CLI is started without the launcher.
- a setting of SECRET_PATH_SETTINGS names a place that is, once resolved, outside PROTECTED_ROOT, or a path that lies
  in the CLI home as written or once resolved (the CLI can replace a link written there). The CLI is started under
  the launcher, which does not close that place.

Each is one WARNING for the process (one for each distinct text) that names the condition or the settings, never a
value, and the CLI runs. With CLI_SANDBOX_REQUIRED=true each is one ERROR and SandboxRequired: no CLI runs.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
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
# The probe once a call has asked for it, and its answer once it has run. Kept for the process only where the
# launcher itself answered: the launcher and the kernel stay what they are for as long as the process does, and a
# probe that timed out or could not be started says nothing of either. The calls that start together wait on this
# one future, and not in a thread each.
_asked: concurrent.futures.Future[str | None] | None = None
# What has been written already, so that each thing found is one line for the process.
_reported: set[tuple[bool, str]] = set()


class SandboxRequired(Exception):
    """CLI_SANDBOX_REQUIRED is set and the sandbox would leave a secret open: the CLI is not to be started."""


def _probe(env: dict[str, str]) -> tuple[str | None, bool]:
    """Why the launcher cannot restrict a process here, as one line of a log (None where a probe of it just did),
    and whether that is the launcher's own answer: its success, its refusal, or not Linux. A probe that timed out,
    could not be started or ended any other way is not.

    The probe is the launcher itself, started as a CLI's is, applying the ruleset for PROTECTED_ROOT and exiting.
    """
    if not _LINUX:
        return "Landlock is the Linux kernel's, and this is not Linux", True
    command = [sys.executable, "-I", "-S", LAUNCHER, "--protect", PROTECTED_ROOT, "--probe"]
    try:
        done = subprocess.run(  # noqa: S603 - this interpreter and the launcher beside this file
            command, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "the launcher's probe did not run: " + error_text_for_log(exc), False
    if done.returncode != 0:
        stderr = done.stderr.decode("utf-8", errors="replace")
        said = quoted_for_log(stderr.strip())
        return "the launcher's probe exited %d: %s" % (done.returncode, said), launcher_refused(done.returncode, stderr)
    return None, True


def _landlock_missing(env: dict[str, str]) -> concurrent.futures.Future[str | None]:
    """_probe's answer to come, asked for once however many calls start together.

    The probe runs in a thread of its own to its end, whoever has stopped waiting for it. Its future is running from
    the start, so that the cancel of one waiting call is not every waiting call's.
    """
    global _asked
    with _probe_lock:
        if _asked is None:
            asked: concurrent.futures.Future[str | None] = concurrent.futures.Future()
            asked.set_running_or_notify_cancel()
            # Kept only once its thread runs: a thread that cannot be started fails this call and no other.
            threading.Thread(target=_answer, args=(asked, env), name="cli-sandbox-probe").start()
            _asked = asked
        return _asked


def _forget(asked: concurrent.futures.Future[str | None]) -> None:
    """The next call asks again."""
    global _asked
    with _probe_lock:
        if _asked is asked:
            _asked = None


def _answer(asked: concurrent.futures.Future[str | None], env: dict[str, str]) -> None:
    """The probe's answer, to every call that waits for it, kept or not. An answer that is not kept is forgotten
    before it is given, so that a call that starts from then on asks again."""
    try:
        answer, kept = _probe(env)
    except BaseException as exc:
        _forget(asked)
        asked.set_exception(exc)
        return
    if not kept:
        _forget(asked)
    asked.set_result(answer)


def _within(path: str, directory: str) -> bool:
    """Whether ``path`` is, once resolved, ``directory`` or a place in it."""
    return Path(os.path.realpath(path)).is_relative_to(directory)


def _ways(path: str) -> tuple[str, str]:
    """``path`` as written (absolute and normalised) and once resolved."""
    return os.path.abspath(path), os.path.realpath(path)


def _open_settings(settings: Settings, cli_home: str) -> list[str]:
    """The names of the settings whose secret the ruleset leaves open: each one that is set and resolves to a place
    outside PROTECTED_ROOT as it is written (a root that is a link holds nothing itself), or whose path, as written or
    once resolved, lies in the CLI home as written or once resolved. A path written there is the CLI's to replace,
    wherever it resolves now."""
    homes = _ways(cli_home)
    return [
        name
        for attribute, name in SECRET_PATH_SETTINGS.items()
        if (value := getattr(settings, attribute))
        and (
            not _within(value, PROTECTED_ROOT)
            or any(Path(place).is_relative_to(home) for place in _ways(value) for home in homes)
        )
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
    if not settings.cli_sandbox_enabled:
        missing = "CLI_SANDBOX_ENABLED is false"
    else:
        asked = _asked if _asked is not None else _landlock_missing(env)
        missing = asked.result() if asked.done() else await asyncio.wrap_future(asked)
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

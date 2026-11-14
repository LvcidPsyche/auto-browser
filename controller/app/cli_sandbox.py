"""Whether a provider CLI starts under the Landlock launcher (cli_sandbox_launcher.py), and what is said where the
launcher cannot keep it from the controller's files.

A provider CLI runs as the controller's user, and a page the model was shown can talk it into reading what that user
can: the API token, the takeover token, the auth profiles, the witness and mesh keys. Started under the launcher it
can open, list and run nothing in PROTECTED_ROOT but its own home, what CLI_SANDBOX_WRITABLE names there and the
screenshot of its call; a stat, an inotify watch and a connection to a Unix socket there are not refused
(docs/deployment.md, "The CLI sandbox", lists what the sandbox does not stop). Outside the root it reads, lists and
runs what it did before, and writes only the directories _writable gives it: its home, its HOME, the temp
directories, /dev, and what CLI_SANDBOX_WRITABLE names. The controller is not under the launcher, and what a CLI
could leave in the controller's own home (a Python user site, an ssh config) the controller would run at its next
start.

Each of these is reported:

- CLI_SANDBOX_ENABLED is false: the CLI is started without the launcher, and no probe is run.
- the launcher cannot restrict a process here: this is not Linux, the kernel has no Landlock, or a probe of the
  launcher failed. The CLI is started as it always was, without the launcher. The launcher's own answer is kept for
  the process; a probe that timed out, could not be started or ended any other way is run again at the next call.
- the CLI home is PROTECTED_ROOT or holds it, as written with no '..' part: a rule on that home would open the root,
  and the launcher refuses it. The CLI is started without the launcher.
- the CLI home only resolves into PROTECTED_ROOT, or to it or what holds it, or to a place no directory given holds
  (a link, which a CLI that may write where the home is written may have made), or is not a directory, or has a
  '..' part (Linux reads it after the link before it, which the text cannot say). The CLI is started under the
  launcher without it, and cannot write its home: closed, not open.
- the controller's own home, as HOME or its passwd entry names it, is, holds or lies in a directory the CLI may
  write, but for a CLI home in it clear of what the controller reads at its start; or what it reads there (each
  home's .ssh and .local, and Python's user base, which PYTHONUSERBASE can put anywhere) is, holds or lies in such a
  directory, wherever it is. The CLI is started under the launcher.
- a setting of SECRET_PATH_SETTINGS names a place that is, once resolved, outside PROTECTED_ROOT, or a path that lies
  in a directory the CLI may write, as written or once resolved (the CLI can replace a link written there), or a
  directory that holds one, or a path with a '..' part (Linux reads it after any link before it, which the CLI may
  have made). The CLI is started under the launcher, which does not close that place.

Each is one WARNING for the process (one for each distinct text) that names the condition, and the CLI runs: a
secret's setting by its name and never its value, a home or a directory by its path. Where several hold, only the
first in this list is named: no Landlock, for one, hides an open setting until Landlock is there. With
CLI_SANDBOX_REQUIRED=true each is one ERROR and SandboxRequired: no CLI runs. An entry of CLI_SANDBOX_WRITABLE that
is not given is one WARNING too, and refuses nothing.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import site
import subprocess
import sys
import threading
from pathlib import Path

from .cli_sandbox_launcher import EXIT_NOT_APPLIED, MARKER
from .config import CLI_SANDBOX_PROTECTED_ROOT, Settings
from .utils import error_text_for_log, quoted_for_log

try:
    import pwd
except ImportError:  # not POSIX: no passwd entry to read
    pwd = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PROTECTED_ROOT = CLI_SANDBOX_PROTECTED_ROOT
LAUNCHER = str(Path(__file__).with_name("cli_sandbox_launcher.py"))
# What a CLI may write besides its home, its own temp directories and what CLI_SANDBOX_WRITABLE names: the shared temp
# directories, and /dev for /dev/null, /dev/shm and a pty. Each is given where it is a directory.
SHARED_WRITABLE = ("/tmp", "/var/tmp", "/dev")  # noqa: S108 - directories a CLI may write, no file made in them
# The variables of the CLI's environment that name its temp directories.
TEMP_VARIABLES = ("TMPDIR", "TEMP", "TMP")
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
REQUIRED_MESSAGE = (
    "CLI_SANDBOX_REQUIRED is set and the CLI sandbox would leave the controller's files open to the CLI, or run it "
    "without its home"
)
NOT_APPLIED_MESSAGE = "the CLI sandbox could not be applied, and the CLI was not run"

_LINUX = sys.platform.startswith("linux")
_probe_lock = threading.Lock()
# The probe once a call has asked for it, and its answer once it has run. Kept for the process only where the
# launcher itself answered: the launcher and the kernel stay what they are for as long as the process does, and a
# probe that timed out or could not be started says nothing of either. The calls that start together wait on this
# one future, and not in a thread each.
_asked: concurrent.futures.Future[str | None] | None = None
# What has been written already, so that each thing found is one line for the process: by whether
# CLI_SANDBOX_REQUIRED refused for it (None: a warning that refuses nothing), and its text.
_reported: set[tuple[bool | None, str]] = set()


class SandboxRequired(Exception):
    """CLI_SANDBOX_REQUIRED is set and one of the conditions the module's docstring lists holds: no sandbox, or one
    that leaves a secret, the controller's own home or what the controller reads at its start open to the CLI, or the
    CLI without its home. The CLI is not to be started."""


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


def _as_written(path: str) -> str:
    """``path`` absolute and normalised, two leading slashes read as one, as Linux reads them and config.py's check
    of CLI_SANDBOX_WRITABLE does: POSIX's abspath keeps them, and "//data/x" would lie in no "/data"."""
    written = os.path.abspath(path)
    return "/" + written.lstrip("/") if written.startswith("//") else written


def _climbs(path: str) -> bool:
    """Whether ``path`` has a '..' part. Linux reads it after the link before it, where _as_written drops it with the
    part before: a CLI that made that link would choose where the path leads."""
    return ".." in Path(path).parts


def _ways(path: str) -> tuple[str, str]:
    """``path`` as written (_as_written) and once resolved."""
    return _as_written(path), os.path.realpath(path)


def _lies_in(place: tuple[str, str], directory: tuple[str, str]) -> bool:
    """Whether ``place`` is ``directory`` or lies in it, each as written or once resolved, and ``place`` also with
    each leading part of it resolved and the rest as written: a link part-way along it (an alias such as /var/tmp
    linked to /tmp, which _writable keeps only resolved, or an operator's) can lead into ``directory``, where what
    lies past the link is the CLI's to swap."""
    written, real = place
    parts = Path(written).parts
    leads = [os.path.join(os.path.realpath(Path(*parts[:end])), *parts[end:]) for end in range(2, len(parts))]
    return any(Path(mine).is_relative_to(theirs) for mine in (written, real, *leads) for theirs in directory)


def _not_given(kind: str, written: str, real: str) -> str:
    """Why a directory (as written, and once resolved) is not given to a CLI to write; "" where it is.

    Never PROTECTED_ROOT or what holds it, and only a directory. In PROTECTED_ROOT ``kind`` "home" (the CLI home) and
    "setting" (an entry of CLI_SANDBOX_WRITABLE) are given only where they are written there too: one that only
    resolves there is a link that the CLI may have made. The others ("shared": HOME, the temp directories, /dev) never
    are: in the CLI home they are the home's, and elsewhere in the root they stay closed.
    """
    if not os.path.isdir(real):
        return "is not a directory"
    if Path(PROTECTED_ROOT).is_relative_to(real):
        return "resolves to %s, which is %s or holds it" % (real, PROTECTED_ROOT)
    if Path(real).is_relative_to(PROTECTED_ROOT) and (
        kind == "shared" or not Path(written).is_relative_to(PROTECTED_ROOT)
    ):
        return "resolves to %s, in %s" % (real, PROTECTED_ROOT)
    return ""


def _writable(settings: Settings, cli_home: str, env: dict[str, str]) -> tuple[list[tuple[str, str]], str]:
    """The directories a CLI under the launcher may write, each as written and once resolved: its home, its HOME,
    SHARED_WRITABLE and the temp directories of its environment, and the entries of CLI_SANDBOX_WRITABLE. Only those
    with no '..' part (_climbs) that _not_given passes, none twice and none that lies in another (whose rule covers
    it, and which a CLI could swap for a link). And why the CLI home is not among them, where it is refused or where,
    once resolved, it lies in none of them ("" where it does). An entry refused, or that once resolved lies in none of
    them, is one warning."""
    shared = [env.get("HOME", ""), *SHARED_WRITABLE, *(env.get(name, "") for name in TEMP_VARIABLES)]
    candidates = [
        ("home", cli_home),
        *(("shared", path) for path in shared if os.path.isabs(path)),
        *(("setting", path) for path in settings.cli_sandbox_writable_paths),
    ]
    refused = (
        "CLI_HOME %s %s: provider CLIs are not given it, and run under the Landlock sandbox without a home they can "
        "write (a CLI_HOME that is a link on purpose is to be set to its real path, one with a '..' part written "
        "without it, and one that is not a directory made one)"
    )
    not_given = "CLI_SANDBOX_WRITABLE names %s, which %s: provider CLIs are not given it to write"
    unheld = "resolves to %s, which no directory given to provider CLIs holds"
    given: list[tuple[str, str]] = []
    home_refused = ""
    entries: list[tuple[str, str]] = []
    for kind, path in candidates:
        entry = _ways(path)
        why = "has a '..' part, read after any link before it" if _climbs(path) else _not_given(kind, *entry)
        if why and kind == "home":
            home_refused = refused % (path, why)
        elif why and kind == "setting":
            _warn_once(not_given % (path, why))
        elif kind == "setting":
            entries.append((path, entry[1]))
        if why or any(_lies_in(entry, other) for other in given):
            continue
        given = [other for other in given if not _lies_in(other, entry)] + [entry]
    # A directory that holds an entry or the home as written takes its place (/tmp, for one in /tmp), and covers where
    # it is written and not where it leads: a link there, which a CLI may have made.
    for path, real in entries:
        if not any(Path(real).is_relative_to(other) for _, other in given):
            _warn_once(not_given % (path, unheld % real))
    home = _ways(cli_home)
    if not home_refused and not any(Path(home[1]).is_relative_to(real) for _, real in given):
        home_refused = refused % (cli_home, unheld % home[1])
    return given, home_refused


def _named(entry: tuple[str, str]) -> str:
    """A directory for the log: as written, and once resolved where that is another path."""
    written, real = entry
    return written if written == real else "%s (%s once resolved)" % (written, real)


def _passwd_home() -> str:
    """The controller's home as its passwd entry names it; "" where there is none (not POSIX, or a uid with none)."""
    if pwd is None:
        return ""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return ""


def _controllers_home_open(given: list[tuple[str, str]], cli_home: str) -> str:
    """Why the CLI may write the controller's own home or a place in it, or a place the controller reads at its start,
    for the log; "" where it may not.

    The home is judged as HOME names it and as the passwd entry does: ssh reads the entry's, and the controller's
    next start may have another HOME. A directory given that is, holds or lies in either is reported, but for the CLI
    home lying in it away from the places the controller reads at its start: a host run whose CLI_HOME cannot be made
    falls back to a private directory in the user's home (_private_cli_home), and a CLI_HOME set there is the CLI's
    own. Those places are each home's .ssh and .local, and Python's user base, which PYTHONUSERBASE can put anywhere:
    a directory given that is, holds or lies in one of them is reported wherever it is.
    """
    homes = [_ways(home) for home in dict.fromkeys((os.path.expanduser("~"), _passwd_home())) if os.path.isabs(home)]
    read_at_start = [_ways(os.path.join(home[0], name)) for home in homes for name in (".ssh", ".local")]
    read_at_start.append(_ways(site.getuserbase()))
    own = _ways(cli_home)
    found = (
        "provider CLIs may write %s under the Landlock sandbox, %s, the controller, which is not under the sandbox, "
        "may run at its next start, where it is a Python user site or an ssh config. Keep the controller's home, and "
        "what it reads at its start, out of what CLIs may write: CLI_HOME, CLI_SANDBOX_WRITABLE, the CLI's TMPDIR, "
        "TEMP and TMP, its HOME where CLI_HOME is blank, and the shared temp directories"
    )
    for entry, home in ((entry, home) for entry in given for home in homes):
        if _lies_in(home, entry):
            return found % (
                _named(entry),
                "and the controller's own home %s lies there: what a CLI leaves in it" % _named(home),
            )
        if _lies_in(entry, home) and entry != own:
            return found % (
                _named(entry),
                "which lies in the controller's own home %s: what a CLI leaves there" % _named(home),
            )
    for entry, place in ((entry, place) for entry in given for place in read_at_start):
        if _lies_in(entry, place) or _lies_in(place, entry):
            return found % (
                _named(entry),
                "and the controller reads %s at its start: what a CLI leaves there" % place[0],
            )
    return ""


def _open_settings(settings: Settings, given: list[tuple[str, str]]) -> list[str]:
    """The names of the settings whose secret the ruleset leaves open: each one that is set and has a '..' part
    (_climbs: where it leads is the CLI's to choose where it can make the link before it), or resolves to a place
    outside PROTECTED_ROOT as it is written (a root that is a link holds nothing itself), or whose path, as written or
    once resolved, lies in a directory the CLI may write as written or once resolved, or holds one (a secret that is
    a directory). A path written there is the CLI's to replace, wherever it resolves now."""
    return [
        name
        for attribute, name in SECRET_PATH_SETTINGS.items()
        if (value := getattr(settings, attribute))
        and (
            _climbs(value)
            or not _within(value, PROTECTED_ROOT)
            or any(_lies_in(_ways(value), entry) or _lies_in(entry, _ways(value)) for entry in given)
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


def _warn_once(found: str) -> None:
    if (None, found) not in _reported:
        _reported.add((None, found))
        logger.warning(found)


async def launch_command(
    settings: Settings, command: list[str], *, cli_home: str, image_path: str | None, env: dict[str, str]
) -> list[str]:
    """``command`` as run_cli is to start it: under the launcher wherever the launcher can restrict a process, with
    the directories of _writable to read and write and ``image_path``, where the call has one, to read; ``command``
    itself elsewhere.

    ``env`` is the CLI's, which the probe is started with too. SandboxRequired where one of the module's conditions
    holds and CLI_SANDBOX_REQUIRED is set. Where several conditions hold, the first is named.
    """
    if not settings.cli_sandbox_enabled:
        missing = "CLI_SANDBOX_ENABLED is false"
    else:
        asked = _asked if _asked is not None else _landlock_missing(env)
        missing = asked.result() if asked.done() else await asyncio.wrap_future(asked)
    # As written: a home that only resolves to the root or above it is a link a CLI may have made (_writable), and
    # one with a '..' part need not be what the text says (/data/x/.. is what holds x's target where x is a link).
    if missing is None and not _climbs(cli_home) and Path(PROTECTED_ROOT).is_relative_to(_as_written(cli_home)):
        missing = "CLI_HOME is %s or holds it, which the launcher refuses" % PROTECTED_ROOT
    given: list[tuple[str, str]] = []
    if missing:
        found = "provider CLIs start without the Landlock sandbox and can read the controller's files: %s" % missing
    else:
        given, found = _writable(settings, cli_home, env)
        found = found or _controllers_home_open(given, cli_home)
        if not found and (names := _open_settings(settings, given)):
            found = (
                "the Landlock sandbox of provider CLIs does not close what these settings name, which lies outside "
                "%s, in a directory the CLIs may write, or holds one, or is written with a '..' part (read after any "
                "link before it, which a CLI may have made): %s"
            ) % (PROTECTED_ROOT, ", ".join(names))
    if found:
        _report(settings.cli_sandbox_required, found)
        if settings.cli_sandbox_required:
            raise SandboxRequired
    if missing:
        return command
    grants = [option for _, directory in given for option in ("--rw", directory)]
    if image_path:
        grants += ["--ro-file", os.path.abspath(image_path)]
    return [sys.executable, "-I", "-S", LAUNCHER, "--protect", PROTECTED_ROOT, *grants, "--", *command]


def launcher_refused(returncode: int | None, stderr: str) -> bool:
    """Whether a child started under the launcher ended as the launcher does when it could not apply the ruleset:
    the CLI was not run."""
    return returncode == EXIT_NOT_APPLIED and stderr.startswith(MARKER)

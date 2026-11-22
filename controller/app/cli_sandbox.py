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
  launcher without it, with no rule for its home: it writes there only where another directory given holds where it
  resolves. Closed, not open.
- the controller's own home, as HOME or its passwd entry names it, is, holds or lies in a directory the CLI may write,
  but for a CLI home in it clear of what the controller reads at its start; or what it reads there (each home's .ssh and
  .local, Python's user base, which PYTHONUSERBASE can put anywhere, and the .env Settings reads from the working
  directory) or its own code and the programs it runs outside the sandbox (_code, the one list of them), or a place a
  variable it hands those programs names (_handed), is, holds or lies in such a directory, wherever it is; or the
  working directory was removed, and one of those read from it has a '..' part, so where it leads cannot be said (one
  without is not judged: nothing can be made where it leads; where Playwright's driver finds the browsers is judged,
  '..' or not). The CLI is started under the launcher.
- a setting of SECRET_PATH_SETTINGS or STORE_PATH_SETTINGS names a place that is, once resolved, outside
  PROTECTED_ROOT, or a path that lies in a directory the CLI may write, as written or once resolved (the CLI can
  replace a link written there), or a directory that holds one, or a path with a '..' part (Linux reads it after any
  link before it, which the CLI may have made). A store of DERIVED_STORE_PLACES, beside its setting's place, counts
  the same; SOCKET_PATH_SETTINGS's socket counts where it lies where the CLI may write, holds such a place, or has a
  '..' part, and not for lying outside PROTECTED_ROOT. The CLI is started under the launcher, which does not close
  that place.
- the controller's own temp directory (tempfile's: the one process_hardening.use_private_temp_directory made in
  PROTECTED_ROOT, or the system's where it could not) is, once resolved, outside PROTECTED_ROOT, or lies in or holds a
  directory the CLI may write. The CLI is started under the launcher, which does not close it.

Each is one WARNING for the process (one for each distinct text) that names the condition, and the CLI runs: a
setting by its name and never its value, a home or a directory by its path. Where several hold, only the
first in this list is named: no Landlock, for one, hides an open setting until Landlock is there. With
CLI_SANDBOX_REQUIRED=true each is one ERROR and SandboxRequired: no CLI runs. An entry of CLI_SANDBOX_WRITABLE that
is not given is one WARNING too, and refuses nothing, and so is the CLI's HOME, TMPDIR, TEMP or TMP, or a directory
of SHARED_WRITABLE that is there, that is not given and that no directory given holds.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.machinery
import itertools
import logging
import os
import re
import shlex
import site
import subprocess
import sys
import sysconfig
import tempfile
import threading
from os import PathLike
from pathlib import Path

from .cli_sandbox_launcher import EXIT_NOT_APPLIED, MARKER
from .config import CLI_SANDBOX_PROTECTED_ROOT, Settings
from .process_hardening import (
    PLAYWRIGHT_DRIVER_ENV_NAMES,
    PLAYWRIGHT_DRIVER_ENV_PREFIXES,
    TEMP_VARIABLES,
    TESSERACT_ENV_NAMES,
    TESSERACT_ENV_PREFIXES,
)
from .session_tunnel import AUTOSSH_ENV_PREFIXES
from .startup.extensions import runs_uv_command
from .utils import error_text_for_log, quoted_for_log, quoted_whole_for_log

try:
    import pwd
except ImportError:  # not POSIX: no passwd entry to read
    pwd = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PROTECTED_ROOT = CLI_SANDBOX_PROTECTED_ROOT
LAUNCHER = str(Path(__file__).with_name("cli_sandbox_launcher.py"))
# The programs the controller runs by name outside the sandbox, where exec finds them on its PATH: Tesseract, which
# pytesseract runs for an OCR (ocr.py), and autossh, a tunnel's (session_tunnel.py). The verifier's is
# HARNESS_UV_COMMAND's. A test ties the names to the modules that run them.
PROGRAMS_RUN_OUTSIDE = ("tesseract", "autossh")
# The base names every program the controller runs outside the sandbox gets (providers.base.CLI_BASE_ENV_NAMES) that
# name a place: the CA bundles and the shell (_handed). Not HOME and the Windows home names, judged as the controller's
# home; nor TMPDIR, TEMP and TMP, which the controller sets to its own temp directory for these programs where it has
# one (process_hardening.child_environment); nor PATH, judged as exec searches it.
BASE_PLACE_NAMES = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "SHELL")
# The names handed to Playwright's driver that _handed does not judge as places both ways: XAUTHORITY not at all (the
# driver and its browser only read the cookie, which a CLI that may write it can read already, and xvfb-run puts it in
# /tmp), and XDG_RUNTIME_DIR only where it is, or lies in, a directory given (_runtime_directory).
_HANDED_OTHERWISE = frozenset({"XAUTHORITY", "XDG_RUNTIME_DIR"})
# What a CLI may write besides its home, its own temp directories and what CLI_SANDBOX_WRITABLE names: the shared temp
# directories, and /dev for /dev/null, /dev/shm and a pty. Each is given where it is a directory.
SHARED_WRITABLE = ("/tmp", "/var/tmp", "/dev")  # noqa: S108 - directories a CLI may write, no file made in them
# What separates the words of a command string a shell runs, as the CLI sandbox reads one: every ASCII whitespace, as
# env -S separates its words (shlex's own leaves out the vertical tab and the form feed).
_SHELL_WHITESPACE = " \t\r\n\v\f"
# The end of a URL's scheme in a ':' list's part, at its start or after an '=' or a ',' (http, --url=http, a,file): the
# part after it that starts with '//' but not '///' is the URL's host, and no path (_list_parts).
_URL_SCHEME_END = re.compile(r"(?:^|[=,])[A-Za-z][A-Za-z0-9+.-]*\Z")
# The links Linux follows in one path before it answers ELOOP (its MAXSYMLINKS).
_LINKS_FOLLOWED = 40
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
# The settings that say where the controller keeps a store of its own, checked as a secret's are: what it reads back
# from one (the approvals' digest key, a session, a job, a mesh peer, a cron, a revoked share link), or writes there as
# its record (the compliance manifest of its start), a CLI is no more to read or change than a secret.
STORE_PATH_SETTINGS = {
    "approval_root": "APPROVAL_ROOT",
    "audit_root": "AUDIT_ROOT",
    "state_db_path": "STATE_DB_PATH",
    "session_store_root": "SESSION_STORE_ROOT",
    "job_store_root": "JOB_STORE_ROOT",
    "memory_root": "MEMORY_ROOT",
    "harness_root": "HARNESS_ROOT",
    "mesh_peers_path": "MESH_PEERS_PATH",
    "mcp_session_store_path": "MCP_SESSION_STORE_PATH",
    "cron_store_path": "CRON_STORE_PATH",
    "share_revocation_store_path": "SHARE_REVOCATION_STORE_PATH",
    "isolated_tunnel_known_hosts_path": "ISOLATED_TUNNEL_KNOWN_HOSTS_PATH",
    "isolated_tunnel_info_root": "ISOLATED_TUNNEL_INFO_ROOT",
    "remote_access_info_path": "REMOTE_ACCESS_INFO_PATH",
    "artifact_root": "ARTIFACT_ROOT",
    "upload_root": "UPLOAD_ROOT",
    "compliance_manifest_path": "COMPLIANCE_MANIFEST_PATH",
}
# The stores the controller keeps where no setting names them, by their name and the store setting beside whose place,
# once resolved, the code that keeps one finds it: the isolated sessions' runtime root (session_isolation: each
# session's browser profile and the endpoint file the controller takes for its browser's), and the witness keys' place
# where WITNESS_ROOT cannot be made (browser_manager). Checked as a store is (_derived_store_places).
DERIVED_STORE_PLACES = {"browser-sessions": "artifact_root", "witness": "audit_root"}
# The Unix socket the controller sends Codex's calls to, and takes what answers there for the host bridge. A CLI
# connects to a socket wherever it is (below Landlock ABI 9, Linux 7.1, there is no right for a connect, and the
# launcher does not ask for ABI 9's yet), so one outside PROTECTED_ROOT is no more open than one in it; what is checked,
# as for a secret, is whether the CLI may put a socket of its own in its place.
SOCKET_PATH_SETTINGS = {"openai_host_bridge_socket": "OPENAI_HOST_BRIDGE_SOCKET"}
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
    """CLI_SANDBOX_REQUIRED is set and one of the conditions the module's docstring lists holds: the CLI is not to be
    started."""


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
    """``path`` absolute as written, a relative one read from the working directory, and its leading slashes read as
    one, as Linux reads them and config.py's check of CLI_SANDBOX_WRITABLE does ("//data/x" would lie in no "/data").
    What Linux reads the same is written one way, as Path writes it: a trailing '/', a '/.' and a '//' inside it are
    dropped, so that '/srv/link/' is the home '/srv/link' and is said once. A '..' is kept: Linux reads it after
    the link before it, which _steps follows, and normalising drops that link with the '..'."""
    written = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
    return str(Path("/" + written.lstrip("/") if written.startswith("//") else written))


def _climbs(path: str) -> bool:
    """Whether ``path`` has a '..' part. Linux reads it after the link before it: a CLI that made that link would
    choose where the path leads."""
    return ".." in Path(path).parts


def _ways(path: str) -> tuple[str, str]:
    """``path`` as written (_as_written) and once resolved."""
    return _as_written(path), os.path.realpath(path)


def _steps(path: str) -> list[str]:
    """Each place Linux looks up on its way to where ``path`` leads: each part of it after where the parts before it
    led, and so each part of a link's target in turn, a link there among them. A CLI that may write where one of them
    lies chooses what is there, and so where ``path`` leads, though neither ``path`` nor where it leads lies there
    (an operator's link to a link in /tmp)."""
    resolved, *parts = Path(path).parts
    pending, steps, followed = parts[::-1], [], 0
    while pending and followed <= _LINKS_FOLLOWED:
        part = pending.pop()
        if part == "..":
            resolved = str(Path(resolved).parent)
            continue
        step = _as_written(os.path.join(resolved, part))
        steps.append(step)
        try:
            target = Path(os.readlink(step))
        except OSError:  # no link there: Linux looks the next part up in it
            resolved = step
            continue
        followed += 1
        if target.is_absolute():
            resolved = target.anchor
        pending += target.parts[1 if target.is_absolute() else 0 :][::-1]
    return steps


def _places(place: tuple[str, str]) -> set[Path]:
    r"""Where _in looks for ``place`` (as written, once resolved, with each leading part resolved and the rest as
    written, and at each of its _steps), each with the directories it lies in: what Path.is_relative_to compares, so
    that a comparison is one look-up. A link part-way along the place (an alias such as /var/tmp linked to /tmp, which
    _writable keeps only resolved, or an operator's), or along a link's target, can lead into a directory where what
    lies past the link is the CLI's to swap. Working them out asks the file system about each part of ``place`` at
    each CLI call, so a place compared with several directories is worked out once for all of them.

    A spelling with a '..' part adds nothing: its text lies in what the '..' leaves, and where Linux reads it is
    its real path and its steps. A step adds the directories it lies in, not itself: a CLI that may write a directory
    can swap what lies in it, not the directory, so a '..' through a directory given (/srv/tools/../controller) leads
    back out where nothing of the CLI's is looked up, and a link the CLI makes in it (/srv/tools/l/..) is a step that
    lies there.

    The leads (each leading part resolved, the rest as written) are for a Windows host: there os.readlink gives a
    junction's target with a `\\?\` prefix, which _steps carries on and no given path matches, and the leads, through
    realpath, catch it (five host rows failed without them). On Linux they add nothing (a fuzz of 190,565 pairs
    on the image's file system found no verdict they change)."""
    written, real = place
    parts = Path(written).parts
    leads = [os.path.join(os.path.realpath(Path(*parts[:end])), *parts[end:]) for end in range(2, len(parts))]
    spellings = {Path(path) for path in (written, real, *leads) if not _climbs(path)}
    spelt = {held for path in spellings for held in (path, *path.parents)}
    return spelt | {held for step in map(Path, _steps(written)) for held in step.parents}


def _in(places: set[Path], directory: tuple[str, str]) -> bool:
    """Whether a place, by its ``places`` (_places), is ``directory`` or lies in it, as written or once resolved."""
    return any(Path(theirs) in places for theirs in directory)


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
        return "resolves to %s, which is %s or holds it" % (quoted_whole_for_log(real), PROTECTED_ROOT)
    if Path(real).is_relative_to(PROTECTED_ROOT) and (
        kind == "shared" or not Path(written).is_relative_to(PROTECTED_ROOT)
    ):
        return "resolves to %s, in %s" % (quoted_whole_for_log(real), PROTECTED_ROOT)
    return ""


def _writable(settings: Settings, cli_home: str, env: dict[str, str]) -> tuple[list[tuple[str, str]], str]:
    """The directories a CLI under the launcher may write, each as written and once resolved: its home, its HOME,
    SHARED_WRITABLE and the temp directories of its environment, and the entries of CLI_SANDBOX_WRITABLE. Only those
    with no '..' part (_climbs) that _not_given passes, none twice and none that lies in another (whose rule covers
    it, and which a CLI could swap for a link). And why the CLI home is not among them, where it is refused or where,
    once resolved, it lies in none of them ("" where it does). An entry refused, or that once resolved lies in none of
    them, is one warning, but for a directory that is the CLI home, which the home's own warning names. A directory
    of SHARED_WRITABLE that is not there (a host with no /var/tmp) is no candidate."""
    candidates = [
        ("home", "CLI_HOME", cli_home),
        ("shared", "the CLI's HOME names", env.get("HOME", "")),
        *(("shared", "the shared directory", path) for path in SHARED_WRITABLE if os.path.isdir(path)),
        *(("shared", f"the CLI's {name} names", env.get(name, "")) for name in TEMP_VARIABLES),
        *(("setting", "CLI_SANDBOX_WRITABLE names", path) for path in settings.cli_sandbox_writable_paths),
    ]
    refused = (
        "CLI_HOME %s %s: provider CLIs are not given it, and run under the Landlock sandbox with no rule for their "
        "home (a CLI_HOME that is a link on purpose is to be set to its real path, one with a '..' part written "
        "without it, and one that is not a directory made one)"
    )
    not_given = "%s %s, which %s: provider CLIs are not given it to write"
    unheld = "resolves to %s, which no directory given to provider CLIs holds"
    home = _ways(cli_home)
    passed: list[tuple[str, str]] = []
    home_refused = ""
    # The directories named, each with why it is not given ("" where it passed), to be said where in the end it is
    # not given: as written, or where it resolves, since a directory given that holds it takes its place.
    to_say: list[tuple[str, str, str, str]] = []
    for kind, label, path in candidates:
        if kind == "shared" and not os.path.isabs(path):
            continue
        entry = _ways(path)
        why = "has a '..' part, read after any link before it" if _climbs(path) else _not_given(kind, *entry)
        if why and kind == "home":
            home_refused = refused % (quoted_whole_for_log(path), why)
        elif kind != "home" and entry != home:  # the home's own line names it
            if why and kind == "setting":
                _warn_once(not_given % (label, quoted_whole_for_log(path), why))
            else:
                to_say.append((label, path, entry[1], why))
        if not why:
            passed.append(entry)
    given = _outermost(passed)
    for label, path, real, why in to_say:
        if not any(Path(real).is_relative_to(other) for _, other in given):
            _warn_once(not_given % (label, quoted_whole_for_log(path), why or unheld % quoted_whole_for_log(real)))
    if not home_refused and not any(Path(home[1]).is_relative_to(real) for _, real in given):
        home_refused = refused % (quoted_whole_for_log(cli_home), unheld % quoted_whole_for_log(home[1]))
    return given, home_refused


def _outermost(passed: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Of the directories ``passed``, in the order they are kept, those that lie in no other one kept (_in), each
    as written and once resolved.

    A directory that holds another as written takes its place (/tmp, for one in /tmp), and covers where that one is
    written and not where it leads: a link there, which a CLI may have made. So one passed over for lying in a
    directory that a later one then takes the place of is judged again against what is kept: a TMPDIR where HOME
    leads, with HOME a link in an entry of CLI_SANDBOX_WRITABLE, has a rule of its own. The rounds end at one that
    changes nothing, and at one round per directory: links that lead round in a ring could go on, and stopping
    leaves a directory without a rule, closed, not open.
    """
    places = {entry: _places(entry) for entry in passed}
    given: list[tuple[str, str]] = []
    for _ in passed:
        before = given
        for entry in passed:
            if any(_in(places[entry], other) for other in given):
                continue
            given = [other for other in given if not _in(places[other], entry)] + [entry]
        if given == before:
            break
    return given


def _named(entry: tuple[str, str]) -> str:
    """A directory for the log, quoted: as written, and once resolved where that is another path."""
    written, real = entry
    if written == real:
        return quoted_whole_for_log(written)
    return "%s (%s once resolved)" % (quoted_whole_for_log(written), quoted_whole_for_log(real))


def _passwd_home() -> str:
    """The controller's home as its passwd entry names it; "" where there is none (not POSIX, or a uid with none)."""
    if pwd is None:
        return ""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return ""


def _env_files() -> list[str]:
    """The files Settings reads its values from at the start (its env_file), as pydantic-settings reads them: a str or
    PathLike is one file and anything else several (sources/providers/dotenv.py), each by its path (os.fspath, not its
    text) with a leading '~' expanded (Path.expanduser), and none at a name whose path is "." once that is
    expanded, as pydantic-settings expands it before it looks for a file ("", ".", "./", "././", ".//", and "~/" with
    HOME "."), the working directory, which is no file; a relative one is read from the working
    directory (_read_from)."""
    # env_file alone: Settings' other ways to find a file at its start (env_file_depth's search of the working
    # directory's parents, secrets_dir, a JSON, YAML or TOML source) stay at pydantic-settings' defaults, which
    # test_settings_finds_no_file_at_its_start_but_by_env_file holds.
    names = Settings.model_config.get("env_file") or ()
    paths = map(os.fspath, (names,) if isinstance(names, str | PathLike) else names)
    return [path for path in map(os.path.expanduser, paths) if Path(path) != Path(".")]


def _code(verifier: str, cwd: str | None) -> list[str]:
    """Where the controller runs its code from, wherever that is (a host run from a checkout or a venv in /tmp, say):
    its package, with the launcher it runs at each call, the interpreter as it starts the launcher, the directories of
    its standard library and its packages, each entry of its import path (sys.path: the directory it was started from,
    PYTHONPATH's entries, what a .pth file adds), each directory exec searches (os.get_exec_path: its PATH, or
    os.defpath, /bin and /usr/bin, where PATH is not set), and each program it runs outside the sandbox where exec finds
    it (PROGRAMS_RUN_OUTSIDE, and the ``verifier`` command's program, HARNESS_UV_COMMAND's first word where
    HARNESS_VERIFIER runs it, "" elsewhere): in every directory exec searches, or as written where the name has a '/';
    what each later word of that command names, as _named_by judges it (the verifier runs outside the sandbox, so a
    link a CLI leaves at a script it runs or a file it reads or writes takes what it writes where the CLI chose); the
    working directory itself wherever the verifier runs,
    whatever its command: Python puts it, or a directory in it, first on the verifier's import path in most ways it runs
    (under -m, where runpy imports standard-library modules from it before the module runs: collections, functools
    and nine more; under -c, from stdin, for a script run from there, for an empty part of a PYTHONPATH list), and
    pytest collects every test_*.py under it, so a directory given that is, holds or lies in it
    is reported, as the controller's own '.' is (a script named by its path elsewhere, which puts its own directory
    first, is over-judged so); and where Playwright's driver finds the browsers (_browsers). Each as written, and one
    that is relative, the empty one among them, read from the working directory ``cwd``, or kept as written where that
    was removed (_read_from); _ways and _places follow a link at each to where it leads. And the bytecode prefix where
    one is set (PYTHONPYCACHEPREFIX): Python imports a .pyc it finds there for a module in any of these.

    Not sys.prefix or sys.base_prefix whole: a distribution's Python has /usr for both, and a directory a CLI may
    write in /usr away from these (/usr/local/lib/node_modules, say) holds none of this but where a program the
    controller runs leads there (a verifier npm links into /usr/local/bin)."""
    installed = sysconfig.get_paths()
    code = [str(Path(LAUNCHER).parent), LAUNCHER, sys.executable]
    code += [installed[name] for name in ("stdlib", "platstdlib", "purelib", "platlib")]
    code += [_read_from(cwd, entry) for entry in (*sys.path, *os.get_exec_path())]
    words = _verifier_words(verifier)
    for name in (*PROGRAMS_RUN_OUTSIDE, *words[:1]):
        code += [_read_from(cwd, name)] if "/" in name else _on_exec_path(name, cwd)
    code += _named_by(words[1:], cwd)
    if cwd is not None and words:
        code.append(cwd)
    if (browsers := _browsers(cwd)) is not None:
        code.append(browsers)
    if sys.pycache_prefix:
        code.append(_read_from(cwd, sys.pycache_prefix))
    return list(dict.fromkeys(code))


def _named_by(words: list[str], cwd: str | None) -> list[str]:
    """What ``words`` name, as a later word of the verifier's command and a value handed to a program run outside the
    sandbox are judged, by this one helper: of each, and of each word a shell reads in one (_command_words), the
    paths it may name (_paths_in: a script it runs, a file it reads or writes); of a word led by one '-', the same
    paths of the rest after its first letter where that holds a '/' (_after_first_letter; -I/srv/x:/a names
    /srv/x); and the files Python runs for each module it may name (_modules_named, _module_files); each read from
    the working directory ``cwd`` (_read_from), but for the root itself (_is_root); and each such word without a '/'
    as the program exec finds in each directory it searches (_on_exec_path), as the command's own program is
    (timeout 30 verify)."""
    places: list[str] = []
    for word in _command_words(words):
        rest = _after_first_letter(word)
        named = _paths_in(word) + (_paths_in(rest) if "/" in rest else [])
        named += [path for module in _modules_named(word) for path in _module_files(module)]
        places += [path for path in (_read_from(cwd, name) for name in named if name) if not _is_root(path)]
        if "/" not in word:
            places += _on_exec_path(word, cwd)
    return places


def _on_exec_path(name: str, cwd: str | None) -> list[str]:
    """Where exec looks for ``name``, a name without a '/': in each directory it searches (os.get_exec_path), each read
    from the working directory ``cwd`` (_read_from)."""
    return [os.path.join(_read_from(cwd, place), name) for place in os.get_exec_path()]


def _paths_in(word: str) -> list[str]:
    """The paths ``word`` may name, as _named_by reads a word: the word itself, its part after the first '='
    (--config=v.toml, env's PYTHONPATH=/srv/x, unix:path=/srv/x; "" where it has none), and each part of either as
    a ':' list (_list_parts)."""
    value = word.partition("=")[2]
    return [word, value, *(part for path in (word, value) for part in _list_parts(path))]


def _after_first_letter(word: str) -> str:
    """Of a word led by one '-', whatever flags lead it, the rest after its first letter (pytest's -pmod gives mod,
    -o/srv/x/out.json gives /srv/x/out.json); "" for any other word."""
    return word[2:] if word.startswith("-") and not word.startswith("--") else ""


def _handed(cwd: str | None, passed: list[str]) -> list[str]:
    """The places named by the variables the controller hands the programs it runs outside the sandbox, as its own
    environment has them: Tesseract's (TESSERACT_ENV_NAMES and _PREFIXES: TESSDATA_PREFIX, where it reads its
    parameters, file writes among them), Playwright's driver's (PLAYWRIGHT_DRIVER_ENV_NAMES and _PREFIXES:
    DEBUG_FILE, which it writes, and PLAYWRIGHT_NODEJS_PATH, the node it runs as the driver), but
    PLAYWRIGHT_BROWSERS_PATH, which _browsers judges as the driver reads it, and _HANDED_OTHERWISE, autossh's
    (AUTOSSH_ENV_PREFIXES: AUTOSSH_PATH, AUTOSSH_PIDFILE, AUTOSSH_LOGFILE), the verifier's ``passed`` names
    (HARNESS_UV_ENV_PASSTHROUGH, where it runs; PLAYWRIGHT_BROWSERS_PATH among them as the verifier gets it, raw; not
    HOME, judged as the controller's home, nor TEMP_VARIABLES, which the verifier gets as the controller's own
    temp directory, nor _HANDED_OTHERWISE) and BASE_PLACE_NAMES, each name matched as
    providers.base.allowlisted_environment matches it, upper-cased. A value that holds a '/' is judged as a later word
    of the verifier's command is (_named_by: NODE_OPTIONS=--require=/srv/x/hook.js, its words; unix:path=/srv/x, its
    part after '='); one with none is not (DEBUG_FILE=pw.log, in the working directory)."""
    names = {*TESSERACT_ENV_NAMES, *PLAYWRIGHT_DRIVER_ENV_NAMES, *BASE_PLACE_NAMES} - _HANDED_OTHERWISE
    prefixes = (*TESSERACT_ENV_PREFIXES, *PLAYWRIGHT_DRIVER_ENV_PREFIXES, *AUTOSSH_ENV_PREFIXES)
    passed_names = {name.upper() for name in passed} - {"HOME", *TEMP_VARIABLES, *_HANDED_OTHERWISE}
    values = [
        value
        for name, value in os.environ.items()
        if name.upper() in passed_names
        or (name != "PLAYWRIGHT_BROWSERS_PATH" and (name.upper() in names or name.upper().startswith(prefixes)))
    ]
    return _named_by([value for value in values if "/" in value], cwd)


def _runtime_directory(cwd: str | None) -> list[str]:
    """XDG_RUNTIME_DIR as the controller's environment has it, where it holds a '/' and is not the root itself, read
    from the working directory ``cwd`` (_read_from): Playwright's driver hands it to the browser, which opens its
    sockets there. Judged one way only: a directory given that it is or lies in can swap them; one given inside it is
    not judged, as the CLI's own may lie there (the controller's own fallback CLI home,
    $XDG_RUNTIME_DIR/auto-browser-cli, or a systemd user unit's %t)."""
    value = os.environ.get("XDG_RUNTIME_DIR", "")
    path = _read_from(cwd, value)
    return [path] if "/" in value and not _is_root(path) else []


def _browsers(cwd: str | None) -> str | None:
    """Where Playwright's driver finds the browsers (the Chromium shadow browse runs on a host), read from the working
    directory ``cwd`` where it is relative (_read_from): PLAYWRIGHT_BROWSERS_PATH, normalised as path.join and
    path.resolve join it, where it is set and not "0" (the package's own: None), or $HOME/.cache/ms-playwright where
    it is unset or empty (XDG_CACHE_HOME does not reach the driver:
    process_hardening.confine_playwright_driver_environment), with HOME as node's os.homedir() takes it: as it is where
    it is set, "" included, and the user's home only where it is not (with HOME "" Python's expanduser gives '/'
    and node's "", a relative place). The driver starts once, with the controller, and resolves a relative one then,
    under the directory it was started from."""
    home = os.environ.get("HOME")
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(
        os.path.expanduser("~") if home is None else home, ".cache/ms-playwright"
    )
    if browsers == "0":
        return None
    browsers = _read_from(cwd, browsers)
    return os.path.normpath(browsers) if os.path.isabs(browsers) else browsers


def _verifier_words(command: str) -> list[str]:
    """HARNESS_UV_COMMAND's words, as the harness splits it (startup/extensions.py); none where it cannot be split:
    the harness's start then fails, and no verifier runs."""
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _is_root(path: str) -> bool:
    """Whether ``path`` is the root itself as Linux reads it (/, //, /., /..), which holds every directory given: a
    word of the verifier's command that names it (cd /, a division's '/') is no place."""
    return path.startswith("/") and all(part in ("", ".", "..") for part in path.split("/"))


def _shell_words(word: str) -> list[str]:
    """The words a shell reads in ``word`` where it runs it as a command string (sh -c, bash -lc, env -S, su -c), those
    of both readings in order: as shlex.split reads it, which keeps a name that holds an operator, as
    GNU env -S does (/opt/uv/v;safe.py), or a vertical tab or form feed, as sh does; then with its punctuation (;, &,
    |, <, > and the parentheses) words of their own, so that an operator stuck to a path leaves the path
    (2>/tmp/uv.log, true;/srv/tools/v), and every ASCII whitespace a separator (_SHELL_WHITESPACE). Neither reads a
    comment (a shell starts none inside a word). Where shlex cannot split it (an unmatched quote), its plain whitespace
    split."""
    lexer = shlex.shlex(word, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.whitespace = _SHELL_WHITESPACE
    lexer.commenters = ""
    try:
        return list(dict.fromkeys(shlex.split(word) + list(lexer)))
    except ValueError:
        return word.split()


def _command_words(words: list[str]) -> list[str]:
    """``words``, and each word a shell reads in one of them (_shell_words), read again until none is new (a string
    nested twice, sh -c "sh -c '...'"), in the order found. The words seen bound it: a word read in another is
    the same or shorter."""
    seen = dict.fromkeys(words)
    pending = list(words)
    while pending:
        for word in _shell_words(pending.pop()):
            if word not in seen:
                seen[word] = None
                pending.append(word)
    return list(seen)


def _list_parts(path: str) -> list[str]:
    """The parts of ``path`` read as a ':' list of paths, Linux's separator whatever this host's (env
    PATH=/srv/x:/usr/bin, -cp /a:/srv/x); none where it holds no ':'. A part that starts with '//' but not
    '///' after one that ends in a URL's scheme (_URL_SCHEME_END) is the URL's host, and is left out
    (http://dev:8080/api gave //dev, that is /dev); a URL's path after three slashes is a part (sqlite:////tmp/v.db,
    file:///srv/x), and so is a path beside a URL in a list (http://x/:/srv/x, /opt/lib://srv/x). Any
    other ':' is read as a list's, which can over-judge (host:/path)."""
    if ":" not in path:
        return []
    parts = path.split(":")
    hosts = {
        index
        for index, part in enumerate(parts[1:], 1)
        if part.startswith("//") and not part.startswith("///") and _URL_SCHEME_END.search(parts[index - 1])
    }
    return [part for index, part in enumerate(parts) if index not in hosts]


def _modules_named(word: str) -> list[str]:
    """The modules a later word of the verifier's command may name, each judged by the files Python runs for it
    (_module_files): the word itself (the word after -m, which Python takes as it is, -m -mmod's -mmod among them;
    uv's module, its first word after its options or '--', whose -m and --module are flags), a '/' in it
    or not (Python imports pkg before it fails on pkg.bad/name); and, of a word led by one '-', whatever flags
    lead it, the rest after its first letter (_after_first_letter: pytest's -pmod names mod) and the rest after its
    first 'm' (-mmod and -uBmmod name mod; -m, -Rm, -tm and -nm none, the word after them naming it). No word is
    read as an option of a program: judging a name a word does not name refuses only where a link there leads into a
    directory given."""
    if not word.startswith("-") or word.startswith("--"):
        return [word] if word else []
    return [name for name in (word, _after_first_letter(word), word[1:].partition("m")[2]) if name]


def _module_files(name: str) -> list[str]:
    """The files Python runs for the module ``name`` (none for no name: Python refuses -m without one). Under -m Python
    finds the module from its working directory, the first entry of its import path, so each is relative to it. For
    a.b.c, Python's FileFinder looks, for each name on the way and the module itself (a, a/b, a/b/c), for a package's
    __init__ and then for the name as a file, each in every suffix it imports (importlib.machinery.all_suffixes():
    source, a sourceless .pyc, an extension module), and runs a package module's __main__ (a/b/c/__main__ in each
    suffix). A module file on the way (a.py) runs before Python says a is no package. Judged by its name alone
    (a.b.c), a link a CLI left at a/b/c.py, or at a package's __init__.py, ran what the CLI chose outside the sandbox;
    so did a .pyc, an extension module or a.py, judged by the .py files alone. A name that holds a '/' names no module
    Python can find: it imports the names on the way before the first part that holds one, and fails there, so only
    their files run (pkg's for pkg.bad/name; none for ./tmp/verify.py, whose first part is empty, which Python
    refuses as a relative name)."""
    if not name:
        return []
    parts = name.split(".")
    held = "/" in name
    if held:
        parts = list(itertools.takewhile(lambda part: part and "/" not in part, parts))
    names = [os.path.join(*parts[:end]) for end in range(1, len(parts) + 1)]
    stems = [stem for path in names for stem in (os.path.join(path, "__init__"), path)]
    if not held:
        stems.append(os.path.join(names[-1], "__main__"))
    return [stem + suffix for stem in stems for suffix in importlib.machinery.all_suffixes()]


def _working_directory() -> str | None:
    """The controller's working directory, or None where it was removed while the controller runs (getcwd's ENOENT):
    then nothing can be made where a relative name without a '..' part leads, and a '..' still leads to what was its
    parent (measured, as root and as the controller's user)."""
    try:
        return os.getcwd()
    except FileNotFoundError:
        return None


def _read_from(cwd: str | None, entry: str) -> str:
    """A place the controller reads by a name it was given (an entry of the import path or of PATH, an env_file, a word
    of the verifier's command, PLAYWRIGHT_BROWSERS_PATH, the bytecode prefix, Python's user base, a variable's value
    handed to a program: _handed), where it is read: as written, and one that is relative, the empty one among them,
    from the working directory ``cwd``, or as written where that was removed (None: _controllers_home_open judges it).
    Not normalised: a '..' after a link is read after the link, which _ways resolves as Linux does, and abspath would
    drop it with the part before."""
    return entry if os.path.isabs(entry) or cwd is None else os.path.join(cwd, entry)


def _controllers_home_open(
    held: list[tuple[tuple[str, str], set[Path]]], cli_home: str, verifier: str, passed: list[str]
) -> str:
    """Why the CLI may write the controller's own home or a place in it, or a place the controller reads at its start
    or runs a program from at a call, for the log; "" where it may not.

    The home is judged as HOME names it and as the passwd entry does: ssh reads the entry's, and the controller's next
    start may have another HOME. A directory given that is, holds or lies in either is reported, but for the CLI home
    lying in it away from the places the controller reads at its start: a host run whose CLI_HOME cannot be made falls
    back to a private directory in the user's home (_private_cli_home), and a CLI_HOME set there is the CLI's own. Those
    places are each home's .ssh and .local, Python's user base, which PYTHONUSERBASE can put anywhere, the .env Settings
    reads from the working directory (_env_files), the controller's own code and the programs it runs (_code, with the
    ``verifier`` command), and the places the variables it hands them name (_handed, with the verifier's ``passed``
    names; and the runtime directory, _runtime_directory, judged one way only, where it is or lies in a directory
    given): a directory given that is, holds or lies in one of them is reported wherever it is. Where the working
    directory was removed (_working_directory), one of those read from it that has a '..' part is reported, after the
    homes, and one without is not judged, but for where Playwright's driver finds the browsers (_browsers): it resolved
    a relative one under that directory at its start, and a CLI that may write the directory's parent can make it again,
    so it is reported, '..' or not.
    """
    homes = [_ways(home) for home in dict.fromkeys((os.path.expanduser("~"), _passwd_home())) if os.path.isabs(home)]
    read_at_start = [_ways(os.path.join(home[0], name)) for home in homes for name in (".ssh", ".local")]
    cwd = _working_directory()
    # The user base as site reads its user site: an absolute one normalised as site.makepath does; one still relative
    # here (the working directory was removed) kept as written, so a '..' in it is judged.
    user_base = _read_from(cwd, site.getuserbase())
    read = [os.path.normpath(user_base) if os.path.isabs(user_base) else user_base]
    read += [_read_from(cwd, name) for name in _env_files()] + _code(verifier, cwd) + _handed(cwd, passed)
    read_at_start += [_ways(path) for path in read if os.path.isabs(path)]
    own = _ways(cli_home)
    found = (
        "provider CLIs may write %s under the Landlock sandbox, %s, the controller, which is not under the sandbox, "
        "may run or use at its next start, CLI call, OCR, tunnel or verification, where it is a Python user site, an "
        "ssh config, its own code, a program it runs, or what the verifier's command names, a script it runs or a "
        "file it reads or writes. Keep the controller's home, its code, the programs it runs and "
        "what it reads at its start out of what CLIs may write: CLI_HOME, CLI_SANDBOX_WRITABLE, the CLI's TMPDIR, "
        "TEMP and TMP, its HOME where CLI_HOME is blank, and the shared temp directories"
    )
    homes_at = [(home, _places(home)) for home in homes]
    for (entry, entry_at), (home, home_at) in ((one, other) for one in held for other in homes_at):
        if _in(home_at, entry):
            return found % (
                _named(entry),
                "and the controller's own home %s lies there: what a CLI leaves in it" % _named(home),
            )
        if _in(entry_at, home) and entry != own:
            return found % (
                _named(entry),
                "which lies in the controller's own home %s: what a CLI leaves there" % _named(home),
            )
    browsers = _browsers(cwd)
    runtime = _runtime_directory(cwd)
    if unplaced := [
        path for path in (*read, *runtime) if not os.path.isabs(path) and (_climbs(path) or path == browsers)
    ]:
        # Each reason only where something it explains is named.
        reasons = ["where a '..' part leads from there cannot be said"] if any(map(_climbs, unplaced)) else []
        if browsers in unplaced:
            reasons.append(
                "Playwright's driver, started there, reads a relative PLAYWRIGHT_BROWSERS_PATH (or its default place, "
                "where HOME is relative) where the removed directory was, which a CLI that may write its parent can "
                "make again"
            )
        return (
            "the controller reads %s, at its start or at a call, from its working directory, which was removed: %s; so "
            "it cannot be said whether provider CLIs under the Landlock sandbox may write there. Start the controller "
            "from a directory that stays, or name these by an absolute path without a '..'"
        ) % (", ".join(map(quoted_whole_for_log, unplaced)), ", and ".join(reasons))
    runtime_at = [_ways(path) for path in runtime if os.path.isabs(path)]
    # Each place judged both ways, but the runtime directory only where it is, or lies in, a directory given.
    places_at = [(place, _places(place), True) for place in read_at_start]
    places_at += [(place, _places(place), False) for place in runtime_at]
    for (entry, entry_at), (place, place_at, both) in ((one, other) for one in held for other in places_at):
        if _in(place_at, entry) or (both and _in(entry_at, place)):
            return found % (
                _named(entry),
                "and the controller reads %s, at its start or at a call: what a CLI leaves there" % _named(place),
            )
    return ""


def _open_settings(settings: Settings, held: list[tuple[tuple[str, str], set[Path]]]) -> list[str]:
    """The names of the settings that are set and whose secret the ruleset leaves open (_left_open; a socket's is not
    judged for lying outside PROTECTED_ROOT), then those of the stores no setting names (_derived_store_places) that
    it leaves open as it does a secret's place."""
    named = [
        name
        for attribute, name in (SECRET_PATH_SETTINGS | STORE_PATH_SETTINGS | SOCKET_PATH_SETTINGS).items()
        if (value := getattr(settings, attribute))
        and _left_open(value, held, inside=attribute not in SOCKET_PATH_SETTINGS)
    ]
    return named + [name for name, place in _derived_store_places(settings).items() if _left_open(place, held)]


def _derived_store_places(settings: Settings) -> dict[str, str]:
    """Each place of DERIVED_STORE_PLACES, by its name in a report: beside where its setting's place resolves, as the
    code that keeps the store finds it."""
    return {
        f"{name} beside {STORE_PATH_SETTINGS[attribute]}": str(
            Path(os.path.realpath(getattr(settings, attribute))).parent / name
        )
        for name, attribute in DERIVED_STORE_PLACES.items()
    }


def _left_open(path: str, held: list[tuple[tuple[str, str], set[Path]]], *, inside: bool = True) -> bool:
    """Whether the ruleset leaves ``path`` open with the directories ``held`` given to write: it has a '..' part
    (_climbs: where it leads is the CLI's to choose where it can make the link before it), or with ``inside`` resolves
    to a place outside PROTECTED_ROOT as it is written (a root that is a link holds nothing itself), or, as written or
    once resolved, lies in one of them as written or once resolved, or holds one (a secret that is a directory). A
    path written there is the CLI's to replace, wherever it resolves now."""
    return _climbs(path) or (inside and not _within(path, PROTECTED_ROOT)) or _open_to(_ways(path), held)


def _open_to(place: tuple[str, str], held: list[tuple[tuple[str, str], set[Path]]]) -> bool:
    """Whether ``place`` lies in, or holds, one of the directories ``held``, each with its _places."""
    place_at = _places(place)
    return any(_in(place_at, entry) or _in(entry_at, place) for entry, entry_at in held)


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
        found = found or _controllers_files_open(settings, given, cli_home)
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


def _controllers_files_open(settings: Settings, given: list[tuple[str, str]], cli_home: str) -> str:
    """Why the ruleset, with the directories ``given`` to write, leaves the CLI one of the controller's files, for the
    log; "" where it leaves none. In this order, the first that holds is named: its home, its code and what it reads at
    its start (_controllers_home_open, with HARNESS_UV_COMMAND and HARNESS_UV_ENV_PASSTHROUGH's names where
    HARNESS_VERIFIER runs the command: runs_uv_command); a setting's secret or store, or a store no setting names
    (_open_settings); its own temp directory."""
    held = [(entry, _places(entry)) for entry in given]  # each given directory's _places, once for the call
    runs = runs_uv_command(settings)
    verifier, passed = (settings.harness_uv_command, settings.harness_uv_env_passthrough_names) if runs else ("", [])
    if found := _controllers_home_open(held, cli_home, verifier, passed):
        return found
    if names := _open_settings(settings, held):
        return (
            "the Landlock sandbox of provider CLIs does not close what these settings name, which lies outside %s, in "
            "a directory the CLIs may write, or holds one, or is written with a '..' part (read after any link before "
            "it, which a CLI may have made): %s"
        ) % (PROTECTED_ROOT, ", ".join(names))
    if _left_open(temp := tempfile.gettempdir(), held):
        return (
            "the controller keeps its own temp files (an OCR's screenshot, a local browser's profile and downloads) in "
            "%s, which the Landlock sandbox of provider CLIs does not close: it lies outside %s, in a directory the "
            "CLIs may write, or holds one"
        ) % (quoted_whole_for_log(temp), PROTECTED_ROOT)
    return ""


def launcher_refused(returncode: int | None, stderr: str) -> bool:
    """Whether a child started under the launcher ended as the launcher does when it could not apply the ruleset:
    the CLI was not run."""
    return returncode == EXIT_NOT_APPLIED and stderr.startswith(MARKER)

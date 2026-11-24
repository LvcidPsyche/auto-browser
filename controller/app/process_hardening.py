"""Make the controller process non-dumpable, and keep the controller's secrets out of Playwright's driver and Tesseract.

The controller starts children that run as its own uid: the provider CLIs, the Universal Verifier command, autossh,
the regex worker, Tesseract, and Playwright's node driver (and any browser the driver launches). proc(5): reading
/proc/PID/environ (or mem, maps, fd) takes a PTRACE_MODE_READ check, which a same-uid caller passes while the target
is dumpable. A non-dumpable process passes it only for a caller holding CAP_SYS_PTRACE, so a same-uid child that runs
unsandboxed can no longer read the controller's whole environment around the allowlisted one it was given. The flag
resets at execve, so the children themselves stay ordinary and dumpable: each gets an allowlisted environment
instead of the controller's, the driver through confine_playwright_driver_environment() and Tesseract through
confine_tesseract_environment(). make_non_dumpable acts on Linux only.

The temp files of the children of its own that run outside the CLI sandbox (Tesseract, Playwright's driver and its
browsers, the Universal Verifier's command and autossh), and the controller's own, SQLite's among them, go to a
directory of the controller's in the CLI sandbox's protected root (use_private_temp_directory, child_environment), not
to the /tmp every sandboxed provider CLI may read and write; a provider CLI keeps the operator's. SQLite's are moved by
its process-wide temp directory (PRAGMA temp_store_directory), as SQLite reads SQLITE_TMPDIR and TMPDIR only once, when
the sqlite3 module is imported; os.environ is not changed.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import importlib
import importlib.metadata
import logging
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Mapping

from .utils import error_text_for_log, quoted_for_log, quoted_whole_for_log

PR_SET_DUMPABLE = 4
# What the Playwright driver keeps of its environment besides the provider CLIs' base names (CLI_BASE_ENV_NAMES and
# LC_*): Playwright's own variables (PW_*, PLAYWRIGHT_BROWSERS_PATH and the rest of PLAYWRIGHT_*), its debug logging,
# and what shadow browse's headed browser needs to reach a host's display and session bus.
PLAYWRIGHT_DRIVER_ENV_NAMES = frozenset(
    {"DEBUG", "DEBUG_FILE", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
)
PLAYWRIGHT_DRIVER_ENV_PREFIXES = ("PW_", "PLAYWRIGHT_")
# The private Playwright module whose get_driver_env PipeTransport.connect calls (confine_playwright_driver_environment).
PLAYWRIGHT_TRANSPORT_MODULE = "playwright._impl.transport"
# What Tesseract keeps besides the base names: its data path and OpenMP's threads (PATH, a base name, is what
# subprocess finds the tesseract binary by).
TESSERACT_ENV_NAMES = frozenset({"TESSDATA_PREFIX"})
TESSERACT_ENV_PREFIXES = ("OMP_",)
_CONFINED = "_auto_browser_confined"
# The controller's own temp directory, in the CLI sandbox's protected root (use_private_temp_directory), and the
# variables a process reads its temp directory from (the CLI sandbox reads a CLI's by them too).
PRIVATE_TEMP_NAME = "controller-tmp"
TEMP_VARIABLES = ("TMPDIR", "TEMP", "TMP")
_private_temp: str | None = None
# The name of shutil.rmtree's error hook: onexc from Python 3.12, which deprecates onerror.
_RMTREE_ON_ERROR = "onexc" if sys.version_info >= (3, 12) else "onerror"

logger = logging.getLogger(__name__)


def make_non_dumpable() -> bool:
    """prctl(PR_SET_DUMPABLE, 0) on Linux: True when it took, False (after one warning on Linux) when it did not."""
    if not sys.platform.startswith("linux"):
        logger.debug("process hardening: PR_SET_DUMPABLE is Linux-only; skipped on %s", sys.platform)
        return False
    try:
        # The libc already loaded in this process, as drop_caps does.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(*[ctypes.c_ulong(value) for value in (PR_SET_DUMPABLE, 0, 0, 0, 0)]) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    except (OSError, AttributeError) as exc:  # AttributeError: no prctl in this libc
        logger.warning(
            "process hardening: could not make the controller non-dumpable (%s); a process running as its uid "
            "can read its environment through /proc",
            error_text_for_log(exc),
        )
        return False
    return True


def use_private_temp_directory() -> str | None:
    """Keep the controller's own temp files in a directory of its own, PRIVATE_TEMP_NAME in the CLI sandbox's
    protected root, made 0700 and empty, where no provider CLI under the sandbox reaches them: tempfile's directory
    (pytesseract writes each OCR's screenshot there while Tesseract reads it), SQLite's (_give_to_sqlite: a store's
    sort or temp table larger than its cache goes to a file there), and the temp directory of Playwright's
    driver (a browser it launches puts its profile and downloads there), of Tesseract, and of the other children of
    the controller's own (child_environment: the Universal Verifier's command, autossh). os.environ is not changed: a
    provider CLI keeps the operator's TMPDIR. Linux only, as the sandbox is. The directory, or None.

    It is the same directory at every start, and what an earlier start left in it (after a crash) is removed first.
    A second call in the same process keeps it as it is: what this process put there may be in use.

    Where the root is not there or cannot be written, or a link, another user's directory or one with a mode other
    than 0700 is in its place, or what an earlier start left there cannot be removed, the system temp directory stays,
    and one WARNING says why (_emptied names the path that failed). Nothing is refused here: each CLI call judges
    tempfile's directory (cli_sandbox.launch_command)."""
    global _private_temp
    if not sys.platform.startswith("linux"):
        logger.debug(
            "the controller's own temp directory is Linux's, as the CLI sandbox is; skipped on %s", sys.platform
        )
        return None
    from . import cli_sandbox

    path = os.path.join(cli_sandbox.PROTECTED_ROOT, PRIVATE_TEMP_NAME)
    if _private_temp == path:
        return path
    try:
        why = _make_private_temp(cli_sandbox.PROTECTED_ROOT, path)
    except OSError as exc:
        why = error_text_for_log(exc)
    if why:
        logger.warning(
            "the controller's own temp files (an OCR's screenshot, a local browser's profile and downloads) stay in "
            "the system temp directory %s, which a provider CLI may read and write: %s",
            quoted_for_log(tempfile.gettempdir()),
            why,
        )
        return None
    tempfile.tempdir = _private_temp = path
    _give_to_sqlite(path)
    return path


def _give_to_sqlite(path: str) -> None:
    """Make ``path`` SQLite's temp directory for every connection in this process, through a connection of its own;
    one WARNING where SQLite does not take it (an error, or a build without the pragma)."""
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as connection:
            # A pragma takes no bound parameter, so the path is written in as a quoted SQL string.
            connection.execute("PRAGMA temp_store_directory = '%s'" % path.replace("'", "''"))
            row = connection.execute("PRAGMA temp_store_directory").fetchone()
    except sqlite3.Error as exc:
        why = error_text_for_log(exc)
    else:
        why = "" if row == (path,) else "this SQLite has no PRAGMA temp_store_directory"
    if why:
        logger.warning(
            "SQLite's temp files (a sort or a temp table larger than a store's cache) stay in the directory SQLite "
            "chooses, which a provider CLI may read and write, and not in %s: %s",
            quoted_for_log(path),
            why,
        )


def _make_private_temp(root: str, path: str) -> str:
    """Make ``path`` in ``root`` the controller's temp directory, empty; why it is not one ("") where it is."""
    if not os.path.isdir(root):
        return "%s is not a directory" % quoted_for_log(root)
    if not os.path.lexists(path):
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)  # the umask may have taken bits off the mkdir's mode
    return _not_private(path) or _emptied(path)


def _emptied(path: str) -> str:
    """Remove what an earlier start left in ``path``; why it could not ("" where it could), with the whole path that
    failed, quoted whole, where rmtree's own error names another: on Python 3.11 only the path's last part, on
    3.12 the directory it was in (measured). The directory itself stays, so that what hides it from another
    process (the host bridge unit's InaccessiblePaths=) still holds."""
    failed = path  # the directory, then each entry in turn, then the first path rmtree's hook names under it

    def note(function: object, name: str, error: object) -> None:
        nonlocal failed
        # The first only: Python 3.12's rmtree calls the hook once more, for the directory the file was in (measured).
        if failed == entry.path:
            failed = name
        raise  # rmtree calls this while it handles the error, which goes on up

    try:
        with os.scandir(path) as entries:
            for entry in entries:
                failed = entry.path
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, **{_RMTREE_ON_ERROR: note})
                else:
                    os.unlink(entry.path)
    except OSError as exc:
        return "what an earlier start left in %s could not be removed: %s, %s" % (
            quoted_whole_for_log(path),
            quoted_whole_for_log(failed),
            error_text_for_log(exc),
        )
    return ""


def _not_private(path: str) -> str:
    """Why what is at ``path`` is no directory of this user's that no one else can open; "" where it is one."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return "%s is a link" % quoted_for_log(path)
    if not stat.S_ISDIR(info.st_mode):
        return "%s is not a directory" % quoted_for_log(path)
    if info.st_uid != os.geteuid():
        return "%s is owned by uid %d, not this user's" % (quoted_for_log(path), info.st_uid)
    if stat.S_IMODE(info.st_mode) != 0o700:
        return "%s has mode %04o, not 0700" % (quoted_for_log(path), stat.S_IMODE(info.st_mode))
    return ""


def child_environment(names: tuple[str, ...] | frozenset[str] = (), prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """The environment of a child of the controller's own, one that runs outside the CLI sandbox (Tesseract, the
    Universal Verifier's command, autossh): the allowlisted one (providers.base.allowlisted_environment, with ``names``
    and ``prefixes``) with the controller's own temp directory as its TMPDIR (_with_private_temp). A provider CLI's
    (providers.base.cli_environment) keeps the operator's."""
    from .providers.base import allowlisted_environment

    return _with_private_temp(allowlisted_environment(names, prefixes))


def _with_private_temp(env: dict[str, str]) -> dict[str, str]:
    """``env`` with TMPDIR, and TEMP and TMP where it has them, the controller's own temp directory, where there is
    one."""
    if _private_temp is None:
        return env
    names = [name for name in TEMP_VARIABLES if name == "TMPDIR" or name in env]
    return {**env, **dict.fromkeys(names, _private_temp)}


def confine_playwright_driver_environment() -> None:
    """Give Playwright's node driver, and the browsers it launches, an allowlisted environment. Idempotent.

    Playwright has no way to set its driver's environment: PipeTransport.connect starts the driver with
    env=get_driver_env(), which copies the whole of os.environ, and looks that name up in
    PLAYWRIGHT_TRANSPORT_MODULE each time it runs. So this replaces that private name with a wrapper that keeps
    only the base names and PLAYWRIGHT_DRIVER_ENV_NAMES / _PREFIXES of what the original returns, its temp directory
    the controller's own where there is one (use_private_temp_directory). If an upgrade moves
    the name or its module, this raises rather than let the driver start with the controller's secrets (the pins are
    exact, and CI runs the lifespan, so that goes red there first).
    """
    from .providers.base import CLI_BASE_ENV_NAMES, CLI_BASE_ENV_PREFIXES

    try:
        transport = importlib.import_module(PLAYWRIGHT_TRANSPORT_MODULE)
    except ModuleNotFoundError:  # the module moved: the same error as for a moved name, below
        transport = None
    original = getattr(transport, "get_driver_env", None)
    if not callable(original):
        try:
            version = importlib.metadata.version("playwright")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        raise RuntimeError(
            f"Playwright {version} has no {PLAYWRIGHT_TRANSPORT_MODULE}.get_driver_env to confine; its driver would "
            "start with the controller's whole environment"
        )
    if getattr(original, _CONFINED, False):
        return
    kept_names = CLI_BASE_ENV_NAMES | PLAYWRIGHT_DRIVER_ENV_NAMES
    kept_prefixes = (*CLI_BASE_ENV_PREFIXES, *PLAYWRIGHT_DRIVER_ENV_PREFIXES)

    @functools.wraps(original)
    def confined_driver_env() -> dict:
        kept = {
            name: value
            for name, value in original().items()
            if name.upper() in kept_names or name.upper().startswith(kept_prefixes)
        }
        return _with_private_temp(kept)

    setattr(confined_driver_env, _CONFINED, True)
    transport.get_driver_env = confined_driver_env


def confine_tesseract_environment() -> None:
    """Give the Tesseract launches that read pytesseract's environ global an allowlisted environment. Harmless to
    call again.

    pytesseract has no way to set Tesseract's environment: it does `from os import environ` and passes that module
    global as env= in two launches, subprocess_args (every OCR call: image_to_data and the rest) and
    get_tesseract_version, reading it each time. So this replaces the global with the base names,
    TESSERACT_ENV_NAMES and TESSERACT_ENV_PREFIXES of this process's environment, its temp directory the controller's
    own where there is one (use_private_temp_directory, called first). get_languages passes no env=, so
    its Tesseract inherits the controller's real environment: the controller must not call it (a test fails if a
    module under app/ names it). If an upgrade drops the global, this raises rather than let Tesseract start with
    the controller's secrets (the pin is exact, and CI runs the lifespan, so that goes red there first).
    """
    module = importlib.import_module("pytesseract.pytesseract")
    if not isinstance(getattr(module, "environ", None), Mapping):
        try:
            version = importlib.metadata.version("pytesseract")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        raise RuntimeError(
            f"pytesseract {version} has no module-level environ to confine; Tesseract would start with the "
            "controller's whole environment"
        )
    module.environ = child_environment(TESSERACT_ENV_NAMES, TESSERACT_ENV_PREFIXES)

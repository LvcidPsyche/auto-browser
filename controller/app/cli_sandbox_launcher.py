"""Start a provider CLI where it cannot open the controller's files: a Landlock ruleset, then an exec.

    python -I -S cli_sandbox_launcher.py --protect ROOT [--rw DIR]... [--ro-file FILE]... [--probe] -- CLI [ARG]...

The controller starts this file as a script (app.cli_sandbox builds the command), with the environment, the working
directory and the descriptors the CLI is to have. It restricts itself and then execs the CLI, so the CLI is this
process: the same PID and the same process group. Everything after the first ``--`` is the CLI's, whatever it looks
like. Standard library only, and nothing of the app: it runs under -I -S.

Landlock only allows, so the ruleset names what stays open:

- every entry beside ROOT, and beside each directory that holds ROOT, from / down, with every right (on a file,
  every right a file's rule can carry);
- each --rw directory with every right (the CLI home, which lies in ROOT);
- each --ro-file to read, and no more (the screenshot of this call).

No rule goes on ROOT or on a directory that holds it, so what is in ROOT and has no rule of its own is closed, to
root as to anyone, and so is listing / itself. A symbolic link gets no rule: the place it names has one, or is in
ROOT, or holds it. ROOT is read as it is written: one whose holding directory is reached through a link is refused,
and one that is itself a link to another place closes nothing there (app.cli_sandbox warns of that one).
PR_SET_NO_NEW_PRIVS is set first, as the kernel requires of a process without CAP_SYS_ADMIN.

It execs only with the ruleset on. The exit statuses that are its own, each with one line on stderr that begins with
MARKER and names the call that failed:

- 125 (EXIT_NOT_APPLIED): the ruleset is not on, and the CLI was not started. Wrong arguments, a grant that cannot
  be made as asked, a kernel without Landlock, or a kernel call that failed.
- 126 (EXIT_NOT_RUN): the ruleset is on, and the CLI was found and could not be run.
- 127 (EXIT_NOT_FOUND): the ruleset is on, and there is no such CLI.

With --probe it applies the ruleset and exits 0 without an exec: how the controller learns that all of this works.
"""

from __future__ import annotations

import ctypes
import errno
import os
import posixpath
import signal
import stat
import struct
import sys

MARKER = "auto-browser cli sandbox: "
EXIT_NOT_APPLIED = 125
EXIT_NOT_RUN = 126
EXIT_NOT_FOUND = 127

# Landlock's rights over files, by the bit each has in linux/landlock.h.
EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
# Those four and the nine that make and remove entries (REMOVE_DIR to MAKE_SYM): all that the first ABI knows.
_FIRST_ABI_RIGHTS = (1 << 13) - 1
REFER = 1 << 13
TRUNCATE = 1 << 14
IOCTL_DEV = 1 << 15
# The ABI each later right came with. A ruleset that names a right the kernel does not know is refused whole.
_RIGHTS_SINCE_ABI = {2: REFER, 3: TRUNCATE, 5: IOCTL_DEV}
# What a rule on anything but a directory may carry: the kernel answers any other right there with EINVAL.
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV

# The same numbers on x86-64 and arm64.
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38


class NotApplied(Exception):
    """The ruleset is not on this process: ``call`` is what failed and ``detail`` how."""

    def __init__(self, call: str, detail: str) -> None:
        super().__init__(call, detail)
        self.call = call
        self.detail = detail


def _errno_text(number: int) -> str:
    return "%s (%s)" % (errno.errorcode.get(number, number), os.strerror(number))


def handled_rights(abi: int) -> int:
    """The rights a ruleset handles under Landlock ABI ``abi``: every one this file knows that the ABI knows too.

    An ABI above the newest in _RIGHTS_SINCE_ABI is read as that one: what it added is not handled, so not denied.
    """
    rights = _FIRST_ABI_RIGHTS
    for since, right in _RIGHTS_SINCE_ABI.items():
        if abi >= since:
            rights |= right
    return rights


def beside_the_root(root: str, listdir) -> list[str]:
    """Each entry beside ``root`` or beside a directory that holds it, from / down: what the CLI keeps.

    Neither ``root`` nor a directory that holds it is among them. A level that is not there ends the walk: ``root``
    is not there either, and stays closed if it appears, since nothing that would hold it has a rule.
    """
    beside: list[str] = []
    level = "/"
    for name in root.strip("/").split("/"):
        try:
            entries = sorted(listdir(level))
        except (FileNotFoundError, NotADirectoryError):
            break
        except OSError as error:
            raise NotApplied(f"listdir {level}", _errno_text(error.errno or 0)) from None
        beside += [posixpath.join(level, entry) for entry in entries if entry != name]
        level = posixpath.join(level, name)
    return beside


def _holds(directory: str, path: str) -> bool:
    """Whether ``path`` is ``directory`` or lies in it."""
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def _add_rule(kernel, ruleset: int, fd: int, rights: int, path: str) -> None:
    try:
        kernel.add_rule(ruleset, fd, rights)
    except NotApplied as refused:
        raise NotApplied(f"{refused.call} on {path}", refused.detail) from None


def _allow_beside(kernel, ruleset: int, path: str, handled: int) -> None:
    try:
        fd = kernel.open_path(path, follow=False)
    except OSError:
        return  # gone since the listing, or not to be opened: no rule, which allows nothing
    try:
        kind = kernel.mode(fd)
        if not stat.S_ISLNK(kind):
            _add_rule(kernel, ruleset, fd, handled if stat.S_ISDIR(kind) else handled & FILE_RIGHTS, path)
    finally:
        kernel.close(fd)


def _grant(kernel, ruleset: int, option: str, path: str, rights: int, *, directory: bool) -> None:
    """A rule the caller asked for by name. Unlike an entry beside the root, one that cannot be made is an error."""
    asked = f"{option} {path}"
    try:
        # A file is opened as itself: a link in its place is no regular file, and is refused below.
        fd = kernel.open_path(path, follow=directory)
    except OSError as error:
        raise NotApplied(asked, _errno_text(error.errno or 0)) from None
    try:
        kind = kernel.mode(fd)
        if directory and not stat.S_ISDIR(kind):
            raise NotApplied(asked, "not a directory")
        if not directory and not stat.S_ISREG(kind):
            raise NotApplied(asked, "not a regular file")
        _add_rule(kernel, ruleset, fd, rights, path)
    finally:
        kernel.close(fd)


def apply(kernel, root: str, writable: list[str], readable_files: list[str]) -> None:
    """Restrict this process, for good: see the module's text for what stays open. NotApplied if any step fails."""
    handled = handled_rights(kernel.abi())
    # The walk reads root as it is written. Where a directory that holds it is reached through a link, an entry
    # beside that link may be what really holds the root, and its rule would open it.
    holder = posixpath.dirname(root)
    if kernel.realpath(holder) != holder:
        raise NotApplied(f"--protect {root}", "a directory that holds it is reached through a link")
    ruleset = kernel.create_ruleset(handled)
    try:
        for path in beside_the_root(root, kernel.listdir):
            _allow_beside(kernel, ruleset, path, handled)
        real_root = kernel.realpath(root)
        for path in writable:
            if _holds(kernel.realpath(path), real_root):
                raise NotApplied(f"--rw {path}", "it is the protected root, or holds it")
            _grant(kernel, ruleset, "--rw", path, handled, directory=True)
        for path in readable_files:
            _grant(kernel, ruleset, "--ro-file", path, READ_FILE, directory=False)
        kernel.no_new_privs()
        kernel.restrict_self(ruleset)
    finally:
        kernel.close(ruleset)


class _Linux:
    """The kernel, as apply() calls it. A call the kernel refuses is NotApplied, by the call's own name."""

    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.syscall.restype = ctypes.c_long

    def _syscall(self, name: str, number: int, *arguments) -> int:
        result = self._libc.syscall(ctypes.c_long(number), *arguments)
        if result < 0:
            raise NotApplied(name, _errno_text(ctypes.get_errno()))
        return result

    def abi(self) -> int:
        # ENOSYS from a kernel built without Landlock, EOPNOTSUPP from one that has it switched off.
        flags = ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION)
        return self._syscall(
            "landlock_create_ruleset(version)", _SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), flags
        )

    def create_ruleset(self, handled: int) -> int:
        # The first, 8-byte form of struct landlock_ruleset_attr (handled_access_fs alone): every ABI takes it, and
        # it handles no network right.
        attribute = ctypes.c_uint64(handled)
        size = ctypes.c_size_t(ctypes.sizeof(attribute))
        return self._syscall(
            "landlock_create_ruleset", _SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attribute), size, ctypes.c_uint(0)
        )

    def add_rule(self, ruleset: int, fd: int, rights: int) -> None:
        # struct landlock_path_beneath_attr is packed: the rights' 8 bytes, then the descriptor's 4.
        attribute = struct.pack("=Qi", rights, fd)
        rule_type = ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH)
        self._syscall(
            "landlock_add_rule", _SYS_LANDLOCK_ADD_RULE, ctypes.c_int(ruleset), rule_type, attribute, ctypes.c_uint(0)
        )

    def no_new_privs(self) -> None:
        if self._libc.prctl(_PR_SET_NO_NEW_PRIVS, *[ctypes.c_ulong(value) for value in (1, 0, 0, 0)]) != 0:
            raise NotApplied("prctl(PR_SET_NO_NEW_PRIVS)", _errno_text(ctypes.get_errno()))

    def restrict_self(self, ruleset: int) -> None:
        self._syscall("landlock_restrict_self", _SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint(0))

    listdir = staticmethod(os.listdir)
    realpath = staticmethod(os.path.realpath)
    close = staticmethod(os.close)

    @staticmethod
    def open_path(path: str, *, follow: bool) -> int:
        return os.open(path, os.O_PATH | os.O_CLOEXEC | (0 if follow else os.O_NOFOLLOW))

    @staticmethod
    def mode(fd: int) -> int:
        return os.fstat(fd).st_mode


def started_with() -> dict[bytes, bytes]:
    """The environment this process was started with, which is the CLI's.

    Not os.environ: started in the C locale, Python has by now set LC_CTYPE in that one (PEP 538).
    """
    try:
        with open("/proc/self/environ", "rb") as source:
            entries = source.read().split(b"\0")
    except OSError as error:
        raise NotApplied("read /proc/self/environ", _errno_text(error.errno or 0)) from None
    return dict(entry.split(b"=", 1) for entry in entries if b"=" in entry)


def _arguments(arguments: list[str]) -> tuple[str, list[str], list[str], bool, list[str]]:
    """(root, the --rw directories, the --ro-file files, whether --probe, the CLI's command) of the launcher's own
    arguments, or NotApplied("arguments", ...). The launcher's own end at the first ``--``."""
    end = arguments.index("--") if "--" in arguments else len(arguments)
    values: dict[str, list[str]] = {"--protect": [], "--rw": [], "--ro-file": []}
    probe = False
    own = iter(arguments[:end])
    for option in own:
        if option == "--probe":
            probe = True
        elif option in values:
            value = next(own, None)
            if value is None:
                raise NotApplied("arguments", f"{option} takes a path")
            values[option].append(value)
        else:
            raise NotApplied("arguments", f"{option} is no option of the launcher")
    roots, command = values["--protect"], arguments[end + 1 :]
    root = roots[0] if len(roots) == 1 else ""
    # Written any other way (/a/../b, /a/, //a), the walk would read the root as another place than it names.
    if not root.startswith("/") or root.startswith("//") or root == "/" or posixpath.normpath(root) != root:
        raise NotApplied("arguments", "--protect takes one path, once: absolute, normalised, and not /")
    if not all(path.startswith("/") for path in values["--rw"] + values["--ro-file"]):
        raise NotApplied("arguments", "--rw and --ro-file take absolute paths")
    if not command and not probe:
        raise NotApplied("arguments", "no command follows --")
    return root, values["--rw"], values["--ro-file"], probe, command


def _stop(status: int, call: str, detail: str) -> int:
    sys.stderr.write(f"{MARKER}{call}: {detail}\n")
    return status


def main(argv: list[str]) -> int:
    try:
        root, writable, readable_files, probe, command = _arguments(argv[1:])
        environment = started_with()
        apply(_Linux(), root, writable, readable_files)
    except NotApplied as refused:
        return _stop(EXIT_NOT_APPLIED, refused.call, refused.detail)
    except Exception as unexpected:  # whatever it is, the ruleset is not known to be on: no exec
        number = getattr(unexpected, "errno", None)
        detail = _errno_text(number) if isinstance(number, int) else type(unexpected).__name__
        return _stop(EXIT_NOT_APPLIED, "launcher", detail)
    if probe:
        return 0
    # Python ignores SIGPIPE and SIGXFSZ from its start, and an ignored signal stays ignored through an exec: the CLI
    # gets them as a child started directly does (subprocess's restore_signals).
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    try:
        # The lookup subprocess makes for a command started directly: PATH of the environment the child gets.
        os.execvpe(command[0], command, environment)  # noqa: S606 - the CLI the controller named, as it names it
    except OSError as error:
        missing = error.errno in (errno.ENOENT, errno.ENOTDIR)
        return _stop(EXIT_NOT_FOUND if missing else EXIT_NOT_RUN, "execvpe", _errno_text(error.errno or 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv))

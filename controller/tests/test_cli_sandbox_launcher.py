"""The Landlock launcher a provider CLI starts under (N1): app/cli_sandbox_launcher.py.

What the launcher decides is tested anywhere, over a stand-in for the kernel: which paths get a rule and with which
rights, the order of the calls, and that a call that fails means no exec. What the kernel then does is tested on Linux
where Landlock works (GitHub's runners and the image there), each time in a child and never in this process, under a
protected directory in tmp_path: the ruleset takes its root as an argument.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cli_sandbox_launcher as launcher

LAUNCHER = Path(launcher.__file__)
ALL_RIGHTS = launcher.handled_rights(5)
# A directory tree as the stand-in kernel sees one: the image's root, with /data to protect.
IMAGE_ROOT = {
    "/": "dir",
    "/.dockerenv": "file",
    "/bin": "link",
    "/data": "dir",
    "/data/api-token": "file",
    "/data/artifacts": "dir",
    "/data/artifacts/link.png": "link",
    "/data/artifacts/s1.png": "file",
    "/data/cli-home": "dir",
    "/etc": "dir",
    "/proc": "dir",
    "/usr": "dir",
}


class FakeKernel:
    """The launcher's kernel calls over a tree of {path: "dir" | "file" | "link"}, recording what was asked.

    A path the tree does not hold cannot be opened; `failing` names the one call that is refused.
    """

    def __init__(self, tree: dict[str, str], *, abi: int = 5, failing: str | None = None) -> None:
        self.tree = dict(tree)
        self.abi_version = abi
        self.failing = failing
        self.real: dict[str, str] = {}
        self.calls: list[str] = []
        self.handled: int | None = None
        self.rules: dict[str, int] = {}
        self.followed: dict[str, bool] = {}
        self._open: dict[int, str] = {}
        self.closed: list[int] = []

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name == self.failing:
            raise launcher.NotApplied(name, "EPERM (Operation not permitted)")

    def abi(self) -> int:
        self._call("abi")
        return self.abi_version

    def create_ruleset(self, handled: int) -> int:
        self._call("create_ruleset")
        self.handled = handled
        self._open[3] = "<ruleset>"
        return 3

    def listdir(self, path: str) -> list[str]:
        if self.tree.get(path) != "dir":
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        prefix = path.rstrip("/") + "/"
        below = [name[len(prefix) :] for name in self.tree if name.startswith(prefix)]
        return [name for name in below if name and "/" not in name]

    def realpath(self, path: str) -> str:
        return self.real.get(path, path)

    def open_path(self, path: str, *, follow: bool) -> int:
        if path not in self.tree:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        fd = 10 + len(self._open)
        self._open[fd] = path
        self.followed[path] = follow
        return fd

    def mode(self, fd: int) -> int:
        return {"dir": stat.S_IFDIR, "file": stat.S_IFREG, "link": stat.S_IFLNK}[self.tree[self._open[fd]]]

    def path_of(self, fd: int) -> str:
        # What /proc/self/fd says of a descriptor: the real path of what it was opened for.
        return self.realpath(self._open[fd])

    def add_rule(self, ruleset: int, fd: int, rights: int) -> None:
        self._call("add_rule")
        assert ruleset == 3 and fd not in self.closed
        self.rules[self._open[fd]] = rights

    def no_new_privs(self) -> None:
        self._call("no_new_privs")

    def restrict_self(self, ruleset: int) -> None:
        self._call("restrict_self")

    def close(self, fd: int) -> None:
        self.closed.append(fd)


def _applied(tree=IMAGE_ROOT, *, root="/data", writable=("/data/cli-home",), readable_files=(), **kernel) -> FakeKernel:
    fake = FakeKernel(tree, **kernel)
    launcher.apply(fake, root, list(writable), list(readable_files))
    return fake


# ---- the rights ----


def test_the_first_abi_handles_its_thirteen_rights_and_no_later_one() -> None:
    assert launcher.handled_rights(1) == (1 << 13) - 1


@pytest.mark.parametrize(
    ("abi", "added"),
    [(2, ["REFER"]), (3, ["REFER", "TRUNCATE"]), (4, ["REFER", "TRUNCATE"]), (5, ["REFER", "TRUNCATE", "IOCTL_DEV"])],
)
def test_each_abi_handles_the_rights_it_knows_and_none_it_does_not(abi: int, added: list[str]) -> None:
    later = {"REFER": launcher.REFER, "TRUNCATE": launcher.TRUNCATE, "IOCTL_DEV": launcher.IOCTL_DEV}
    expected = (1 << 13) - 1
    for name in added:
        expected |= later[name]

    assert launcher.handled_rights(abi) == expected


@pytest.mark.parametrize("abi", [6, 8, 40])
def test_an_abi_above_the_newest_known_is_read_as_that_one(abi: int) -> None:
    # GitHub's ubuntu-24.04 runner has ABI 8 (measured 2026-11-09): a right this file does not know is never asked for.
    assert launcher.handled_rights(abi) == launcher.handled_rights(5) == (1 << 16) - 1


def test_a_file_rule_carries_only_what_the_kernel_takes_on_a_file() -> None:
    # Any other right on something that is no directory is the kernel's EINVAL (measured on 6.8 and 7.0).
    assert launcher.FILE_RIGHTS == (
        launcher.EXECUTE | launcher.WRITE_FILE | launcher.READ_FILE | launcher.TRUNCATE | launcher.IOCTL_DEV
    )
    assert not launcher.FILE_RIGHTS & launcher.READ_DIR


# ---- what gets a rule ----


def test_every_entry_beside_the_protected_root_is_listed_and_never_the_root_or_what_holds_it() -> None:
    beside = launcher.beside_the_root("/data", FakeKernel(IMAGE_ROOT).listdir)

    assert beside == ["/.dockerenv", "/bin", "/etc", "/proc", "/usr"]


def test_the_walk_lists_what_lies_beside_each_level_down_to_a_deeper_root() -> None:
    tree = {
        "/": "dir",
        "/etc": "dir",
        "/tmp": "dir",
        "/tmp/other": "dir",
        "/tmp/run": "dir",
        "/tmp/run/log.txt": "file",
        "/tmp/run/protected": "dir",
        "/tmp/run/protected/secret": "file",
    }

    beside = launcher.beside_the_root("/tmp/run/protected", FakeKernel(tree).listdir)

    assert beside == ["/etc", "/tmp/other", "/tmp/run/log.txt"]
    assert not [path for path in beside if "/tmp/run/protected".startswith(path.rstrip("/") + "/")]


def test_a_level_that_is_not_there_ends_the_walk() -> None:
    tree = {"/": "dir", "/etc": "dir", "/srv": "dir", "/srv/other": "dir"}

    assert launcher.beside_the_root("/srv/missing/data", FakeKernel(tree).listdir) == ["/etc", "/srv/other"]
    assert launcher.beside_the_root("/data", FakeKernel(tree).listdir) == ["/etc", "/srv"]


def test_a_root_whose_holder_is_reached_through_a_link_is_refused_before_any_rule() -> None:
    # /srv is a link to /mnt/srv: /mnt lies beside it, holds the real root, and would get a rule.
    tree = {"/": "dir", "/etc": "dir", "/mnt": "dir", "/mnt/srv": "dir", "/mnt/srv/data": "dir", "/srv": "link"}
    fake = FakeKernel(tree)
    fake.real["/srv"] = "/mnt/srv"

    with pytest.raises(launcher.NotApplied) as refused:
        launcher.apply(fake, "/srv/data", [], [])

    assert refused.value.call == "--protect /srv/data"
    assert fake.rules == {} and "create_ruleset" not in fake.calls


@pytest.mark.parametrize("abi", [1, 2, 3, 4, 5, 8])
def test_an_entry_beside_the_root_gets_the_rights_to_read_list_and_run_and_no_other(abi: int) -> None:
    # The controller is not under the ruleset: what a CLI could write beside the root, it could leave there for the
    # controller to run (a usercustomize.py in its Python user site, an ssh config) or to read.
    fake = _applied(abi=abi)

    read_and_run = launcher.EXECUTE | launcher.READ_FILE
    assert fake.rules["/etc"] == fake.rules["/usr"] == fake.rules["/proc"] == read_and_run | launcher.READ_DIR
    assert fake.rules["/.dockerenv"] == read_and_run
    assert fake.handled == launcher.handled_rights(abi)
    assert fake.rules["/data/cli-home"] == launcher.handled_rights(abi), "the home keeps every right"


def test_no_rule_goes_on_the_protected_root_or_on_what_holds_it() -> None:
    fake = _applied()

    assert "/data" not in fake.rules and "/" not in fake.rules
    assert sorted(fake.rules) == ["/.dockerenv", "/data/cli-home", "/etc", "/proc", "/usr"]


def test_a_symbolic_link_beside_the_root_is_opened_as_itself_and_gets_no_rule() -> None:
    # Followed, a link that names the protected root (or a directory in it) would put a rule on it.
    fake = _applied()

    assert "/bin" not in fake.rules
    assert fake.followed["/bin"] is False and fake.followed["/etc"] is False


def test_an_entry_gone_since_the_listing_is_passed_over() -> None:
    class Vanishing(FakeKernel):
        def open_path(self, path: str, *, follow: bool) -> int:
            if path == "/etc":
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
            return super().open_path(path, follow=follow)

    fake = Vanishing(IMAGE_ROOT)
    launcher.apply(fake, "/data", ["/data/cli-home"], [])

    assert "/etc" not in fake.rules and "/usr" in fake.rules
    assert fake.calls[-1] == "restrict_self"


def test_the_cli_home_gets_every_right_and_the_screenshot_only_a_read() -> None:
    fake = _applied(readable_files=["/data/artifacts/s1.png"])

    assert fake.rules["/data/cli-home"] == ALL_RIGHTS
    assert fake.rules["/data/artifacts/s1.png"] == launcher.READ_FILE
    assert "/data/artifacts" not in fake.rules, "the grant is the file, not the directory that holds it"
    # The home is opened following links, and the descriptor's own path then shows any link on the way; the file is
    # opened as itself, so a link in its place is refused.
    assert fake.followed["/data/cli-home"] is True
    assert fake.followed["/data/artifacts/s1.png"] is False


@pytest.mark.parametrize("swapped", ["/tmp/x", "/tmp/x/y"], ids=["the directory", "a directory on its way"])
def test_a_writable_directory_reached_through_a_link_is_refused_and_gets_no_rule(swapped: str) -> None:
    # The controller resolves each --rw path. A link on the way means the path changed since: a CLI of another call
    # removed /tmp/x and put a link to a directory of the root there, which a rule made by the path would open.
    tree = {**IMAGE_ROOT, "/tmp": "dir", "/tmp/x": "dir", "/tmp/x/y": "dir", "/data/auth": "dir"}
    fake = FakeKernel(tree)
    fake.real[swapped] = "/data/auth"

    with pytest.raises(launcher.NotApplied) as refused:
        launcher.apply(fake, "/data", ["/data/cli-home", swapped], [])

    assert (refused.value.call, refused.value.detail) == (f"--rw {swapped}", "reached through a link")
    assert swapped not in fake.rules and "/data/auth" not in fake.rules
    assert "restrict_self" not in fake.calls
    assert sorted(fake.closed) == sorted(fake._open)


def test_an_older_abi_puts_no_right_it_does_not_know_in_the_ruleset_or_in_a_rule() -> None:
    fake = _applied(abi=1, readable_files=["/data/artifacts/s1.png"])

    later = launcher.REFER | launcher.TRUNCATE | launcher.IOCTL_DEV
    assert fake.handled == (1 << 13) - 1
    assert not any(rights & later for rights in fake.rules.values())
    assert fake.rules["/.dockerenv"] == launcher.EXECUTE | launcher.READ_FILE


def test_the_rules_come_first_then_no_new_privs_then_the_restriction() -> None:
    fake = _applied()

    assert fake.calls[:2] == ["abi", "create_ruleset"]
    assert fake.calls[-2:] == ["no_new_privs", "restrict_self"]
    assert set(fake.calls[2:-2]) == {"add_rule"}


def test_every_descriptor_the_launcher_opened_is_closed_before_it_returns() -> None:
    fake = _applied(readable_files=["/data/artifacts/s1.png"])

    assert sorted(fake.closed) == sorted(fake._open)


@pytest.mark.parametrize("call", ["abi", "create_ruleset", "add_rule", "no_new_privs", "restrict_self"])
def test_a_refused_kernel_call_stops_the_launcher_there(call: str) -> None:
    fake = FakeKernel(IMAGE_ROOT, failing=call)

    with pytest.raises(launcher.NotApplied) as refused:
        launcher.apply(fake, "/data", ["/data/cli-home"], [])

    # A rule that the kernel refuses is named with the entry it was for: the first, as the walk sorts them.
    assert refused.value.call == ("add_rule on /.dockerenv" if call == "add_rule" else call)
    assert fake.calls[-1] == call, "nothing is called after the call that failed"


def test_a_rule_the_kernel_refuses_for_a_grant_names_the_grants_path() -> None:
    class RefusesTheHome(FakeKernel):
        def add_rule(self, ruleset: int, fd: int, rights: int) -> None:
            if self._open[fd] == "/data/cli-home":
                raise launcher.NotApplied("landlock_add_rule", "EINVAL (Invalid argument)")
            super().add_rule(ruleset, fd, rights)

    fake = RefusesTheHome(IMAGE_ROOT)

    with pytest.raises(launcher.NotApplied) as refused:
        launcher.apply(fake, "/data", ["/data/cli-home"], [])

    assert (refused.value.call, refused.value.detail) == (
        "landlock_add_rule on /data/cli-home",
        "EINVAL (Invalid argument)",
    )
    assert "restrict_self" not in fake.calls and sorted(fake.closed) == sorted(fake._open)


@pytest.mark.parametrize(
    ("writable", "readable_files", "names"),
    [
        (["/data/missing"], [], "--rw /data/missing"),
        (["/data/api-token"], [], "--rw /data/api-token"),
        (["/data"], [], "--rw /data"),
        (["/"], [], "--rw /"),
        (["/data/cli-home"], ["/data/missing.png"], "--ro-file /data/missing.png"),
        (["/data/cli-home"], ["/data/artifacts"], "--ro-file /data/artifacts"),
        (["/data/cli-home"], ["/data/artifacts/link.png"], "--ro-file /data/artifacts/link.png"),
    ],
    ids=[
        "no such home",
        "a home that is a file",
        "the root",
        "what holds the root",
        "no such file",
        "a directory for a file",
        "a link for a file",
    ],
)
def test_a_grant_that_cannot_be_made_as_asked_stops_the_launcher(
    writable: list[str], readable_files: list[str], names: str
) -> None:
    fake = FakeKernel(IMAGE_ROOT)

    with pytest.raises(launcher.NotApplied) as refused:
        launcher.apply(fake, "/data", writable, readable_files)

    assert refused.value.call == names
    assert "restrict_self" not in fake.calls
    assert not {"/", "/data", "/data/artifacts", "/data/artifacts/link.png", "/data/api-token"} & set(fake.rules)


def test_a_home_whose_real_place_holds_the_root_is_refused() -> None:
    fake = FakeKernel({**IMAGE_ROOT, "/home": "dir", "/home/link": "dir"})
    fake.real["/home/link"] = "/"

    with pytest.raises(launcher.NotApplied) as refused:
        launcher.apply(fake, "/data", ["/home/link"], [])

    assert refused.value.call == "--rw /home/link"
    assert "/home/link" not in fake.rules


# ---- main: arguments, the exec, the exit status ----


class _Exec(Exception):
    """Raised in place of the exec, carrying what it was asked to run."""


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """main() over a stand-in kernel and with an exec that records: the kernel, what the exec was passed, and the
    signals set before it (set for real, they would be this process's)."""
    fake = FakeKernel(IMAGE_ROOT)
    seen = SimpleNamespace(kernel=fake, exec=None, signals=[])

    def execvpe(file, arguments, environment):
        seen.exec = (file, list(arguments), dict(environment))
        raise _Exec

    monkeypatch.setattr(launcher, "_Linux", lambda: fake)
    monkeypatch.setattr(launcher, "started_with", lambda: {b"PATH": b"/usr/bin", b"KEY": b"value"})
    monkeypatch.setattr(launcher.os, "execvpe", execvpe)
    monkeypatch.setattr(launcher.signal, "signal", lambda number, handler: seen.signals.append((number, handler)))
    return seen


def _main(*arguments: str) -> int:
    return launcher.main([str(LAUNCHER), *arguments])


def test_main_restricts_then_execs_the_cli_with_the_environment_it_was_started_with(started) -> None:
    with pytest.raises(_Exec):
        _main("--protect", "/data", "--rw", "/data/cli-home", "--", "codex", "exec", "-")

    assert started.kernel.calls[-1] == "restrict_self"
    assert started.exec == ("codex", ["codex", "exec", "-"], {b"PATH": b"/usr/bin", b"KEY": b"value"})
    # Python ignores SIGPIPE from its start, and an exec keeps an ignored signal ignored: the CLI gets the default.
    expected = [(signal.SIGPIPE, signal.SIG_DFL)] if hasattr(signal, "SIGPIPE") else []
    assert [entry for entry in started.signals if entry[0] == getattr(signal, "SIGPIPE", None)] == expected


def test_a_cli_argument_that_looks_like_a_launcher_option_is_the_clis(started) -> None:
    cli = ["cli", "--probe", "--protect", "/", "--rw", "/data", "--ro-file", "/data/api-token", "--", "--rw"]

    with pytest.raises(_Exec):
        _main("--protect", "/data", "--rw", "/data/cli-home", "--", *cli)

    assert started.exec[1] == cli
    assert sorted(started.kernel.rules) == ["/.dockerenv", "/data/cli-home", "/etc", "/proc", "/usr"]


def test_the_probe_applies_the_ruleset_and_runs_nothing(started, capsys: pytest.CaptureFixture[str]) -> None:
    assert _main("--protect", "/data", "--probe") == 0

    assert started.kernel.calls[-1] == "restrict_self"
    assert started.exec is None
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "arguments",
    [
        ["--", "cli"],
        ["--protect", "/", "--", "cli"],
        ["--protect", "data", "--", "cli"],
        ["--protect", "/data/../etc", "--", "cli"],
        ["--protect", "/data/", "--", "cli"],
        ["--protect", "//data", "--", "cli"],
        ["--protect", "/data", "--protect", "/etc", "--", "cli"],
        ["--protect", "/data", "--rw", "cli-home", "--", "cli"],
        ["--protect", "/data", "--ro-file", "shot.png", "--", "cli"],
        ["--protect", "/data", "--unknown", "--", "cli"],
        ["--protect", "/data", "--rw"],
        ["--protect", "/data", "--"],
        ["--protect", "/data"],
    ],
    ids=[
        "no root",
        "the root is /",
        "a relative root",
        "a root with ..",
        "a root that is not normalised",
        "a root with two leading slashes",
        "two roots",
        "a relative home",
        "a relative file",
        "an unknown option",
        "an option without its value",
        "no command after --",
        "no -- at all",
    ],
)
def test_wrong_arguments_are_the_launchers_status_and_nothing_is_restricted_or_run(
    started, capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    assert _main(*arguments) == launcher.EXIT_NOT_APPLIED == 125

    assert started.kernel.calls == [] and started.exec is None
    assert capsys.readouterr().err.startswith(launcher.MARKER + "arguments: ")


@pytest.mark.parametrize("call", ["abi", "create_ruleset", "add_rule", "no_new_privs", "restrict_self"])
def test_a_refused_kernel_call_is_the_launchers_status_a_line_naming_it_and_no_exec(
    started, capsys: pytest.CaptureFixture[str], call: str
) -> None:
    started.kernel.failing = call

    assert _main("--protect", "/data", "--rw", "/data/cli-home", "--", "cli") == 125

    assert started.exec is None, "never an exec without the ruleset"
    error = capsys.readouterr().err
    named = "add_rule on /.dockerenv" if call == "add_rule" else call
    assert error == f"{launcher.MARKER}{named}: EPERM (Operation not permitted)\n"


def test_a_bad_rule_is_the_launchers_status_and_no_exec(started, capsys: pytest.CaptureFixture[str]) -> None:
    assert _main("--protect", "/data", "--rw", "/data/missing", "--", "cli") == 125

    assert started.exec is None and "restrict_self" not in started.kernel.calls
    assert capsys.readouterr().err.startswith(f"{launcher.MARKER}--rw /data/missing: ")


def test_an_environment_that_cannot_be_read_is_the_launchers_status_and_no_exec(
    started, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unreadable():
        raise launcher.NotApplied("read /proc/self/environ", "EACCES (Permission denied)")

    monkeypatch.setattr(launcher, "started_with", unreadable)

    assert _main("--protect", "/data", "--", "cli") == 125

    assert started.exec is None and started.kernel.calls == []
    assert capsys.readouterr().err == f"{launcher.MARKER}read /proc/self/environ: EACCES (Permission denied)\n"


def test_an_error_nothing_expected_is_the_launchers_status_and_no_exec(
    started, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_libc():
        raise AttributeError("no syscall in this libc")

    monkeypatch.setattr(launcher, "_Linux", no_libc)

    assert _main("--protect", "/data", "--", "cli") == 125

    assert started.exec is None
    assert capsys.readouterr().err == f"{launcher.MARKER}launcher: AttributeError\n"


def test_an_os_error_nothing_expected_is_named_by_its_errno_and_not_by_its_text(
    started, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An OSError's own text carries the path it was for: the line says which error, and no more.
    def mode(fd: int) -> int:
        raise OSError(errno.EIO, "Input/output error", "/data/cli-home")

    monkeypatch.setattr(started.kernel, "mode", mode)

    assert _main("--protect", "/data", "--rw", "/data/cli-home", "--", "cli") == 125

    assert started.exec is None and "restrict_self" not in started.kernel.calls
    assert capsys.readouterr().err == f"{launcher.MARKER}launcher: EIO ({os.strerror(errno.EIO)})\n"


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (FileNotFoundError(errno.ENOENT, "No such file or directory"), 127),
        (NotADirectoryError(errno.ENOTDIR, "Not a directory"), 127),
        (PermissionError(errno.EACCES, "Permission denied"), 126),
        (OSError(errno.ENOEXEC, "Exec format error"), 126),
    ],
    ids=["not found", "not a directory", "not permitted", "not a program"],
)
def test_a_cli_that_cannot_be_started_is_126_or_127_with_a_line_naming_the_exec(
    started, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: OSError, status: int
) -> None:
    def execvpe(file, arguments, environment):
        raise failure

    monkeypatch.setattr(launcher.os, "execvpe", execvpe)

    assert _main("--protect", "/data", "--", "no-such-cli") == status

    error = capsys.readouterr().err
    assert error.startswith(launcher.MARKER + "execvpe: ") and errno.errorcode[failure.errno] in error


def test_the_launcher_imports_only_the_standard_library() -> None:
    # It is started with -I -S as a script: nothing of the app, and no package, is there to import.
    import ast

    tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {
        (node.module or "").split(".")[0] if not node.level else "."
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert imported <= {"__future__", "ctypes", "errno", "os", "posixpath", "signal", "stat", "struct", "sys"}


# ---- the real kernel ----


def _landlock_abi() -> int:
    """The running kernel's Landlock ABI: 0 where there is none (not Linux, an older kernel, Landlock off)."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        return launcher._Linux().abi()
    except (launcher.NotApplied, OSError, AttributeError):
        return 0


LANDLOCK_ABI = _landlock_abi()
needs_landlock = pytest.mark.skipif(LANDLOCK_ABI < 1, reason="needs Linux with Landlock")
# What a child is given: nothing of this process's environment but where programs are.
CHILD_ENV = {"PATH": os.environ.get("PATH", "")}
# The CLI stand-in: tries each [operation, path...] of its first argument and prints what came of each.
STAND_IN = r"""
import errno, json, os, subprocess, sys

def attempt(operation, *paths):
    try:
        if operation == "read":
            return open(paths[0], encoding="utf-8").read()
        if operation == "list":
            os.listdir(paths[0])
        elif operation == "create":
            open(paths[0], "x").close()
        elif operation == "append":
            open(paths[0], "a").close()
        elif operation == "write":
            with open(paths[0], "w") as target:
                target.write("written")
        elif operation == "truncate":
            os.truncate(paths[0], 0)
        elif operation == "stat":
            os.stat(paths[0])
        elif operation == "mkdir":
            os.mkdir(paths[0])
        elif operation == "rename":
            os.rename(paths[0], paths[1])
        elif operation == "remove":
            os.remove(paths[0])
        elif operation == "run":
            done = subprocess.run(list(paths), stdin=subprocess.DEVNULL, capture_output=True)
            return "ok" if done.returncode == 0 else "exit %d" % done.returncode
        elif operation == "pty":
            for fd in os.openpty():
                os.close(fd)
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    return "ok"

status = open("/proc/self/status", encoding="utf-8").read()
print(json.dumps({
    "pid": os.getpid(),
    "argv": sys.argv[2:],
    "no_new_privs": [line.split()[1] for line in status.splitlines() if line.startswith("NoNewPrivs:")],
    "environment": open("/proc/self/environ", "rb").read().decode("utf-8").split("\0")[:-1],
    "descriptors": sorted(os.listdir("/proc/self/fd")),
    "results": [attempt(*step) for step in json.loads(sys.argv[1])],
}))
"""


def test_landlock_works_on_the_linux_that_ci_runs() -> None:
    # The kernel tests below skip where Landlock does not work. On CI's Linux it does (ABI 8 on ubuntu-24.04, on the
    # host and in the image: measured 2026-11-09), so a skip there would be those tests not running at all.
    if sys.platform.startswith("linux") and os.environ.get("CI") == "true":
        assert LANDLOCK_ABI >= 1, "Landlock is not available on CI's Linux: the kernel tests of the launcher skipped"


@pytest.fixture
def layout(tmp_path: Path) -> SimpleNamespace:
    """A protected directory laid out as /data is, with the CLI home and two sessions' screenshots in it."""
    base = Path(os.path.realpath(tmp_path))
    root = base / "protected"
    secrets = [
        root / "api-token",
        root / "takeover-token",
        root / "auth" / "profiles" / "work" / "state.json",
        root / "witness" / "keys" / "witness.key",
        root / "mesh" / "identity" / "node.key",
        root / "ssh" / "id_ed25519",
        root / "proxy-personas.json",
        root / "browser-profile" / "browser-ws-endpoint.txt",
    ]
    mine = root / "artifacts" / "session-1" / "shot.png"
    theirs = root / "artifacts" / "session-2" / "shot.png"
    for path in [*secrets, mine, theirs, base / "outside" / "note.txt"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content of {path.name}", encoding="utf-8")
    home = root / "cli-home"
    (home / "a").mkdir(parents=True)
    (home / "b").mkdir()
    return SimpleNamespace(base=base, root=root, home=home, secrets=secrets, mine=mine, theirs=theirs)


def _launcher_command(layout: SimpleNamespace, *options: str) -> list[str]:
    protect = ["--protect", str(layout.root), "--rw", str(layout.home)]
    return [sys.executable, "-I", "-S", str(LAUNCHER), *protect, *options]


def _stand_in(layout: SimpleNamespace, steps: list[list[str]], *options: str, env=None, extra=()) -> dict:
    command = [*_launcher_command(layout, *options), "--", sys.executable, "-c", STAND_IN, json.dumps(steps), *extra]
    done = _run(command, layout, env=env)
    assert done.returncode == 0, f"exit {done.returncode}: {done.stderr[-2000:]}"
    return json.loads(done.stdout)


def _run(command: list[str], layout: SimpleNamespace, *, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        env=env or CHILD_ENV,
        cwd=layout.home,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )


@needs_landlock
def test_a_child_under_the_launcher_cannot_read_any_secret_in_the_protected_root(layout) -> None:
    report = _stand_in(layout, [["read", str(secret)] for secret in layout.secrets])

    assert report["results"] == ["EACCES"] * len(layout.secrets)
    assert layout.secrets[0].read_text(encoding="utf-8") == "content of api-token", "this process still reads it"


@needs_landlock
def test_a_child_under_the_launcher_cannot_list_the_protected_root_or_make_a_file_in_it(layout) -> None:
    steps = [
        ["list", str(layout.root)],
        ["list", str(layout.root / "auth")],
        ["create", str(layout.root / "new-file")],
        ["append", str(layout.root / "api-token")],
        ["remove", str(layout.root / "takeover-token")],
        ["mkdir", str(layout.root / "new-directory")],
    ]

    assert _stand_in(layout, steps)["results"] == ["EACCES"] * len(steps)
    assert not (layout.root / "new-file").exists() and (layout.root / "takeover-token").exists()


@needs_landlock
def test_a_child_under_the_launcher_still_stats_a_secret(layout) -> None:
    # Landlock has no right for it: documented as a limit, and pinned here so that the documents stay true.
    assert _stand_in(layout, [["stat", str(layout.secrets[0])]])["results"] == ["ok"]


@needs_landlock
def test_the_cli_reads_the_screenshot_it_was_given_and_not_another_sessions(layout) -> None:
    steps = [["read", str(layout.mine)], ["read", str(layout.theirs)], ["list", str(layout.mine.parent)]]

    report = _stand_in(layout, steps, "--ro-file", str(layout.mine))

    assert report["results"] == ["content of shot.png", "EACCES", "EACCES"]


@needs_landlock
def test_the_screenshot_is_given_to_read_and_not_to_write_or_remove(layout) -> None:
    steps = [["append", str(layout.mine)], ["remove", str(layout.mine)]]

    assert _stand_in(layout, steps, "--ro-file", str(layout.mine))["results"] == ["EACCES", "EACCES"]
    assert layout.mine.read_text(encoding="utf-8") == "content of shot.png"


@needs_landlock
def test_a_cli_given_no_screenshot_reads_none(layout) -> None:
    assert _stand_in(layout, [["read", str(layout.mine)]])["results"] == ["EACCES"]


@needs_landlock
def test_the_cli_writes_in_its_home_and_renames_across_two_directories_of_it(layout) -> None:
    first, second = layout.home / "a" / "file.txt", layout.home / "b" / "file.txt"
    steps = [["create", str(first)], ["rename", str(first), str(second)], ["list", str(layout.home)]]
    if LANDLOCK_ABI < 2:
        steps.pop(1)  # a rename across directories needs REFER, which the first ABI refuses to everyone

    report = _stand_in(layout, steps)

    assert set(report["results"]) == {"ok"}
    assert (second if LANDLOCK_ABI >= 2 else first).is_file()


@needs_landlock
def test_beside_the_root_the_cli_reads_lists_and_runs_and_writes_nothing(layout) -> None:
    # What the controller, which is not under the ruleset, could later read or run is no CLI's to leave there.
    outside = layout.base / "outside"
    note, program = outside / "note.txt", outside / "program"
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    works = [["read", str(note)], ["list", str(outside)], ["run", str(program)], ["list", "/proc/self"]]
    refused = [
        ["create", str(outside / "new.txt")],
        ["append", str(note)],
        ["write", str(note)],
        ["remove", str(note)],
        ["rename", str(note), str(outside / "renamed.txt")],
        ["mkdir", str(outside / "new-directory")],
    ]
    if LANDLOCK_ABI >= 3:
        refused.append(["truncate", str(note)])  # the third ABI's right: below it, Landlock cannot refuse it

    # /dev as the controller gives it to every CLI (SHARED_WRITABLE): the run's stdin is /dev/null, which Python opens
    # to read and write, and without /dev to write that open is refused before the program starts.
    report = _stand_in(layout, works + refused, "--rw", "/dev")

    assert report["results"] == ["content of note.txt", "ok", "ok", "ok", *["EACCES"] * len(refused)]
    assert note.read_text(encoding="utf-8") == "content of note.txt"
    assert sorted(path.name for path in outside.iterdir()) == ["note.txt", "program"]


@needs_landlock
def test_a_writable_directory_swapped_for_a_link_into_the_root_ends_the_launcher_and_the_cli_does_not_run(
    layout,
) -> None:
    # A CLI of another call removed the directory the controller resolved and put a link to the root's auth there.
    swapped = layout.base / "outside" / "swapped"
    swapped.symlink_to(layout.root / "auth", target_is_directory=True)
    marker = layout.home / "ran"
    run = [sys.executable, "-c", "open(r'%s', 'w')" % marker]

    done = _run([*_launcher_command(layout, "--rw", str(swapped)), "--", *run], layout)

    assert done.returncode == launcher.EXIT_NOT_APPLIED
    assert done.stderr == f"{launcher.MARKER}--rw {swapped}: reached through a link\n"
    assert not marker.exists(), "the CLI ran"


@needs_landlock
def test_a_directory_that_holds_the_root_is_not_listed_and_takes_no_new_entry(layout) -> None:
    # No rule goes on what holds the root (/ in production), so the rights to list it and to make an entry in it are
    # nobody's. What lies beside the root in it has a rule of its own, as the test above shows.
    steps = [
        ["list", "/"],
        ["list", str(layout.base)],
        ["create", str(layout.base / "new-file")],
        ["mkdir", str(layout.base / "new-directory")],
    ]

    assert _stand_in(layout, steps)["results"] == ["EACCES"] * len(steps)


@needs_landlock
def test_a_link_beside_the_root_that_names_a_directory_in_it_is_no_way_in(layout) -> None:
    (layout.base / "way-in").symlink_to(layout.root / "auth", target_is_directory=True)
    (layout.base / "dangling").symlink_to(layout.base / "nowhere")
    inside = layout.base / "way-in" / "profiles" / "work" / "state.json"
    assert inside.read_text(encoding="utf-8") == "content of state.json"

    assert _stand_in(layout, [["read", str(inside)], ["list", str(layout.base / "way-in")]])["results"] == [
        "EACCES",
        "EACCES",
    ]


@needs_landlock
def test_the_exec_keeps_the_pid_and_passes_the_clis_arguments_as_they_are(layout) -> None:
    extra = ["--probe", "--protect", "/", "--", "--rw", str(layout.root)]
    command = [*_launcher_command(layout), "--", sys.executable, "-c", STAND_IN, "[]", *extra]

    process = subprocess.Popen(
        command, env=CHILD_ENV, cwd=layout.home, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True
    )
    output, _ = process.communicate(timeout=120)

    report = json.loads(output)
    assert report["pid"] == process.pid
    assert report["argv"] == extra


@needs_landlock
def test_the_cli_gets_the_environment_the_launcher_was_given_and_nothing_more(layout) -> None:
    # No LANG and no LC_*: the C locale, in which Python adds LC_CTYPE to its own environment as it starts (PEP 538).
    env = {**CHILD_ENV, "HOME": str(layout.home), "PROVIDER_KEY": "k=v with spaces", "EMPTY": ""}

    report = _stand_in(layout, [], env=env)

    assert sorted(report["environment"]) == sorted(f"{name}={value}" for name, value in env.items())


@needs_landlock
def test_the_cli_starts_with_no_descriptor_of_the_launchers_and_with_no_new_privs(layout) -> None:
    report = _stand_in(layout, [], "--ro-file", str(layout.mine))

    # 0, 1 and 2, and the one the listing of /proc/self/fd itself holds.
    assert len(report["descriptors"]) == 4 and report["descriptors"][:3] == ["0", "1", "2"]
    assert report["no_new_privs"] == ["1"]


@needs_landlock
def test_the_cli_starts_with_the_signals_a_direct_start_gives_it(layout) -> None:
    # Python ignores SIGPIPE and SIGXFSZ from its start, and an ignored signal stays ignored through an exec.
    def ignored(command: list[str]) -> list[str]:
        done = _run(command, layout)
        assert done.returncode == 0, done.stderr[-2000:]
        return [line for line in done.stdout.splitlines() if line.startswith(("SigIgn:", "SigBlk:"))]

    status = ["cat", "/proc/self/status"]

    assert ignored([*_launcher_command(layout), "--", *status]) == ignored(status)


@needs_landlock
def test_the_probe_exits_zero_and_runs_nothing(layout) -> None:
    marker = layout.home / "ran"
    command = [*_launcher_command(layout, "--probe"), "--", sys.executable, "-c", "open(r'%s', 'w')" % marker]

    done = _run(command, layout)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert not marker.exists()


@needs_landlock
@pytest.mark.parametrize("grant", ["a home that is not there", "the root's parent as a home", "a directory as a file"])
def test_a_bad_rule_exits_with_the_launchers_status_and_the_cli_does_not_run(layout, grant: str) -> None:
    options = {
        "a home that is not there": ["--rw", str(layout.base / "not-there")],
        "the root's parent as a home": ["--rw", str(layout.base)],
        "a directory as a file": ["--ro-file", str(layout.mine.parent)],
    }[grant]
    marker = layout.home / "ran"
    command = [*_launcher_command(layout, *options), "--", sys.executable, "-c", "open(r'%s', 'w')" % marker]

    done = _run(command, layout)

    assert done.returncode == launcher.EXIT_NOT_APPLIED
    assert done.stderr.startswith(f"{launcher.MARKER}{options[0]} {options[1]}: ")
    assert not marker.exists(), "the CLI ran without the ruleset"


@needs_landlock
def test_a_cli_that_is_not_there_exits_127_with_the_launchers_line(layout) -> None:
    command = [*_launcher_command(layout), "--", "auto-browser-no-such-cli"]

    # A PATH of its own: each of its directories can be searched, so the one failure is that the CLI is in none.
    done = _run(command, layout, env={"PATH": "/usr/bin:/bin"})

    assert done.returncode == launcher.EXIT_NOT_FOUND
    assert done.stderr.startswith(launcher.MARKER + "execvpe: ENOENT")


@needs_landlock
def test_a_bare_cli_name_is_found_in_a_directory_that_only_the_childs_own_path_names(layout) -> None:
    # The exec looks the name up in the PATH of the environment the launcher was started with (its bytes), as a
    # child started directly would: the image's CLIs, outside os.defpath, start by bare name so.
    directory = layout.base / "outside" / "only-here"
    directory.mkdir()
    cli = directory / "auto-browser-test-cli"
    cli.write_text("#!/bin/sh\necho found by its PATH\n", encoding="utf-8")
    cli.chmod(0o755)
    assert str(directory) not in [*os.environ.get("PATH", "").split(os.pathsep), *os.defpath.split(os.pathsep)]

    done = _run([*_launcher_command(layout), "--", cli.name], layout, env={"PATH": str(directory)})

    assert (done.returncode, done.stdout) == (0, "found by its PATH\n"), done.stderr[-2000:]


# A stand-in for the controller: non-dumpable, as the controller makes itself at its start (PR_SET_DUMPABLE is 4),
# it starts the command it is given and passes on what that printed.
_NON_DUMPABLE_PARENT = r"""
import ctypes, subprocess, sys
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(*[ctypes.c_ulong(value) for value in (4, 0, 0, 0, 0)]) != 0:
    sys.exit("could not become non-dumpable")
done = subprocess.run(sys.argv[1:], capture_output=True, text=True, timeout=100)
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
sys.exit(done.returncode)
"""
_READ_THE_PARENTS_ENVIRON = r"""
import os
try:
    open("/proc/%d/environ" % os.getppid(), "rb").read()
except PermissionError:
    print("denied")
except OSError:
    print("error")
else:
    print("read")
print("own status read" if open("/proc/self/status").read().startswith("Name:") else "own status missing")
"""


@needs_landlock
def test_proc_stays_readable_and_the_controllers_environ_does_not(layout) -> None:
    # /proc is beside the protected root, so the ruleset allows it: what keeps the controller's environ from a CLI
    # is that the controller is non-dumpable, and that a restricted process may not look into one that is not.
    child = [*_launcher_command(layout), "--", sys.executable, "-c", _READ_THE_PARENTS_ENVIRON]
    command = [sys.executable, "-c", _NON_DUMPABLE_PARENT, *child]

    done = _run(command, layout)

    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.splitlines() == ["denied", "own status read"]

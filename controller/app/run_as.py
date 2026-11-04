"""Who the controller runs as: never root by default (CONTROLLER_RUN_AS=auto|root).

controller/entrypoint.sh starts as root only to run `python -m app.run_as`, then execs uvicorn through setpriv as the
user this prints. auto picks:

- the owner of /data, when that is neither root nor the browser's uid (10001): a bind mount the host user owns (a
  Linux clone of the repo, CI). The host user keeps its files, and the 0600 data/api-token stays theirs, so the stdio
  bridge and scripts read it;
- otherwise the image's controller user, uid 10002 (Docker Desktop, where bind mounts show as root, a named volume,
  or a data dir a 1.14 pre-release left owned by 10001). It never shares the browser containers' uid.

The primary gid is the data dir's group only when that is not 0 and not a system group of the image; otherwise the
uid's own (the entrypoint creates it).

Before that, entries of the controller's own /data subtrees left by earlier releases (owned by root, or by 10001 from
a pre-release) are handed to that user. The walk holds directory fds and stats and chowns relative to them without
following links (its own dir-fd walk), so swapping a directory for a symlink mid-walk cannot steer it out of the
tree; it stays on /data's filesystem (sub-mounts are skipped), skips hard-linked files, and never enters the browser's
directories (browser-profile, downloads, browser-sessions/*/profile and */downloads). A chown that fails is an ERROR
naming the path; the controller still starts, and a summary says the pass repeats on every start until it is fixed.
A marker file records a clean pass so later starts skip the scan.
root keeps the old behaviour and readiness warns about it.

The root step is Linux-only (dir fds); the entrypoint is the only caller.
"""

from __future__ import annotations

import errno
import fnmatch
import logging
import os
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONTROLLER_UID = 10002
# browser-node's `browser` user (uid and gid): the controller never runs as it.
BROWSER_UID = 10001
DATA_ROOT = Path("/data")
# Set by the entrypoint for readiness: why the controller runs as the user it does.
REASON_ENV = "CONTROLLER_RUN_AS_REASON"
# Written after a pass that handed everything over, holding "uid:gid\n": later starts as that user skip the scan,
# which on a Docker Desktop bind mount took 71.66s for 20102 entries even with nothing to change. A root run
# removes it (root may write root-owned files); delete it by hand to force a new pass.
MARKER_NAME = ".auto-browser-owner"
_MARKER_MAX_BYTES = 64
# Relative to /data: the browser containers' own directories, never walked.
BROWSER_OWNED_GLOBS = ("browser-profile", "downloads", "browser-sessions/*/profile", "browser-sessions/*/downloads")


@dataclass
class UserDecision:
    uid: int | None  # None: stay root
    gid: int | None
    reason: str


def _is_image_system_group(gid: int) -> bool:
    if gid >= 1000:
        return False
    try:
        import grp

        grp.getgrgid(gid)
    except (ImportError, KeyError):
        return False
    return True


# Never the controller's primary group: root, the browser user's group (the decided design shares no group with
# the browser), and nogroup.
NOBODY_UID = 65534
# The walk is depth-bounded, never recursing into a RecursionError: deeper entries are errors (and no marker).
MAX_WALK_DEPTH = 200
# And bounded in breadth: progress is logged every WALK_PROGRESS_EVERY entries, and a pass stops after
# MAX_WALK_ENTRIES (an error: no marker, the next start goes on).
WALK_PROGRESS_EVERY = 10_000
# What root without DAC capabilities needs, as "others", to open and list a directory it handed over (o+r and o+x):
# an errored subtree's ancestor, or /data, is handed over only with both, so the next start can retry it.
ROOT_REOPENS = stat.S_IROTH | stat.S_IXOTH
MAX_WALK_ENTRIES = 2_000_000
_REFUSED_GIDS = frozenset({0, BROWSER_UID, NOBODY_UID})


def _safe_gid(gid: int, is_system_group: Callable[[int], bool]) -> bool:
    return gid not in _REFUSED_GIDS and not is_system_group(gid)


def decide_user(
    run_as: str,
    *,
    data_root: Path,
    owner_uid: int,
    owner_gid: int,
    is_system_group: Callable[[int], bool] = _is_image_system_group,
) -> UserDecision:
    if run_as not in {"auto", "root"}:
        raise ValueError(f"CONTROLLER_RUN_AS must be auto or root, got '{run_as}'")
    if run_as == "root":
        return UserDecision(None, None, "CONTROLLER_RUN_AS=root (not recommended)")
    root = data_root.as_posix()
    if owner_uid == 0:
        return UserDecision(
            CONTROLLER_UID,
            CONTROLLER_UID,
            f"{root} belongs to root, so the image's controller user (uid {CONTROLLER_UID})",
        )
    if owner_uid == BROWSER_UID:
        # A 1.14 pre-release left it so, or a host user happens to have uid 10001 (docs: chown ./data to another uid).
        return UserDecision(
            CONTROLLER_UID,
            CONTROLLER_UID,
            f"{root} is owned by uid {BROWSER_UID}, browser-node's uid, which the controller never runs as, so the "
            f"image's controller user (uid {CONTROLLER_UID})",
        )
    reason = f"the owner of {root} (uid {owner_uid})"
    gid = owner_gid
    if not _safe_gid(gid, is_system_group):
        fallback = owner_uid if _safe_gid(owner_uid, is_system_group) else CONTROLLER_UID
        kind = {0: "root", BROWSER_UID: "browser-node's group", 65534: "nogroup"}.get(gid, "a system group")
        reason += f"; its group {gid} is {kind}, so group {fallback}"
        gid = fallback
    return UserDecision(owner_uid, gid, reason)


def controller_owned_paths(settings: Any, data_root: Path = DATA_ROOT) -> list[Path]:
    """The controller's own files and directories under data_root, outermost only."""
    parent = lambda value: Path(value).parent  # noqa: E731 - file settings: their directory is the subtree
    candidates: list[Path] = [
        Path(settings.api_token_file),
        Path(settings.artifact_root),
        Path(settings.upload_root),
        Path(settings.auth_root),
        Path(settings.approval_root),
        Path(settings.audit_root),
        Path(settings.witness_root),
        Path(settings.session_store_root),
        Path(settings.job_store_root),
        Path(settings.compliance_manifest_path),
        Path(settings.harness_root),
        Path(settings.memory_root),
        parent(settings.isolated_tunnel_key_path),
        parent(settings.isolated_tunnel_known_hosts_path),
        Path(settings.isolated_tunnel_info_root),
        parent(settings.remote_access_info_path),
        parent(settings.mcp_session_store_path),
        parent(settings.cron_store_path),
        data_root / "browser-sessions",
        data_root / "db",
    ]
    if settings.cli_home:
        candidates.append(Path(settings.cli_home))
    # The browser containers own these (browser-node's entrypoint hands them to its own user).
    browser_owned = {parent(settings.browser_ws_endpoint_file), data_root / "browser-profile", data_root / "downloads"}

    def lexically_clean(path: Path) -> bool:
        # A ".." would let /data/../etc pass is_relative_to; a "." is noise that could hide one.
        return not any(part in {"..", "."} for part in path.parts) and os.path.normpath(path) == str(path)

    inside = sorted(
        {
            path
            for path in candidates
            if lexically_clean(path)
            and path != data_root
            and path.is_relative_to(data_root)
            and path not in browser_owned
        },
        key=lambda path: len(path.parts),
    )
    outermost: list[Path] = []
    for path in inside:
        if not any(path.is_relative_to(kept) for kept in outermost):
            outermost.append(path)
    return sorted(outermost)


@dataclass
class OwnershipReport:
    changed: int = 0
    examined: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # Directories already the user's that root could not open: taken as handed over (contents not looked at).
    unchecked: list[str] = field(default_factory=list)
    # Directories kept as their owner's because something under them failed and handing them over would lock root
    # out (no o+x): the next start can still reach and retry their contents.
    withheld: list[str] = field(default_factory=list)
    truncated: bool = False
    changed_paths: list[str] = field(default_factory=list)
    seconds: float = 0.0


def _escaped(text: str) -> str:
    """A path or message as one printable log line, always escaped (D14, P5#8): backslashes too, so an escaped
    sequence in a name cannot pass for a real one, and newlines and ANSI escapes never reach the log raw."""
    return text.encode("unicode_escape", "backslashreplace").decode("ascii")


def _browser_owned(relative: str) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in BROWSER_OWNED_GLOBS)


def _open_dir_nofollow(name: str, dir_fd: int) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def _unescape_mountinfo(field: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as \040, \011, \012, \134.
    for code, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(code, char)
    return field


def mount_points_under(data_root: Path, mountinfo: str | None = None) -> set[str]:
    """Paths (relative to data_root) that are mount points inside it, from /proc/self/mountinfo.

    st_dev alone misses a bind mount from the same filesystem, and on Docker Desktop every host bind shares one."""
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return set()
    root = os.path.normpath(str(data_root))
    found = set()
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        point = os.path.normpath(_unescape_mountinfo(fields[4]))
        if point != root and point.startswith(root.rstrip("/") + "/"):
            found.add(point[len(root.rstrip("/")) + 1 :])
    return found


def fix_ownership(
    data_root: Path,
    paths: list[Path],
    uid: int,
    gid: int,
    *,
    from_uids: frozenset[int] = frozenset({0}),
    lstat: Callable[..., os.stat_result] = os.stat,
    before_open: Callable[[str], None] | None = None,
    mountinfo: str | None = None,
    max_entries: int | None = None,
) -> OwnershipReport:
    """Give entries owned by one of from_uids to uid:gid: data_root itself (not recursively) and each path's tree.

    Everything goes through directory fds, bottom-up: an entry is lstat'ed relative to its directory's fd; a
    directory is opened O_NOFOLLOW and checked to be the entry that was lstat'ed before it is entered, its contents are
    handed over, and only then is the directory itself fchown'ed through the fd still held. So a directory swapped
    for a symlink between the stat and the open is not entered, and root, even without CAP_DAC_OVERRIDE, never loses
    access to a 0700 directory it has not finished. Skipped and logged: entries on another device, mount points under
    data_root (from mountinfo), non-directories with more than one link, the browser containers' directories, and
    any path with "..". Every OSError is an error in the report, never an exception (no marker is then written).
    `lstat`, `before_open` and `mountinfo` are test hooks."""
    report = OwnershipReport()
    max_entries = MAX_WALK_ENTRIES if max_entries is None else max_entries
    started = time.monotonic()
    mounts = mount_points_under(data_root, mountinfo)

    def chown_fd(fd: int, relative: str, st: os.stat_result) -> None:
        if st.st_uid in from_uids:
            try:
                os.fchown(fd, uid, gid)
                report.changed += 1
                report.changed_paths.append(relative)
            except OSError as exc:
                report.errors.append(f"{data_root / relative}: {exc.strerror or exc}")

    def skip(relative: str, st: os.stat_result, root_dev: int) -> bool:
        if st.st_dev != root_dev:
            report.skipped.append(f"{relative}: another filesystem (a mount under {data_root})")
            return True
        if relative in mounts:
            report.skipped.append(f"{relative}: a mount point under {data_root}")
            return True
        if stat.S_ISDIR(st.st_mode) and _browser_owned(relative):
            report.skipped.append(f"{relative}: a browser container's directory")
            return True
        if not stat.S_ISDIR(st.st_mode) and st.st_nlink > 1:
            report.skipped.append(f"{relative}: {st.st_nlink} hard links")
            return True
        return False

    def visit(parent_fd: int, name: str, relative: str, root_dev: int, depth: int = 0) -> None:
        if report.truncated:
            return
        if report.examined >= max_entries:
            report.truncated = True
            report.errors.append(f"{data_root}: more than {max_entries} entries, the pass stopped there")
            return
        if depth > MAX_WALK_DEPTH:
            report.errors.append(f"{data_root / relative}: deeper than {MAX_WALK_DEPTH} levels, not handed over")
            return
        try:
            st = lstat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            report.errors.append(f"{data_root / relative}: {exc.strerror or exc}")
            return
        report.examined += 1
        if report.examined % WALK_PROGRESS_EVERY == 0:
            logger.info("hand-over pass: %d entries examined so far", report.examined)
        if skip(relative, st, root_dev):
            return
        if not stat.S_ISDIR(st.st_mode):
            if st.st_uid in from_uids:
                try:
                    os.chown(name, uid, gid, dir_fd=parent_fd, follow_symlinks=False)
                    report.changed += 1
                    report.changed_paths.append(relative)
                except OSError as exc:
                    report.errors.append(f"{data_root / relative}: {exc.strerror or exc}")
            return
        if before_open is not None:
            before_open(relative)
        try:
            fd = _open_dir_nofollow(name, parent_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):  # swapped for a symlink or a file since the lstat
                report.skipped.append(f"{relative}: no longer a directory")
            elif exc.errno == errno.EACCES and st.st_uid == uid:
                # Root without CAP_DAC_READ_SEARCH cannot open a 0700 directory that is the user's already: one an
                # earlier start handed over (never one whose subtree failed and that root would lose access to, see
                # below) or one the user made (a mkdtemp staging dir). Taken as handed over; its contents were not
                # looked at, which main() logs once at INFO.
                report.unchecked.append(relative)
            else:
                report.errors.append(f"{data_root / relative}: {exc.strerror or exc}")
            return
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                report.skipped.append(f"{relative}: replaced since it was examined")
                return
            try:
                # Listed lazily and no further than the cap allows; only what was taken is sorted.
                room = max_entries - report.examined + 1
                children = []
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if len(children) >= room:
                            break
                        children.append(entry.name)
                children.sort()
            except OSError as exc:
                report.errors.append(f"{data_root / relative}: {exc.strerror or exc}")
                return
            errors_before = len(report.errors)
            for child in children:
                if report.truncated:
                    break
                visit(fd, child, f"{relative}/{child}", root_dev, depth + 1)
            # Bottom-up, after its contents. When some of them failed, a directory root could not enter as another
            # owner's (no o+x: root without DAC capabilities) keeps its owner, so the next start can still reach and
            # retry the failed entries; one with o+x is handed over all the same.
            if len(report.errors) == errors_before or opened.st_mode & ROOT_REOPENS == ROOT_REOPENS:
                chown_fd(fd, relative, opened)
            else:
                report.withheld.append(relative)
        finally:
            os.close(fd)

    try:
        root_fd = os.open(str(data_root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        report.errors.append(f"{data_root}: {exc.strerror or exc}")
        report.seconds = time.monotonic() - started
        return report
    try:
        root_st = os.fstat(root_fd)
        report.examined += 1
        for top in paths:
            try:
                relative_top = top.relative_to(data_root).as_posix()
                parts = relative_top.split("/")
                if any(part in {"..", ".", ""} for part in parts):
                    report.errors.append(f"{top}: not a plain path under {data_root}, not handed over")
                    continue
                # Open the top's parent component by component, refusing symlinks and directories a non-root user
                # owns: a configured path deeper than one level must not be steered elsewhere through its parents.
                parent_fd = os.dup(root_fd)
                try:
                    for index, part in enumerate(parts[:-1]):
                        try:
                            st = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            break
                        parent_relative = "/".join(parts[: index + 1])
                        if st.st_dev != root_st.st_dev or parent_relative in mounts:
                            # Like a top that is itself a mount point: skipped, not an error.
                            report.skipped.append(
                                f"{parent_relative}: a mount point under {data_root} among the parents of {top}"
                            )
                            break
                        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0:
                            report.errors.append(
                                f"{data_root / parent_relative}: a parent of {top} that is not a "
                                "root-owned directory, so its tree was not handed over"
                            )
                            break
                        if before_open is not None:
                            before_open(parent_relative)
                        next_fd = _open_dir_nofollow(part, parent_fd)
                        os.close(parent_fd)
                        parent_fd = next_fd
                        opened_parent = os.fstat(parent_fd)
                        if (opened_parent.st_dev, opened_parent.st_ino) != (st.st_dev, st.st_ino):
                            report.errors.append(
                                f"{data_root / parent_relative}: a parent of {top} replaced since it was examined"
                            )
                            break
                    else:
                        visit(parent_fd, parts[-1], relative_top, root_st.st_dev)
                finally:
                    os.close(parent_fd)
            except OSError as exc:
                report.errors.append(f"{top}: {exc.strerror or exc}")
        # /data itself last, after everything under it, on the same rule: a stuck entry must not keep the controller
        # from writing into /data unless handing /data over would lock root out of it.
        if not report.errors or root_st.st_mode & ROOT_REOPENS == ROOT_REOPENS:
            chown_fd(root_fd, ".", root_st)
        else:
            report.withheld.append(".")
    finally:
        os.close(root_fd)
    report.seconds = time.monotonic() - started
    return report


def read_marker(data_root: Path, uid: int) -> bytes | None:
    """The marker's bytes, or None for anything odd (absent, a symlink, not a regular file, another owner, too big).

    Opened through /data's fd with O_NOFOLLOW|O_NONBLOCK, so a planted symlink or FIFO is neither followed nor
    waited on; never decoded, so no content can crash the root step."""
    try:
        root_fd = os.open(str(data_root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        fd = os.open(MARKER_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    except OSError:
        return None
    finally:
        os.close(root_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_size > _MARKER_MAX_BYTES:
            return None
        return os.read(fd, _MARKER_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)


# The marker child's exit code for anything but an OSError (whose errno it exits with otherwise).
_CHILD_UNEXPECTED = 255


def write_marker(data_root: Path, uid: int, gid: int) -> None:
    """Replace the marker with a new regular file owned by uid:gid.

    Root opens /data and writes the marker through that fd as the user, in a child that has switched to it: /data is
    the user's by then, and root without CAP_DAC_OVERRIDE (cap_drop ALL plus CHOWN, SETUID, SETGID) could not create
    a file in it. The marker is the user's anyway (it could forge one), so nothing is lost. The old entry is unlinked
    and the new one created O_EXCL|O_NOFOLLOW, so a hard link planted there is never written through."""
    root_fd = os.open(str(data_root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not (hasattr(os, "fork") and os.geteuid() == 0 and uid != 0):
            _write_marker_at(root_fd, uid, gid)
            return
        pid = os.fork()
        if pid == 0:  # the child: become the user, write, and leave without running any of the parent's cleanup
            code = 0
            try:
                os.setgroups([])
                os.setresgid(gid, gid, gid)
                os.setresuid(uid, uid, uid)
                _write_marker_at(root_fd, uid, gid)
            except OSError as exc:
                code = exc.errno if exc.errno and 0 < exc.errno < _CHILD_UNEXPECTED else errno.EIO
            except BaseException:
                code = _CHILD_UNEXPECTED
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
        if code < 0:
            raise OSError(errno.EIO, f"writing the marker as uid {uid} failed: the child was killed by signal {-code}")
        if code == _CHILD_UNEXPECTED:
            raise OSError(errno.EIO, f"writing the marker as uid {uid} failed: an unexpected error in the child")
        if code != 0:
            raise OSError(code, f"writing the marker as uid {uid} failed: {os.strerror(code)}")
    finally:
        os.close(root_fd)


def _write_marker_at(root_fd: int, uid: int, gid: int) -> None:
    try:
        os.unlink(MARKER_NAME, dir_fd=root_fd)
    except FileNotFoundError:
        pass
    except IsADirectoryError:
        os.rmdir(MARKER_NAME, dir_fd=root_fd)
    fd = os.open(MARKER_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=root_fd)
    try:
        os.write(fd, marker_bytes(uid, gid))
        st = os.fstat(fd)
        if (st.st_uid, st.st_gid) != (uid, gid):
            os.fchown(fd, uid, gid)
    finally:
        os.close(fd)


def remove_marker(data_root: Path) -> None:
    try:
        root_fd = os.open(str(data_root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        os.unlink(MARKER_NAME, dir_fd=root_fd)  # unlink never follows a symlink
    except OSError as exc:
        if exc.errno not in (errno.ENOENT,):
            logger.warning("Could not remove %s: %s", data_root / MARKER_NAME, exc)
    finally:
        os.close(root_fd)


def marker_bytes(uid: int, gid: int) -> bytes:
    return f"{uid}:{gid}\n".encode("ascii")


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def main(*, run_as: str | None = None, settings: Any = None, data_root: Path | None = None) -> int:
    """Print exactly three lines for entrypoint.sh, which parses them strictly (no eval):
    RUN_UID=<digits, or empty to stay root>, RUN_GID=<digits or empty>, RUN_REASON=<one line of text>."""
    run_as = (run_as if run_as is not None else os.environ.get("CONTROLLER_RUN_AS", "auto")).strip().lower() or "auto"
    data_root = data_root or Path(os.environ.get("CONTROLLER_DATA_ROOT", str(DATA_ROOT)))
    try:
        if run_as == "root":  # nothing to look at: no switch, no ownership changes
            decision = decide_user(run_as, data_root=data_root, owner_uid=0, owner_gid=0)
        else:
            st = os.stat(data_root, follow_symlinks=False)
            decision = decide_user(run_as, data_root=data_root, owner_uid=st.st_uid, owner_gid=st.st_gid)
    except ValueError as exc:
        logger.error("%s", exc)
        return 64
    if run_as != "root" and st.st_uid == NOBODY_UID:
        # Root-squashed NFS shows root's files as nobody's, and root's chown cannot work there: running as 65534 is
        # the uid that can still read them (its own 0600 token included).
        logger.warning(
            "%s is owned by uid %s (nobody): data on a root-squashed or NFS mount looks like this. The controller runs "
            "as uid %s, the owner, which can read those files; give ./data to a real uid where you can.",
            data_root,
            NOBODY_UID,
            NOBODY_UID,
        )
    if decision.uid is None:
        remove_marker(data_root)
        logger.warning(
            "CONTROLLER_RUN_AS=root: the controller runs as root, as before 1.14.0. Not recommended: unset it "
            "(auto) to run as the owner of %s or uid %s.",
            data_root,
            CONTROLLER_UID,
        )
    elif read_marker(data_root, decision.uid) == marker_bytes(decision.uid, decision.gid):
        logger.info(
            "controller runs as uid %s (%s); %s says its files were handed over already (delete it to look again)",
            decision.uid,
            decision.reason,
            data_root / MARKER_NAME,
        )
    else:
        if settings is None:
            from .config import Settings

            settings = Settings()
        paths = controller_owned_paths(settings, data_root)
        from_uids = frozenset({0, BROWSER_UID} - {decision.uid})
        logger.info(
            "Handing the controller's files under %s to uid %s: a one-time pass (skipped on later starts) that can "
            "take a minute or two on Docker Desktop bind mounts with many files.",
            data_root,
            decision.uid,
        )
        report = fix_ownership(data_root, paths, decision.uid, decision.gid, from_uids=from_uids)
        if not report.errors:
            try:
                write_marker(data_root, decision.uid, decision.gid)
            except OSError as exc:
                logger.warning(
                    "Could not write %s (%s): the next start scans /data again", data_root / MARKER_NAME, exc
                )
        logger.info(
            "controller runs as uid %s (%s); %d of %d entries under %s handed to it (from uids %s) in %.2fs",
            decision.uid,
            decision.reason,
            report.changed,
            report.examined,
            data_root,
            ",".join(map(str, sorted(from_uids))),
            report.seconds,
        )
        if report.unchecked:
            logger.warning(
                "%d directories already uid %s's could not be opened without CAP_DAC_READ_SEARCH and were taken as "
                "handed over without looking inside, e.g. %s. After running an older release, or CONTROLLER_RUN_AS=root, "
                "on this data, a container without DAC_READ_SEARCH cannot hand over what those wrote in them: chown "
                "./data by hand then.",
                len(report.unchecked),
                decision.uid,
                ", ".join(_escaped(entry) for entry in report.unchecked[:5]),
            )
        for skipped in report.skipped[:20]:
            logger.warning("Not handed over: %s", _escaped(skipped))
        if len(report.skipped) > 20:
            logger.warning("... and %d more entries not handed over", len(report.skipped) - 20)
        for withheld in report.withheld[:20]:
            logger.warning(
                "Not handed over: %s: kept as its owner's until its contents can be handed over", _escaped(withheld)
            )
        if len(report.withheld) > 20:
            logger.warning("... and %d more directories kept as their owner's", len(report.withheld) - 20)
        for error in report.errors[:20]:
            logger.error(
                "Could not give %s to uid %s, so the controller may not be able to use it. Fix its ownership, or "
                "set CONTROLLER_RUN_AS=root to run as root as before 1.14.0 (not recommended).",
                _escaped(error),
                decision.uid,
            )
        if len(report.errors) > 20:
            logger.error("... and %d more entries that could not be handed over", len(report.errors) - 20)
        if report.errors:
            logger.error(
                "No marker written: %d entries could not be handed over (first: %s), so this pass is retried on every "
                "start until they are fixed. See docs/deployment.md, 'Who the controller runs as'.",
                len(report.errors),
                "; ".join(_escaped(error) for error in report.errors[:3]),
            )
    print(f"RUN_UID={'' if decision.uid is None else decision.uid}")
    print(f"RUN_GID={'' if decision.gid is None else decision.gid}")
    print(f"RUN_REASON={_one_line(decision.reason)}")
    return 0


@dataclass
class ControllerUser:
    euid: int | None
    reason: str


def controller_user_status() -> ControllerUser:
    geteuid = getattr(os, "geteuid", None)
    return ControllerUser(euid=geteuid() if geteuid else None, reason=os.environ.get(REASON_ENV, ""))


def controller_user_check(user: Any):
    from .readiness import ReadinessCheck

    reason = user.reason or "started without the image's entrypoint"
    details = {"uid": user.euid, "reason": reason}
    if user.euid == 0:
        return ReadinessCheck(
            name="controller_user",
            status="warn",
            message=f"The controller runs as root ({reason}): a compromise of it is root in its container.",
            fix=(
                f"Set CONTROLLER_RUN_AS=auto (the default) so it runs as the owner of /data or uid {CONTROLLER_UID}. "
                "A container without CAP_SETUID/CAP_SETGID (cap_drop ALL) cannot switch users: start it as the "
                f'controller user, compose user: "{CONTROLLER_UID}:{CONTROLLER_UID}" (or docker run --user '
                f"{CONTROLLER_UID}:{CONTROLLER_UID}), with ./data owned by that uid, or add cap_add CHOWN, DAC_READ_SEARCH, SETUID and "
                "SETGID."
            ),
            details=details,
        )
    return ReadinessCheck(
        name="controller_user",
        status="pass",
        message=f"The controller runs as uid {user.euid}: {reason}.",
        details=details,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="entrypoint: %(levelname)s %(message)s", stream=sys.stderr)
    sys.exit(main())

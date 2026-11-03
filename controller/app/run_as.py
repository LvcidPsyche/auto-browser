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
following links (os.fwalk), so swapping a directory for a symlink mid-walk cannot steer it out of the tree; it stays
on /data's filesystem (sub-mounts are skipped), skips hard-linked files, and never enters the browser's directories
(browser-profile, downloads, browser-sessions/*/profile and */downloads). A chown that fails is an ERROR naming the
path and the way out; the controller still starts. A marker file records a clean pass so later starts skip the scan.
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
        return UserDecision(
            CONTROLLER_UID,
            CONTROLLER_UID,
            f"{root} belongs to the browser's uid {BROWSER_UID} (a 1.14 pre-release), so the image's controller user "
            f"(uid {CONTROLLER_UID})",
        )
    reason = f"the owner of {root} (uid {owner_uid})"
    gid = owner_gid
    if gid == 0 or is_system_group(gid):
        reason += f"; its group {gid} is {'root' if gid == 0 else 'a system group'}, so the uid's own group {owner_uid}"
        gid = owner_uid
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
    inside = sorted(
        {
            path
            for path in candidates
            if path != data_root and path.is_relative_to(data_root) and path not in browser_owned
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
    changed_paths: list[str] = field(default_factory=list)
    seconds: float = 0.0


def _browser_owned(relative: str) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in BROWSER_OWNED_GLOBS)


def _open_dir_nofollow(name: str, dir_fd: int) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def fix_ownership(
    data_root: Path,
    paths: list[Path],
    uid: int,
    gid: int,
    *,
    from_uids: frozenset[int] = frozenset({0}),
    lstat: Callable[..., os.stat_result] = os.stat,
    after_dir: Callable[[str], None] | None = None,
) -> OwnershipReport:
    """Give entries owned by one of from_uids to uid:gid: data_root itself (not recursively) and each path's tree.

    Everything goes through directory fds: each entry is stat'ed and chowned relative to its directory's fd with
    follow_symlinks=False, and os.fwalk only descends into a directory after checking the fd it opened is the one
    it stat'ed. Entries on another device than data_root, non-directories with more than one link, and the browser's
    directories are skipped. `lstat` and `after_dir` are test hooks (a fake st_dev; a swap between steps)."""
    report = OwnershipReport()
    started = time.monotonic()
    root_fd = os.open(str(data_root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_st = os.fstat(root_fd)
        report.examined += 1
        if root_st.st_uid in from_uids:
            try:
                os.fchown(root_fd, uid, gid)
                report.changed += 1
                report.changed_paths.append(".")
            except OSError as exc:
                report.errors.append(f"{data_root}: {exc.strerror or exc}")

        def handle(dir_fd: int, name: str, relative: str, *, is_dir: bool) -> bool:
            """Chown one entry; False when it must not be descended into."""
            try:
                st = lstat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            except OSError as exc:
                report.errors.append(f"{data_root / relative}: {exc.strerror or exc}")
                return False
            report.examined += 1
            if st.st_dev != root_st.st_dev:
                report.skipped.append(f"{relative}: another filesystem (a mount under {data_root})")
                return False
            if not stat.S_ISDIR(st.st_mode) and st.st_nlink > 1:
                report.skipped.append(f"{relative}: {st.st_nlink} hard links")
                return False
            if st.st_uid in from_uids:
                try:
                    os.chown(name, uid, gid, dir_fd=dir_fd, follow_symlinks=False)
                    report.changed += 1
                    report.changed_paths.append(relative)
                except OSError as exc:
                    report.errors.append(f"{data_root / relative}: {exc.strerror or exc}")
            return is_dir and stat.S_ISDIR(st.st_mode)

        for top in paths:
            relative_top = top.relative_to(data_root).as_posix()
            parts = relative_top.split("/")
            # Open the top's parent component by component, refusing symlinks and directories a non-root user owns:
            # a configured path deeper than one level must not be steered elsewhere through its parents.
            parent_fd = os.dup(root_fd)
            try:
                for index, part in enumerate(parts[:-1]):
                    try:
                        st = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        break
                    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0:
                        report.errors.append(
                            f"{data_root / '/'.join(parts[: index + 1])}: a parent of {top} that is not a root-owned "
                            "directory, so its tree was not handed over"
                        )
                        break
                    next_fd = _open_dir_nofollow(part, parent_fd)
                    os.close(parent_fd)
                    parent_fd = next_fd
                else:
                    name = parts[-1]
                    if not handle(parent_fd, name, relative_top, is_dir=True):
                        continue
                    for dirpath, dirnames, filenames, dir_fd in os.fwalk(name, dir_fd=parent_fd, follow_symlinks=False):
                        base = "/".join(parts[:-1] + [dirpath]) if len(parts) > 1 else dirpath
                        keep = []
                        for child in sorted(dirnames):
                            relative = f"{base}/{child}"
                            if _browser_owned(relative):
                                report.skipped.append(f"{relative}: a browser container's directory")
                                continue
                            if handle(dir_fd, child, relative, is_dir=True):
                                keep.append(child)
                        dirnames[:] = keep
                        for child in sorted(filenames):
                            handle(dir_fd, child, f"{base}/{child}", is_dir=False)
                        if after_dir is not None:
                            after_dir(base)
            finally:
                os.close(parent_fd)
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


def write_marker(data_root: Path, uid: int, gid: int) -> None:
    """Replace the marker with a new regular file owned by uid:gid.

    The old entry is unlinked and the new one created O_EXCL|O_NOFOLLOW: O_TRUNC on an existing name would let a
    hard link planted there make root truncate the linked file."""
    root_fd = os.open(str(data_root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.unlink(MARKER_NAME, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        except IsADirectoryError:
            os.rmdir(MARKER_NAME, dir_fd=root_fd)
        fd = os.open(MARKER_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=root_fd)
        try:
            os.write(fd, marker_bytes(uid, gid))
            os.fchown(fd, uid, gid)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


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
        for skipped in report.skipped[:20]:
            logger.warning("Not handed over: %s", skipped)
        for error in report.errors[:20]:
            logger.error(
                "Could not give %s to uid %s, so the controller may not be able to use it. Fix its ownership, or "
                "set CONTROLLER_RUN_AS=root to run as root as before 1.14.0 (not recommended).",
                error,
                decision.uid,
            )
        if len(report.errors) > 20:
            logger.error("... and %d more entries that could not be handed over", len(report.errors) - 20)
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
            fix=f"Set CONTROLLER_RUN_AS=auto (the default) so it runs as the owner of /data or uid {CONTROLLER_UID}.",
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

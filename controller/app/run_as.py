"""Who the controller runs as: never root by default (CONTROLLER_RUN_AS=auto|root).

controller/entrypoint.sh starts as root only to run `python -m app.run_as`, then execs uvicorn through setpriv as the
user this prints. auto picks:

- the owner of /data, when that is not root: a bind mount the host user owns (a Linux clone of the repo, CI). The
  host user keeps its files, and the 0600 data/api-token stays theirs, so the stdio bridge and scripts read it;
- otherwise the image's controller user, uid 10001 (Docker Desktop, where bind mounts show as root, or a named
  volume).

Before that, root-owned entries of the controller's own /data subtrees (what releases before 1.14.0 wrote as root)
are handed to that user: only entries owned by root, never a blanket chown, never the browser's directories
(browser-profile, downloads), never anything outside /data. A chown that fails (a read-only or root-squashed mount) is
an ERROR naming the path and the way out; the controller still starts as that user, so whatever it then cannot
read fails with its own message. root keeps the old behaviour and readiness warns about it.
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONTROLLER_UID = 10001
DATA_ROOT = Path("/data")
# Set by the entrypoint for readiness: why the controller runs as the user it does.
REASON_ENV = "CONTROLLER_RUN_AS_REASON"
# Written after a pass that handed everything over, holding "uid:gid": later starts as that user skip the scan,
# which on a Docker Desktop bind mount took 71.66s for 20102 entries even with nothing to change. A root run
# removes it (root may write root-owned files); delete it by hand to force a new pass.
MARKER_NAME = ".auto-browser-owner"

Owner = Callable[[Path], int]
Chown = Callable[[Path, int, int], None]


@dataclass
class UserDecision:
    uid: int | None  # None: stay root
    gid: int | None
    reason: str


def decide_user(run_as: str, *, data_root: Path, owner_uid: int, owner_gid: int) -> UserDecision:
    if run_as not in {"auto", "root"}:
        raise ValueError(f"CONTROLLER_RUN_AS must be auto or root, got '{run_as}'")
    if run_as == "root":
        return UserDecision(None, None, "CONTROLLER_RUN_AS=root (not recommended)")
    if owner_uid != 0:
        return UserDecision(owner_uid, owner_gid, f"the owner of {data_root.as_posix()} (uid {owner_uid})")
    return UserDecision(
        CONTROLLER_UID,
        CONTROLLER_UID,
        f"{data_root.as_posix()} belongs to root, so the image's controller user (uid {CONTROLLER_UID})",
    )


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
    seconds: float = 0.0


def _real_owner(path: Path) -> int:
    return os.lstat(path).st_uid


def _real_chown(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid, follow_symlinks=False)


def fix_ownership(
    data_root: Path,
    paths: list[Path],
    uid: int,
    gid: int,
    *,
    owner: Owner = _real_owner,
    chown: Chown = _real_chown,
) -> OwnershipReport:
    """Give root-owned entries to uid:gid: data_root itself (not recursively) and each path's whole tree.

    Symlinks are changed themselves, never followed."""
    report = OwnershipReport()
    started = time.monotonic()

    def visit(path: Path, *, recurse: bool) -> None:
        try:
            report.examined += 1
            if owner(path) == 0:
                chown(path, uid, gid)
                report.changed += 1
        except FileNotFoundError:
            return
        except OSError as exc:
            report.errors.append(f"{path}: {exc.strerror or exc}")
            return
        if recurse and not os.path.islink(path) and os.path.isdir(path):
            try:
                children = sorted(os.scandir(path), key=lambda entry: entry.name)
            except OSError as exc:
                report.errors.append(f"{path}: {exc.strerror or exc}")
                return
            for child in children:
                visit(Path(child.path), recurse=True)

    visit(data_root, recurse=False)
    for path in paths:
        if os.path.lexists(path):
            visit(path, recurse=True)
    report.seconds = time.monotonic() - started
    return report


def main(
    *,
    run_as: str | None = None,
    settings: Any = None,
    data_root: Path | None = None,
    owner: Owner = _real_owner,
    owner_gid: Callable[[Path], int] = lambda path: os.lstat(path).st_gid,
    chown: Chown = _real_chown,
) -> int:
    """Print RUN_UID, RUN_GID and RUN_REASON as shell assignments for entrypoint.sh (empty RUN_UID: stay root)."""
    run_as = (run_as if run_as is not None else os.environ.get("CONTROLLER_RUN_AS", "auto")).strip().lower() or "auto"
    data_root = data_root or Path(os.environ.get("CONTROLLER_DATA_ROOT", str(DATA_ROOT)))
    try:
        if run_as == "root":  # nothing to look at: no switch, no ownership changes
            decision = decide_user(run_as, data_root=data_root, owner_uid=0, owner_gid=0)
        else:
            decision = decide_user(
                run_as, data_root=data_root, owner_uid=owner(data_root), owner_gid=owner_gid(data_root)
            )
    except ValueError as exc:
        logger.error("%s", exc)
        return 64
    marker = data_root / MARKER_NAME
    if decision.uid is None:
        marker.unlink(missing_ok=True)
        logger.warning(
            "CONTROLLER_RUN_AS=root: the controller runs as root, as before 1.14.0. Not recommended: unset it "
            "(auto) to run as the owner of %s or uid %s.",
            data_root,
            CONTROLLER_UID,
        )
    elif _read_marker(marker) == f"{decision.uid}:{decision.gid}":
        logger.info(
            "controller runs as uid %s (%s); %s says its files were handed over already (delete it to look again)",
            decision.uid,
            decision.reason,
            marker,
        )
    else:
        if settings is None:
            from .config import Settings

            settings = Settings()
        paths = controller_owned_paths(settings, data_root)
        report = fix_ownership(data_root, paths, decision.uid, decision.gid, owner=owner, chown=chown)
        if not report.errors:
            try:
                marker.write_text(f"{decision.uid}:{decision.gid}\n", encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not write %s (%s): the next start scans /data again", marker, exc)
        logger.info(
            "controller runs as uid %s (%s); %d root-owned of %d entries under %s handed to it in %.2fs",
            decision.uid,
            decision.reason,
            report.changed,
            report.examined,
            data_root,
            report.seconds,
        )
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
    print(f"RUN_REASON={shlex.quote(decision.reason)}")
    return 0


def _read_marker(marker: Path) -> str | None:
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None


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

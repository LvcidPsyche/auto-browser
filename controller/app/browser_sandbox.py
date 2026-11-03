"""Chromium's own sandbox in the browser containers (BROWSER_SANDBOX=auto|on|off) as readiness reports it.

browser-node/server.mjs (sandbox.mjs) launches Chromium sandboxed, checks from /proc that renderers really are,
falls back under `auto` and refuses under `on`, then writes what happened to browser-sandbox.json beside its
endpoint file, before the endpoint file. The controller reads that file for the shared browser-node (beside
BROWSER_WS_ENDPOINT_FILE) and for each isolated container (in its profile directory, see session_isolation.py).

The state is self-attested: the container it describes writes it, in a directory that container's browser user
owns. So it is read without following links, from a regular file under a size cap, keeping only known keys of the
right types with bounded strings, and only once per browser run (an isolated container's at provisioning, the
shared node's when its endpoint file changes): a browser compromised later cannot rewrite what readiness reports for
the run it was verified in. Under BROWSER_SANDBOX=on the controller enforces it: a container whose state is not
"on" is refused (session_isolation.py) or fails readiness (the shared node).
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BROWSER_SANDBOX_STATE_FILENAME = "browser-sandbox.json"
STATE_MAX_BYTES = 16384
_STATES = {"on", "off", "refused"}
_STRING_KEYS = {"setting": 16, "sandbox": 16, "cause": 64, "detail": 1000, "seccomp_profile": 32}
_VERIFIED_KEYS = {
    "renderers": int,
    "renderer_userns_differs": bool,
    "browser_seccomp_filters": int,
    "renderer_seccomp_filters": list,
    "method": str,
}
# The shared node's state, keyed by its endpoint file's content: one read per browser run.
_SHARED_CACHE: dict[str, Any] = {}

# Moby's default seccomp profile plus chroot, clone and unshare; provenance in seccomp/README.md.
SECCOMP_PROFILE_PATH = Path(__file__).resolve().parent / "browser" / "seccomp" / "chromium-sandbox.json"


def _unreadable(detail: str) -> dict[str, Any]:
    return {"sandbox": "unreadable", "detail": detail[: _STRING_KEYS["detail"]]}


def _sanitize(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _unreadable("browser-sandbox.json is not a JSON object")
    state: dict[str, Any] = {}
    for key, limit in _STRING_KEYS.items():
        if key in raw and (raw[key] is None or isinstance(raw[key], str)):
            state[key] = raw[key] if raw[key] is None else raw[key][:limit]
    if "userns_exposed" in raw and isinstance(raw["userns_exposed"], bool):
        state["userns_exposed"] = raw["userns_exposed"]
    verified = raw.get("verified")
    if isinstance(verified, dict):
        kept: dict[str, Any] = {}
        for key, kind in _VERIFIED_KEYS.items():
            value = verified.get(key)
            if kind is int and isinstance(value, int) and not isinstance(value, bool):
                kept[key] = value
            elif kind is bool and isinstance(value, bool):
                kept[key] = value
            elif kind is str and isinstance(value, str):
                kept[key] = value[:64]
            elif kind is list and isinstance(value, list):
                kept[key] = [item for item in value[:64] if isinstance(item, int) and not isinstance(item, bool)]
        state["verified"] = kept
    if state.get("sandbox") not in _STATES:
        return _unreadable(f"browser-sandbox.json names no known sandbox state ({str(raw.get('sandbox'))[:32]!r})")
    return state


def read_sandbox_state(path: Path) -> dict[str, Any] | None:
    """The state a browser container wrote, sanitized; None while it has written none.

    Opened with O_NOFOLLOW|O_NONBLOCK and required to be a regular file of at most STATE_MAX_BYTES."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return _unreadable(f"{path} could not be opened safely ({exc.strerror or exc}; a symlink is refused)")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return _unreadable(f"{path} is not a regular file")
        if st.st_size > STATE_MAX_BYTES:
            return _unreadable(f"{path} is larger than {STATE_MAX_BYTES} bytes")
        data = os.read(fd, STATE_MAX_BYTES + 1)
    except OSError as exc:
        return _unreadable(f"{path} could not be read ({exc.strerror or exc})")
    finally:
        os.close(fd)
    try:
        return _sanitize(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return _unreadable(f"{path} is not valid JSON")


def load_seccomp_profile(path: str | Path) -> str:
    """The profile as compact JSON, for a container's SecurityOpt (the daemon takes the JSON, not a path).

    Raises OSError or ValueError when the file is missing or not a JSON object."""
    profile = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(profile, dict) or "syscalls" not in profile:
        raise ValueError(f"{path} is not a seccomp profile (no 'syscalls')")
    return json.dumps(profile, separators=(",", ":"))


def _profile_readable(path: str | Path) -> bool:
    try:
        load_seccomp_profile(path)
    except (OSError, ValueError):
        return False
    return True


def _shared_node_state(endpoint_file: Path) -> dict[str, Any] | None:
    state_file = endpoint_file.with_name(BROWSER_SANDBOX_STATE_FILENAME)
    try:
        endpoint = endpoint_file.read_text(encoding="utf-8").strip()
    except OSError:
        endpoint = None
    if endpoint is None:
        # No browser running through this endpoint: report a state file anyway, since a browser that refused to
        # start sandboxed (BROWSER_SANDBOX=on) writes one and no endpoint.
        _SHARED_CACHE.clear()
        return read_sandbox_state(state_file)
    if _SHARED_CACHE.get("endpoint") == endpoint:
        return _SHARED_CACHE["state"]
    state = read_sandbox_state(state_file)
    if state is None:
        return {"sandbox": "unknown"}  # not cached: an older image, or one still writing
    _SHARED_CACHE.clear()
    _SHARED_CACHE.update(endpoint=endpoint, state=state)
    return state


def browser_sandbox_status(settings: Any, provisioner: Any = None) -> dict[str, Any]:
    """What readiness reports: the shared browser-node's state and each live isolated container's."""
    endpoint_file = Path(getattr(settings, "browser_ws_endpoint_file", "") or "")
    browser_node: dict[str, Any] | None = None
    # A controller that never connects through the endpoint file (its own local Chromium, an attach mode) has no
    # browser-node to report; one whose endpoint file exists but holds no state runs an older or still-starting image.
    if endpoint_file.name:
        browser_node = _shared_node_state(endpoint_file)
    states = getattr(provisioner, "sandbox_states", None)
    profile_path = getattr(settings, "isolated_browser_seccomp_profile", str(SECCOMP_PROFILE_PATH))
    return {
        "setting": getattr(settings, "browser_sandbox", "auto"),
        "isolation_mode": getattr(settings, "session_isolation_mode", "shared_browser_node"),
        "profile_path": str(profile_path),
        "profile_readable": _profile_readable(profile_path),
        "browser_node": browser_node,
        "isolated": dict(states()) if callable(states) else {},
    }


@dataclass
class _Line:
    label: str
    state: dict[str, Any] | None

    @property
    def sandbox(self) -> str:
        return str((self.state or {}).get("sandbox", "unknown"))

    def describe(self) -> str:
        if self.sandbox in {"unknown", "unreadable"}:
            detail = (self.state or {}).get("detail") or (
                "no browser-sandbox.json (an image older than 1.14.0, or still starting)"
            )
            return f"{self.label}: sandbox state unknown ({detail})"
        return (
            f"{self.label}: sandbox {self.sandbox} ({self.state.get('cause')}): {self.state.get('detail', '')}".rstrip(
                ": "
            )
        )


def browser_sandbox_check(status: dict[str, Any]):
    """The readiness check. Never silent about a browser without its sandbox: any such browser is a warning that
    names the browser, the cause and the detail its container wrote; `on` with isolation and no readable profile
    fails, since every isolated session would be refused."""
    from .readiness import ReadinessCheck  # readiness imports this module's caller, not this module

    name = "browser_sandbox"
    details = dict(status)
    if (
        status.get("setting") == "on"
        and status.get("isolation_mode") == "docker_ephemeral"
        and not status.get("profile_readable")
    ):
        return ReadinessCheck(
            name=name,
            status="fail",
            message=(
                f"BROWSER_SANDBOX=on, but the seccomp profile {status.get('profile_path')} cannot be read: every "
                "isolated session is refused, since its browser could not be sandboxed."
            ),
            fix="Restore app/browser/seccomp/chromium-sandbox.json or point ISOLATED_BROWSER_SECCOMP_PROFILE at it.",
            details=details,
        )
    lines: list[_Line] = []
    if status.get("browser_node") is not None:
        lines.append(_Line("browser-node", status["browser_node"]))
    for session_id, state in sorted((status.get("isolated") or {}).items()):
        lines.append(_Line(f"session {session_id}", state))
    if not lines:
        return ReadinessCheck(
            name=name,
            status="pass",
            message="No browser container is running yet; each reports its sandbox state when it starts.",
            details=details,
        )
    unsandboxed = [line for line in lines if line.sandbox != "on"]
    # A browser that refused to start, or any browser not verified "on" while BROWSER_SANDBOX=on, is a failure:
    # under on nothing may run unsandboxed, and a refused browser-node is down.
    failing = [line for line in unsandboxed if line.sandbox == "refused" or status.get("setting") == "on"]
    if failing:
        return ReadinessCheck(
            name=name,
            status="fail",
            message=(
                f"{len(failing)} browser container(s) refused to run, or run without a verified sandbox while "
                "BROWSER_SANDBOX=on: " + "; ".join(line.describe() for line in failing)
            ),
            fix=(
                "See each cause. A host that refuses unprivileged user namespaces (AppArmor) cannot run Chromium "
                "sandboxed: use BROWSER_SANDBOX=auto there, or allow them."
            ),
            details=details,
        )
    if not unsandboxed:
        return ReadinessCheck(
            name=name,
            status="pass",
            message=f"Chromium's sandbox is on and verified in {len(lines)} browser container(s).",
            details=details,
        )
    return ReadinessCheck(
        name=name,
        status="warn",
        message=(
            f"Chromium runs without its sandbox in {len(unsandboxed)} of {len(lines)} browser container(s): "
            + "; ".join(line.describe() for line in unsandboxed)
        ),
        fix=(
            "See each cause: start browser containers with the seccomp profile (compose and the controller apply "
            "it), or on a host that refuses unprivileged user namespaces accept the fallback; BROWSER_SANDBOX=on "
            "refuses to run unsandboxed."
        ),
        details=details,
    )

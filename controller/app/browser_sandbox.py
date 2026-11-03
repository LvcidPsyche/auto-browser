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

import errno
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BROWSER_SANDBOX_STATE_FILENAME = "browser-sandbox.json"
STATE_MAX_BYTES = 16384
_STATES = {"on", "off", "refused"}
_STRING_KEYS = {"setting": 16, "sandbox": 16, "cause": 64, "detail": 1000, "seccomp_profile": 32, "endpoint": 512}
_VERIFIED_KEYS = {
    "renderers": int,
    "renderer_userns_differs": bool,
    "browser_seccomp_filters": int,
    "renderer_seccomp_filters": list,
    "method": str,
}

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
        reason = "a symlink, which is refused" if exc.errno == errno.ELOOP else (exc.strerror or str(exc))
        return _unreadable(f"{path} could not be opened ({reason})")
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


def verified_on(state: dict[str, Any] | None) -> bool:
    """'on' with evidence: at least one renderer verified in its own namespace. A bare 'on' is not enough."""
    if not state or state.get("sandbox") != "on":
        return False
    renderers = (state.get("verified") or {}).get("renderers")
    return isinstance(renderers, int) and not isinstance(renderers, bool) and renderers >= 1


def snapshot_shared_state(settings: Any, endpoint: str) -> dict[str, Any]:
    """The shared browser-node's state for the run at `endpoint`, read once when the controller connects to it.

    Only a state that names this endpoint counts: a browser-node rolled back to an image that writes none (1.13.0)
    leaves the previous run's 'on' on disk beside its new endpoint."""
    endpoint_file = Path(getattr(settings, "browser_ws_endpoint_file", "") or "browser-ws-endpoint.txt")
    state = read_sandbox_state(endpoint_file.with_name(BROWSER_SANDBOX_STATE_FILENAME))
    if state is None:
        return {"sandbox": "unknown", "detail": "no browser-sandbox.json (a browser-node image older than 1.14.0)"}
    if state.get("sandbox") == "unreadable":
        return state
    if state.get("endpoint") != endpoint:
        return {
            "sandbox": "unknown",
            "detail": "browser-sandbox.json describes another run of browser-node, not the one connected to (an older "
            "or rolled-back image, or one still starting)",
        }
    return state


def refusal(who: str, state: dict[str, Any] | None) -> str:
    """The error for a session refused under BROWSER_SANDBOX=on (isolated and shared use the same wording)."""
    if state is None:
        described = "no state (an image older than 1.14.0 ignores BROWSER_SANDBOX)"
    elif state.get("sandbox") == "on":
        described = "on without verification evidence"
    else:
        described = f"{state.get('sandbox')} ({state.get('cause')}): {state.get('detail', '')}"
    return f"BROWSER_SANDBOX=on, but {who} reports {described}; the session was refused."


def browser_sandbox_status(
    settings: Any, provisioner: Any = None, *, shared: dict[str, Any] | None = None
) -> dict[str, Any]:
    """What readiness reports: the shared browser-node's state and each live isolated container's.

    `shared` is the connect-time snapshot (BrowserRuntimeService.shared_sandbox); it is never re-read from disk. Without
    one, a state file beside a missing endpoint file is still reported (a browser that refused to start sandboxed
    writes one and no endpoint), and an endpoint file with no snapshot yet is 'unknown'."""
    endpoint_file = Path(getattr(settings, "browser_ws_endpoint_file", "") or "")
    browser_node: dict[str, Any] | None = shared
    if browser_node is None and endpoint_file.name:
        if endpoint_file.is_file():
            browser_node = {"sandbox": "unknown", "detail": "browser-node has not been connected to yet"}
        else:
            browser_node = read_sandbox_state(endpoint_file.with_name(BROWSER_SANDBOX_STATE_FILENAME))
    states = getattr(provisioner, "sandbox_states", None)
    supports = getattr(provisioner, "seccomp_support_known", None)
    profile_path = getattr(settings, "isolated_browser_seccomp_profile", str(SECCOMP_PROFILE_PATH))
    return {
        "setting": getattr(settings, "browser_sandbox", "auto"),
        "isolation_mode": getattr(settings, "session_isolation_mode", "shared_browser_node"),
        "profile_path": str(profile_path),
        "profile_readable": _profile_readable(profile_path),
        # None until the provisioner has asked the daemon (its first isolated session).
        "seccomp_supported": supports() if callable(supports) else None,
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

    @property
    def verified(self) -> bool:
        return verified_on(self.state)

    def describe(self) -> str:
        if self.sandbox in {"unknown", "unreadable"}:
            detail = (self.state or {}).get("detail") or (
                "no browser-sandbox.json (an image older than 1.14.0, or still starting)"
            )
            return f"{self.label}: sandbox state unknown ({detail})"
        if self.sandbox == "on":
            return f"{self.label}: sandbox on without verification evidence"
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
    if (
        status.get("setting") == "on"
        and status.get("isolation_mode") == "docker_ephemeral"
        and status.get("seccomp_supported") is False
    ):
        return ReadinessCheck(
            name=name,
            status="fail",
            message=(
                "BROWSER_SANDBOX=on, but the Docker daemon reports no seccomp support: every isolated session is "
                "refused, since Chromium's seccomp sandbox cannot run."
            ),
            fix="Run the isolated browsers on a daemon with seccomp, or use BROWSER_SANDBOX=auto.",
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
    unsandboxed = [line for line in lines if not line.verified]
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

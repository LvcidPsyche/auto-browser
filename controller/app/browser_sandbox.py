"""Chromium's own sandbox in the browser containers (BROWSER_SANDBOX=auto|on|off) as readiness reports it.

browser-node/server.mjs (sandbox.mjs) launches Chromium sandboxed, checks from /proc that renderers really are,
falls back under `auto` and refuses under `on`, then writes what happened to browser-sandbox.json beside its
endpoint file, before the endpoint file. The controller reads that file for the shared browser-node (beside
BROWSER_WS_ENDPOINT_FILE) and for each isolated container (in its profile directory, see session_isolation.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BROWSER_SANDBOX_STATE_FILENAME = "browser-sandbox.json"

# Moby's default seccomp profile plus chroot, clone and unshare; provenance in seccomp/README.md.
SECCOMP_PROFILE_PATH = Path(__file__).resolve().parent / "browser" / "seccomp" / "chromium-sandbox.json"


def read_sandbox_state(path: Path) -> dict[str, Any] | None:
    """The state a browser container wrote, None while it has written none."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        state = json.loads(text)
    except ValueError:
        return {"sandbox": "unreadable", "detail": f"{path} is not valid JSON"}
    return state if isinstance(state, dict) else {"sandbox": "unreadable", "detail": f"{path} is not a JSON object"}


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


def browser_sandbox_status(settings: Any, provisioner: Any = None) -> dict[str, Any]:
    """What readiness reports: the shared browser-node's state and each live isolated container's."""
    endpoint_file = Path(getattr(settings, "browser_ws_endpoint_file", "") or "")
    browser_node: dict[str, Any] | None = None
    # A controller that never connects through the endpoint file (its own local Chromium, an attach mode) has no
    # browser-node to report; one whose endpoint file exists but holds no state runs an older or still-starting image.
    if endpoint_file.name and endpoint_file.is_file():
        browser_node = read_sandbox_state(endpoint_file.with_name(BROWSER_SANDBOX_STATE_FILENAME)) or {
            "sandbox": "unknown"
        }
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

"""Chromium's own sandbox in the browser containers (BROWSER_SANDBOX=auto|on|off) as readiness reports it.

browser-node/server.mjs (sandbox.mjs) launches Chromium sandboxed, checks from /proc that renderers really are,
falls back under `auto` and refuses under `on`, then writes what happened to browser-sandbox.json beside its
endpoint file, before the endpoint file. The controller reads that file for the shared browser-node (beside
BROWSER_WS_ENDPOINT_FILE) and for each isolated container (in its profile directory, see session_isolation.py).

The state is self-attested: the container it describes writes it, in a directory that container's browser user
owns, so it is trustworthy only until that browser runs untrusted content. It is read without following links, from
a regular file under a size cap, keeping only known keys of the right types with bounded strings (control
characters removed), and it names the endpoint of the run it describes. An isolated container's is read once at
provisioning; the shared node's is a snapshot taken when the controller connects (runtime.py), bound to that
endpoint, reused on a reconnect to the same endpoint and never re-read for it. Under BROWSER_SANDBOX=on the
controller enforces it: an isolated container whose state is not a verified "on" is refused (session_isolation.py),
shared-mode sessions are refused (runtime.py), and readiness fails. In docker_ephemeral mode the shared node runs no
session, so its line is informational.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BROWSER_SANDBOX_STATE_FILENAME = "browser-sandbox.json"
STATE_MAX_BYTES = 16384
PROFILE_MAX_BYTES = 1 << 20
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


def _printable(text: str) -> str:
    """The text with control and format characters as spaces: it reaches logs, readiness and an agent's context.

    C0, DEL and C1; Unicode format (Cf: bidi overrides, zero-width), line (Zl) and paragraph (Zp) separators,
    surrogates (Cs: a lone one makes the JSON response fail to encode), private-use (Co) and unassigned (Cn) code
    points; the variation selectors (U+FE00-FE0F, U+E0100-E01EF); and the invisible marks and letters U+034F,
    U+115F, U+1160, U+180B-180D, U+180F, U+3164 and U+FFA0."""
    return "".join(" " if _unprintable(ch) else ch for ch in text)


_HIDDEN_CATEGORIES = frozenset({"Cf", "Zl", "Zp", "Cs", "Co", "Cn"})
# Invisible although their category is a mark or a letter (S6#14): the combining grapheme joiner, the Hangul fillers
# and the Mongolian free variation selectors.
_HIDDEN_CODE_POINTS = frozenset({0x034F, 0x115F, 0x1160, 0x180B, 0x180C, 0x180D, 0x180F, 0x3164, 0xFFA0})


def _unprintable(ch: str) -> bool:
    code = ord(ch)
    return (
        code < 32
        or 127 <= code < 160
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
        or code in _HIDDEN_CODE_POINTS
        or unicodedata.category(ch) in _HIDDEN_CATEGORIES
    )


def _sanitize(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _unreadable("browser-sandbox.json is not a JSON object")
    state: dict[str, Any] = {}
    for key, limit in _STRING_KEYS.items():
        if key in raw and (raw[key] is None or isinstance(raw[key], str)):
            state[key] = raw[key] if raw[key] is None else _printable(raw[key][:limit])
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
                kept[key] = _printable(value[:64])
            elif kind is list and isinstance(value, list):
                # Whole or not at all: a list with anything but plain ints, or more than 64 entries, is dropped.
                if len(value) <= 64 and all(isinstance(item, int) and not isinstance(item, bool) for item in value):
                    kept[key] = list(value)
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

    Raises OSError or ValueError when the file is missing or not a JSON object. Read without blocking (a FIFO), a
    regular file only, under a size cap. Links are followed: the path is the operator's (a Kubernetes ConfigMap mount
    is a symlink), and whoever could plant a link there could write the file anyway; it must not sit in a directory a
    browser container can write (data/browser-profile, data/browser-sessions)."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(str(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path} is not a regular file")
        if info.st_size > PROFILE_MAX_BYTES:
            raise ValueError(f"{path} is larger than {PROFILE_MAX_BYTES} bytes")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(PROFILE_MAX_BYTES + 1)
    finally:
        os.close(fd)
    profile = json.loads(data.decode("utf-8"))
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
    """'on' with the evidence sandbox.mjs writes, as scripts/assert_browser_sandbox.py checks the evidence (not the
    setting): at least one renderer,
    each in its own user namespace, and either /proc's seccomp filter counts (every renderer above the browser) or
    chrome://sandbox's own report (old kernels). A bare 'on', or renderers alone, is not enough."""
    if not state or state.get("sandbox") != "on":
        return False
    verified = state.get("verified") or {}
    renderers = verified.get("renderers")
    if not (isinstance(renderers, int) and not isinstance(renderers, bool) and renderers >= 1):
        return False
    if verified.get("renderer_userns_differs") is not True:
        return False
    if verified.get("method") == "chrome://sandbox":
        return True
    filters = verified.get("renderer_seccomp_filters")
    browser_filters = verified.get("browser_seccomp_filters")
    return (
        isinstance(filters, list)
        and len(filters) == renderers
        and isinstance(browser_filters, int)
        and not isinstance(browser_filters, bool)
        and browser_filters >= 0
        and all(isinstance(count, int) and count >= 0 and count > browser_filters for count in filters)
    )


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


def describe_state(state: dict[str, Any]) -> str:
    """ "<sandbox> (<cause>): <detail>", without the parts a state does not have."""
    described = str(state.get("sandbox"))
    if state.get("cause"):
        described += f" ({state['cause']})"
    if state.get("detail"):
        described += f": {state['detail']}"
    return described


ENDPOINT_MAX_BYTES = 512


class EndpointFileError(OSError):
    """A Playwright endpoint file that is not one: never carries the file's content (it may not be an endpoint)."""


def read_endpoint_file(path: Path) -> str | None:
    """A browser container's ws endpoint, read like its state file: O_NOFOLLOW|O_NONBLOCK, a regular file of at most
    ENDPOINT_MAX_BYTES, a ws:// or wss:// URL in printable ASCII. None while it is absent or empty. Anything else is
    EndpointFileError, whose message names the path and never the content: Playwright echoes its connect target into
    errors, and a link planted here could otherwise put any file the controller can read (its API token) into the
    log."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        reason = "a symlink, which is refused" if exc.errno == errno.ELOOP else (exc.strerror or "unreadable")
        raise EndpointFileError(f"the endpoint file {path} could not be opened ({reason})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise EndpointFileError(f"the endpoint file {path} is not a regular file")
        if st.st_size > ENDPOINT_MAX_BYTES:
            raise EndpointFileError(f"the endpoint file {path} is larger than {ENDPOINT_MAX_BYTES} bytes")
        data = os.read(fd, ENDPOINT_MAX_BYTES + 1)
    except EndpointFileError:
        raise
    except OSError as exc:
        raise EndpointFileError(f"the endpoint file {path} could not be read ({exc.strerror or 'error'})") from None
    finally:
        os.close(fd)
    try:
        endpoint = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise EndpointFileError(f"the endpoint file {path} is not UTF-8 text") from None
    if not endpoint:
        return None
    # A real endpoint is ws://host:port/<hex>: printable ASCII without whitespace, so no control or format character
    # reaches a log line or an error (S6#2, F6).
    if (
        not endpoint.startswith(("ws://", "wss://"))
        or not endpoint.isascii()
        or not endpoint.isprintable()
        or any(ch.isspace() for ch in endpoint)
    ):
        raise EndpointFileError(f"the endpoint file {path} does not hold a ws:// or wss:// URL")
    return endpoint


def refusal(who: str, state: dict[str, Any] | None) -> str:
    """The error for a session refused under BROWSER_SANDBOX=on (isolated and shared use the same wording)."""
    if state is None:
        described = "no state (an image older than 1.14.0 ignores BROWSER_SANDBOX)"
    elif state.get("sandbox") == "on":
        described = "on without verification evidence"
    elif state.get("sandbox") == "attached":
        described = "a Chrome attached over CDP, which it cannot verify"
    else:
        described = describe_state(state)
    return f"BROWSER_SANDBOX=on, but {who} reports {described}; the session was refused."


def browser_sandbox_status(
    settings: Any, provisioner: Any = None, *, shared: dict[str, Any] | None = None
) -> dict[str, Any]:
    """What readiness reports: the shared browser-node's state and each live isolated container's.

    `shared` is the connect-time snapshot (BrowserRuntimeService.shared_sandbox, None while not connected), or
    {"sandbox": "attached"} for a Chrome attached over CDP. In shared mode, without one: only a refused state file is
    reported (a browser that refused to start sandboxed writes one and no endpoint), an endpoint file is 'unknown'
    until the controller connects, and neither is 'unknown' too (nothing has reported yet).

    In docker_ephemeral mode no session runs on the shared node: its line is read from its state file, as before the
    snapshot, and is informational (browser_sandbox_check never counts it)."""
    endpoint_file = Path(getattr(settings, "browser_ws_endpoint_file", "") or "")
    isolation_mode = getattr(settings, "session_isolation_mode", "shared_browser_node")
    informational = isolation_mode == "docker_ephemeral"
    browser_node: dict[str, Any] | None = shared
    if browser_node is None and getattr(settings, "cdp_connect_url", None) and not informational:
        # The operator's Chrome over CDP, not connected now: still that Chrome, not a browser-node.
        browser_node = {"sandbox": "attached", "detail": "not connected"}
    if browser_node is None and endpoint_file.name:
        state = read_sandbox_state(endpoint_file.with_name(BROWSER_SANDBOX_STATE_FILENAME))
        if informational:
            browser_node = state
        elif endpoint_file.is_file():
            browser_node = {
                "sandbox": "unknown",
                "detail": "browser-node is not connected (not yet, or the connection dropped)",
            }
        elif state is not None and state.get("sandbox") == "refused":
            # No endpoint: a browser-node that refused to start sandboxed. Anything else a state file says without a
            # run to bind it to is not taken (D3): a forged or stale "on" must not pass.
            browser_node = state
        else:
            browser_node = {"sandbox": "unknown", "detail": "browser-node has not reported its sandbox yet"}
    states = getattr(provisioner, "sandbox_states", None)
    supports = getattr(provisioner, "seccomp_support_known", None)
    profile_path = getattr(settings, "isolated_browser_seccomp_profile", str(SECCOMP_PROFILE_PATH))
    return {
        "setting": getattr(settings, "browser_sandbox", "auto"),
        "isolation_mode": isolation_mode,
        "profile_path": str(profile_path),
        "profile_readable": _profile_readable(profile_path),
        # Asked at provisioner startup under docker_ephemeral; None if that failed (or in shared mode).
        "seccomp_supported": supports() if callable(supports) else None,
        "browser_node": browser_node,
        "browser_node_informational": informational,
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
        if self.sandbox == "attached":
            if (self.state or {}).get("detail"):
                return f"{self.label}: attached over CDP, not checked ({self.state['detail']})"
            return f"{self.label}: attached over CDP, not checked"
        if self.sandbox in {"unknown", "unreadable"}:
            detail = (self.state or {}).get("detail") or (
                "no browser-sandbox.json (an image older than 1.14.0, or still starting)"
            )
            return f"{self.label}: sandbox state unknown ({detail})"
        if self.sandbox == "on":
            return f"{self.label}: sandbox on without verification evidence"
        text = f"{self.label}: sandbox {self.sandbox}"
        if self.state.get("cause"):
            text += f" ({self.state['cause']})"
        if self.state.get("detail"):
            text += f": {self.state['detail']}"
        return text


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
    # In docker_ephemeral mode the shared node runs no session: reported in details, never counted.
    if status.get("browser_node") is not None and not status.get("browser_node_informational"):
        node = status["browser_node"]
        lines.append(_Line("attached Chrome" if node.get("sandbox") == "attached" else "browser-node", node))
    for session_id, state in sorted((status.get("isolated") or {}).items()):
        lines.append(_Line(f"session {session_id}", state))
    # Under on with isolation, a daemon whose seccomp support could not be read: not a pass (S4#11).
    seccomp_unknown = (
        status.get("setting") == "on"
        and status.get("isolation_mode") == "docker_ephemeral"
        and status.get("seccomp_supported") is None
    )
    unknown_seccomp_check = ReadinessCheck(
        name=name,
        status="warn",
        message=(
            "BROWSER_SANDBOX=on, but whether the Docker daemon applies seccomp is unknown (docker info failed at "
            "startup): an isolated session will find out."
        ),
        fix="See the controller log for the docker info error.",
        details=details,
    )
    if not lines:
        if seccomp_unknown:
            return unknown_seccomp_check
        return ReadinessCheck(
            name=name,
            status="pass",
            message="No browser container is running yet; each reports its sandbox state when it starts.",
            details=details,
        )
    # A Chrome attached over CDP (CDP_CONNECT_URL, the operator's; browser.cdp_attach is refused under on) is neither
    # verified nor a browser container: it has its own line, a warning, never a failure.
    attached = [line for line in lines if line.sandbox == "attached"]
    containers = [line for line in lines if line.sandbox != "attached"]
    unsandboxed = [line for line in containers if not line.verified]
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
    if unsandboxed:
        return ReadinessCheck(
            name=name,
            status="warn",
            message=(
                f"Chromium runs without its sandbox in {len(unsandboxed)} of {len(containers)} browser container(s): "
                + "; ".join(line.describe() for line in unsandboxed)
                + "".join(f"; {line.describe()}" for line in attached)
            ),
            fix=(
                "See each cause: start browser containers with the seccomp profile (compose and the controller apply "
                "it), or on a host that refuses unprivileged user namespaces accept the fallback; BROWSER_SANDBOX=on "
                "refuses to run unsandboxed."
            ),
            details=details,
        )
    if attached:
        return ReadinessCheck(
            name=name,
            status="warn",
            # From the attached lines, label and detail (S6#3): an unconnected CDP_CONNECT_URL Chrome reads "not
            # connected", never as if the controller were attached to it.
            message=(
                "browser-node is not in use: "
                + "; ".join(line.describe() for line in attached)
                + "."
                + (
                    f" Chromium's sandbox is on and verified in {len(containers)} browser container(s)."
                    if containers
                    else ""
                )
            ),
            fix="An operator's own Chrome (CDP_CONNECT_URL) is its operator's to sandbox.",
            details=details,
        )
    if seccomp_unknown:
        return unknown_seccomp_check
    return ReadinessCheck(
        name=name,
        status="pass",
        message=f"Chromium's sandbox is on and verified in {len(lines)} browser container(s).",
        details=details,
    )

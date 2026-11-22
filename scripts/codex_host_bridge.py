#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from typing import Any

# The environment Codex gets: what the controller gives its own provider CLIs (cli_environment in
# controller/app/providers/base.py: CLI_BASE_ENV_NAMES, CLI_BASE_ENV_PREFIXES, and the OpenAI adapter's
# cli_env_names), plus each --pass-env NAME. Copies, since the bridge runs with the host's Python and cannot import
# the controller; controller/tests/test_codex_host_bridge_env.py keeps them equal. Without it Codex, and every
# command a prompt-injected Codex runs, got the whole environment of the shell the bridge was started from.
CODEX_BASE_ENV_NAMES = frozenset(
    {
        "PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT",
        "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG", "LANGUAGE", "TZ",
        "TERM", "NO_COLOR", "CI", "USER", "LOGNAME", "SHELL",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    }
)  # fmt: skip
CODEX_BASE_ENV_PREFIXES = ("LC_",)
CODEX_PROVIDER_ENV_NAMES = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME", "CODEX_API_KEY", "CODEX_CA_CERTIFICATE")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def codex_environment(pass_env: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    """The allowlisted part of this process's environment, compared as cli_environment compares it (upper-cased)."""
    allowed = CODEX_BASE_ENV_NAMES | {name.upper() for name in CODEX_PROVIDER_ENV_NAMES}
    allowed |= {name.upper() for name in pass_env}
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() in allowed or name.upper().startswith(CODEX_BASE_ENV_PREFIXES)
    }


PR_SET_DUMPABLE = 4


def make_non_dumpable() -> bool:
    """prctl(PR_SET_DUMPABLE, 0) on Linux, as the controller does (controller/app/process_hardening.py).

    The commands Codex runs are processes of the bridge's uid, and proc(5) lets a same-uid process read a dumpable
    process's /proc/PID/environ: the bridge's whole environment, around the allowlisted one Codex is given. The flag
    resets at execve, so Codex itself stays ordinary. True when it took; False, after one warning, when it did not.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(*[ctypes.c_ulong(value) for value in (PR_SET_DUMPABLE, 0, 0, 0, 0)]) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    except (OSError, AttributeError, ImportError) as exc:
        print(
            f"[codex-bridge] warning: could not make the bridge non-dumpable ({exc}); a process running as its uid "
            "can read its environment through /proc",
            file=sys.stderr,
        )
        return False
    return True


SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX_MODE = "read-only"

# A request is a prompt, a schema and at most one base64 viewport screenshot
# (a few MB; a noisy 4K PNG is ~15 MB as base64). Anything larger is not a
# legitimate request, and the body is read into memory before it is parsed.
MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024
# Seconds a client may stall between bytes of a request. Without it one idle or
# slow connection pinned a handler thread forever.
REQUEST_READ_TIMEOUT_SECONDS = 30.0
# Seconds a client may take to deliver the whole body. The per-read timeout above restarts on every byte, so a
# client sending one byte every 29 s would otherwise hold a handler thread for hours.
REQUEST_BODY_DEADLINE_SECONDS = 60.0
# Seconds a client may take to deliver the request line and headers, in total, for the same reason.
REQUEST_HEADER_DEADLINE_SECONDS = 30.0
# Codex runs at once. Each is a heavy host process, and the socket has no other limit on concurrent requests.
MAX_CONCURRENT_DECISIONS = 2
# Seconds a request waits for one of those slots before it is answered 503.
DECISION_SLOT_WAIT_SECONDS = 2.0
# Seconds a killed codex run's pipes are drained, and then its exit waited for, before the run gives up on each: a
# descendant that left codex's process group (setsid) can hold stdout open long after the kill.
CODEX_DRAIN_TIMEOUT_SECONDS = 3.0
# Characters of Codex's output a line of the bridge log holds at most.
LOGGED_OUTPUT_CHARS = 2000
# Connections served at once. Each holds a handler thread, and a timer thread while its headers come in; a connection
# past this is answered 503 and closed at once.
MAX_CONNECTIONS = 32
_BUSY_BODY = b'{"error": "too many connections"}'
_BUSY_REPLY = (
    b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
    + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(_BUSY_BODY)
    + _BUSY_BODY
)


def build_sandbox_flags(*, allow_host_exec: bool, sandbox_mode: str) -> list[str]:
    """Mirror of app/codex_sandbox.py — this script runs outside the container.

    Codex is handed a screenshot and a prompt and asked for a JSON decision; it
    is never meant to run shell commands here, so the default is a read-only
    sandbox and the bypass is an explicit opt-in (GHSA-xmh3-cw7j-9gp5).
    """
    if allow_host_exec:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    mode = (sandbox_mode or DEFAULT_SANDBOX_MODE).strip() or DEFAULT_SANDBOX_MODE
    if mode not in SANDBOX_MODES:
        raise SystemExit(f"CODEX_SANDBOX_MODE must be one of {', '.join(SANDBOX_MODES)} (got {mode!r})")
    return ["-s", mode]


# Same pattern as MODEL_NAME_PATTERN in controller/app/models.py (this script ships without the controller; a test
# keeps the two equal). The leading alphanumeric keeps a model from being read as a codex flag.
MODEL_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$"
_MODEL_NAME_RE = re.compile(MODEL_NAME_PATTERN)


# sockaddr_un.sun_path, terminator included: 104 on macOS and the BSDs, 108 on Linux.
AF_UNIX_PATH_MAX = 104 if sys.platform == "darwin" else 108


class SocketPathError(RuntimeError):
    pass


def prepare_socket_path(raw: str) -> Path:
    """The resolved path to bind, with a stale socket there removed; SocketPathError when it cannot be used, a socket
    another bridge is listening on included.

    Only a socket is ever unlinked: the path used to be resolved and whatever file it named was deleted, so a
    --socket-path that pointed at a regular file or a symlink removed it (or the symlink's target).
    """
    socket_path = Path(raw).resolve()
    if len(os.fsencode(str(socket_path))) >= AF_UNIX_PATH_MAX:
        raise SocketPathError(
            f"socket path is too long for a unix socket ({len(os.fsencode(str(socket_path)))} bytes, "
            f"limit {AF_UNIX_PATH_MAX - 1}): {socket_path}; pass a shorter --socket-path"
        )
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    # Held for the bridge's life, before the stale-socket check: two bridges probing at once could each find the
    # socket refused, and one then removed the socket the other had just bound.
    _hold_socket_lock(socket_path)
    try:
        for candidate in (Path(raw), socket_path):
            try:
                mode = os.lstat(candidate).st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISSOCK(mode):
                raise SocketPathError(f"{candidate} exists and is not a socket; refusing to remove it")
        if socket_path.exists():
            if _socket_is_listening(socket_path):
                raise SocketPathError(
                    f"another bridge is listening on {socket_path}; stop it or pass another --socket-path"
                )
            socket_path.unlink()  # refused: nothing listens, so it is stale
    except BaseException:
        _release_socket_lock(socket_path)
        raise
    return socket_path


# The lock each socket path's bridge holds (path to descriptor), kept open for the process's life: an flock ends
# with its holder, so the lock file itself stays.
_SOCKET_LOCKS: dict[Path, int] = {}


def _hold_socket_lock(socket_path: Path) -> None:
    """Take an exclusive flock on <socket-path>.lock (created 0600); SocketPathError when another bridge holds it.

    Skipped where fcntl is missing (Windows), as the rest of the POSIX-only code is.
    """
    try:
        import fcntl
    except ImportError:
        return
    lock_path = socket_path.with_name(socket_path.name + ".lock")
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        # EACCES for another uid's 0600 lock file, ELOOP for a symlink there.
        raise SocketPathError(
            f"cannot open the lock file {lock_path} ({exc.strerror or exc}); remove it or pass another --socket-path"
        ) from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SocketPathError(
            f"another bridge is running on {socket_path}; stop it or pass another --socket-path"
        ) from None
    except OSError as exc:
        # ENOLCK, or EOPNOTSUPP on a filesystem without locks: as an unopenable lock file, not a traceback.
        os.close(fd)
        raise SocketPathError(
            f"cannot lock {lock_path} ({exc.strerror or exc}); remove it or pass another --socket-path"
        ) from None
    except BaseException:
        os.close(fd)
        raise
    _SOCKET_LOCKS[socket_path] = fd


def _release_socket_lock(socket_path: Path) -> None:
    fd = _SOCKET_LOCKS.pop(socket_path, None)
    if fd is not None:
        os.close(fd)


def _socket_identity(path: Path) -> tuple[int, int] | None:
    """(st_dev, st_ino) of what `path` names, not following a symlink; None when nothing is there."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return None
    return found.st_dev, found.st_ino


def _socket_is_listening(path: Path) -> bool:
    """True when a connection to the socket at `path` is accepted, or times out on a full backlog; a refused or
    missing one means a stale socket. Any other failure (EACCES: another uid's 0600 socket) says nothing about a
    listener, so it is SocketPathError.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(2.0)
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except (TimeoutError, BlockingIOError):
        return True  # a full backlog: something listens there, so it is not ours to remove
    except OSError as exc:
        raise SocketPathError(
            f"cannot tell whether a bridge is listening on {path} ({exc.strerror or exc}); "
            "remove it or pass another --socket-path"
        ) from None
    finally:
        probe.close()
    return True


def model_name_is_valid(model: str) -> bool:
    return bool(_MODEL_NAME_RE.fullmatch(model)) and ".." not in model


CONFIG_OVERRIDES = (
    "project_doc_fallback_filenames=[]",
    "agents={}",
    "features.multi_agent=false",
    "features.apps=false",
    # No command tool: the decision needs none (the prompt goes in on stdin, the screenshot as --image).
    "features.shell_tool=false",
    "features.unified_exec=false",
    'web_search="disabled"',
    # The project root is the call's own new directory, so Codex reads no .codex/config.toml above it.
    "project_root_markers=[]",
    "notify=[]",  # no program run at the turn's end
    "features.codex_hooks=false",  # no hook of any hooks.json
    # No telemetry export: logs, traces, metrics.
    'otel.exporter="none"',
    'otel.trace_exporter="none"',
    'otel.metrics_exporter="none"',
    "project_doc_max_bytes=0",  # no AGENTS.md from the working directory or its parents
    "features.shell_snapshot=false",  # no login shell for a snapshot: it runs the home's .bash_profile and .bashrc
    "features.js_repl=false",  # no js_repl tool, and no Node (js_repl_node_path, else node) run to offer one
    "features.code_mode=false",  # no code_mode tool, and no Node run to offer one
    "features.apply_patch_freeform=false",  # no apply_patch tool from the feature (a model's catalog can bring one)
    "features.artifact=false",  # no artifacts tool
    "features.image_generation=false",  # no image_generation tool
    "features.request_permissions_tool=false",  # no request_permissions tool
)
# CONFIG_OVERRIDES as a Codex command line takes them, each after a -c.
CONFIG_FLAGS = tuple(word for override in CONFIG_OVERRIDES for word in ("-c", override))
MCP_LIST_COMMAND = ("mcp", "list", "--json")
FEATURES_LIST_COMMAND = ("features", "list")
# The one feature the call turns off that the look leaves out: on its own it gives the model no tool and starts nothing
# (it chooses the shape of the command tool that shell_tool brings, and the look refuses shell_tool), and a Codex newer
# than the pinned one lists it on whatever a call says.
FEATURE_NOT_LOOKED_AT = "unified_exec"
# The features the look reads: each the call turns off (a features.<name>=false of CONFIG_OVERRIDES) but that one.
LOOKED_AT_FEATURES = tuple(
    override.removeprefix("features.").removesuffix("=false")
    for override in CONFIG_OVERRIDES
    if override.startswith("features.")
    and override.endswith("=false")
    and override != f"features.{FEATURE_NOT_LOOKED_AT}=false"
)
# The shape of a feature's name in `codex features list`: a newer Codex lists a dotted name.
FEATURE_NAME = re.compile(r"[a-z0-9_.]+")
# What toml_basic_string writes for each character it escapes.
_TOML_ESCAPES = {ord('"'): '\\"', ord("\\"): "\\\\", **{code: "\\u%04x" % code for code in (*range(0x20), 0x7F)}}


def toml_basic_string(text: str) -> str:
    """``text`` as a TOML basic string: quote and backslash escaped, and each control character as a \\u escape."""
    return '"' + text.translate(_TOML_ESCAPES) + '"'


def mcp_servers_off(names: list[str]) -> str:
    """The -c value that turns off each named MCP server: `mcp_servers={"<name>"={enabled=false},...}`.

    Codex merges a -c table into its config key by key, so `mcp_servers={}` turned off none, and the dotted form
    (`mcp_servers.<name>.enabled=false`) makes Codex exit 1 for a name outside [a-zA-Z0-9_-]; one inline table holds
    for every name.
    """
    return "mcp_servers={%s}" % ",".join(toml_basic_string(name) + "={enabled=false}" for name in dict.fromkeys(names))


def listed_mcp_servers(stdout: bytes) -> dict[str, bool] | None:
    """Each server `codex mcp list --json` printed (a JSON list of objects, each with a string "name"), by name, with
    whether it is on; None for anything else.

    A server counts as off only where its "enabled" is the JSON false: true, no "enabled" or any other value counts as
    on. A name listed more than once is on where any of its entries is.
    """
    try:
        listed = json.loads(stdout.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(listed, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in listed
    ):
        return None
    on = {entry["name"] for entry in listed if entry.get("enabled") is not False}
    return {entry["name"]: entry["name"] in on for entry in listed}


def features_on(stdout: bytes) -> list[str] | None:
    """Those of LOOKED_AT_FEATURES that `codex features list` printed as on, in their order; None where what it
    printed is no list of features.

    It counts as a list only where its bytes are UTF-8, it is not blank and each of its non-empty lines is a row: a
    feature's name (one or more of a-z, 0-9, underscore and dot: FEATURE_NAME), its stage's words, then the word true
    or the word false. A feature is on where its row ends in true; of two rows for one name, the true one wins. A
    feature with no row under the call's name for it counts as off (the bridge runs whatever Codex its host has: a
    newer one lists more features, and may drop one).
    """
    try:
        text = stdout.decode("utf-8")
    except ValueError:
        return None
    rows = [line.split() for line in text.splitlines() if line.strip()]
    if not rows or any(
        len(words) < 3 or not FEATURE_NAME.fullmatch(words[0]) or words[-1] not in ("true", "false") for words in rows
    ):
        return None
    on = {words[0] for words in rows if words[-1] != "false"}
    return [name for name in LOOKED_AT_FEATURES if name in on]


def features_on_error(names: list[str]) -> str:
    """What a call the look stops says: the features it found on (the call's own names for them), where a configuration
    can turn one on against the call, and what to do."""
    features, them = ("the feature", "it") if len(names) == 1 else ("the features", "them")
    return (
        f"the Codex configuration turns on {features} {', '.join(names)}, which a decision call needs off: turn {them} "
        f"off where the configuration turns {them} on, such as in a selected profile, in the managed configuration, in "
        "a requirements file, or under an older name in a [features] table"
    )


# The status of a call refused before its exec, for Codex's configuration or a look or list that failed. The
# controller's client retries a 429 or a 5xx and asks this once, as the controller's own path raises the same refusals
# once.
REFUSAL_STATUS = 409

# The refusal of a call whose second server list shows a server on: the names go to the bridge's log alone.
MCP_SERVERS_ON_ERROR = (
    "the Codex configuration keeps on MCP servers that a decision call turns off, so the Codex call was not made; "
    "see the bridge log"
)
BRIDGE_STOPPING_ERROR = "the bridge is stopping"


@dataclass
class BridgeError(RuntimeError):
    message: str
    status_code: int = 500


@dataclass
class _CodexTimedOut(BridgeError):
    """A codex run that did not finish in time (a 504)."""

    status_code: int = 504


class CodexBridgeService:
    def __init__(
        self,
        *,
        codex_path: str,
        default_model: str | None = None,
        request_timeout_seconds: float = 55.0,
        sandbox_mode: str = DEFAULT_SANDBOX_MODE,
        allow_host_exec: bool = False,
        max_concurrent_decisions: int = MAX_CONCURRENT_DECISIONS,
        pass_env: tuple[str, ...] = (),
    ) -> None:
        # Extra environment variable names Codex gets beside the allowlist (--pass-env).
        self.pass_env = tuple(pass_env)
        self._decision_slots = threading.BoundedSemaphore(max(1, max_concurrent_decisions))
        # Codex runs still going, and whether each leads its own process group: kill_in_flight stops them. Codex is
        # started under the lock and the sweep sets _stopping under it, so a run is either swept or never started.
        self._in_flight: dict[subprocess.Popen, bool] = {}
        self._in_flight_lock = threading.Lock()
        self._stopping = False
        self.codex_path = codex_path
        self.default_model = default_model
        self.request_timeout_seconds = request_timeout_seconds
        self.sandbox_flags = build_sandbox_flags(allow_host_exec=allow_host_exec, sandbox_mode=sandbox_mode)
        if allow_host_exec:
            print(
                "WARNING: CODEX_BRIDGE_ALLOW_HOST_EXEC is set. Codex runs with approvals and "
                "sandboxing disabled, so a model decision can execute arbitrary commands on this "
                "host. Only do this inside an environment that is itself sandboxed.",
                file=sys.stderr,
                flush=True,
            )

    def handle_openai_decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._decision_slots.acquire(timeout=DECISION_SLOT_WAIT_SECONDS):
            raise BridgeError("bridge is busy: too many decisions in flight", status_code=503)
        try:
            return self._decide(payload)
        finally:
            self._decision_slots.release()

    def _decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or "").strip()
        schema = payload.get("schema")
        screenshot = payload.get("screenshot")
        if not prompt:
            raise BridgeError("prompt is required", status_code=400)
        if not isinstance(schema, dict):
            raise BridgeError("schema must be a JSON object", status_code=400)
        # Absent for a text-preset observation, which has no screenshot to send.
        if screenshot is not None and not isinstance(screenshot, dict):
            raise BridgeError("screenshot must be an object", status_code=400)

        model = str(payload.get("model") or self.default_model or "").strip() or None
        if model is not None and not model_name_is_valid(model):
            raise BridgeError("model must match " + MODEL_NAME_PATTERN + " and not contain '..'", status_code=400)
        image_bytes: bytes | None = None
        suffix = ".png"
        if screenshot is not None:
            media_type = str(screenshot.get("media_type") or "image/png").strip() or "image/png"
            image_base64 = str(screenshot.get("base64") or "").strip()
            if not image_base64:
                raise BridgeError("screenshot.base64 is required", status_code=400)

            try:
                image_bytes = base64.b64decode(image_base64, validate=True)
            except Exception as exc:  # pragma: no cover - defensive
                raise BridgeError(f"invalid screenshot.base64: {exc}", status_code=400) from exc

            suffix = mimetypes.guess_extension(media_type) or ".png"
        with tempfile.TemporaryDirectory(prefix="auto-browser-codex-") as tempdir:
            temp_root = Path(tempdir)
            schema_path = temp_root / "schema.json"
            output_path = temp_root / "decision.json"
            image_path = temp_root / f"screenshot{suffix}"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            if image_bytes is not None:
                image_path.write_bytes(image_bytes)

            # One deadline for the call: the look, the server lists and codex exec share it, each getting what is left.
            deadline = time.monotonic() + self.request_timeout_seconds
            # The look first: a call it stops has started one process.
            self._look_at_features(tempdir, deadline)
            servers = self._mcp_servers_off(tempdir, deadline)
            command = self._exec_command(
                model=model,
                servers=servers,
                tempdir=tempdir,
                schema_path=schema_path,
                output_path=output_path,
                image_path=image_path,
                image_bytes=image_bytes,
            )

            # In the folder its lists ran in, as Codex resolves a relative CODEX_HOME against the process's folder.
            returncode, stdout_bytes, stderr_bytes = self._run_codex(
                command, prompt.encode("utf-8"), timeout=deadline - time.monotonic(), cwd=tempdir
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            if stderr.strip():
                # Codex's stderr stays on the bridge side; the reply to the controller carries the decision only.
                print(
                    f"[codex-bridge] codex stderr: {stderr.strip()[:LOGGED_OUTPUT_CHARS]}", file=sys.stderr, flush=True
                )
            raw_text = output_path.read_text(encoding="utf-8") if output_path.exists() else stdout
            if returncode != 0:
                # Codex's output can name host paths or a token: it is logged here (stderr above, stdout when stderr
                # is empty), and the controller gets a fixed message. It decides retries on the status code only.
                if not stderr.strip() and stdout.strip():
                    print(
                        f"[codex-bridge] codex stdout: {stdout.strip()[:LOGGED_OUTPUT_CHARS]}",
                        file=sys.stderr,
                        flush=True,
                    )
                elif not stderr.strip():
                    # Nothing to show, but the reply points here: say what happened.
                    print(
                        f"[codex-bridge] codex exited with {returncode} and printed nothing",
                        file=sys.stderr,
                        flush=True,
                    )
                raise BridgeError(f"codex exited with {returncode}; see the bridge log", status_code=502)
            if not raw_text.strip():
                raise BridgeError("codex returned an empty response", status_code=502)

            return {
                "model": model or self.default_model or "",
                "raw_text": raw_text,
            }

    def _exec_command(
        self,
        *,
        model: str | None,
        servers: list[str],
        tempdir: str,
        schema_path: Path,
        output_path: Path,
        image_path: Path,
        image_bytes: bytes | None,
    ) -> list[str]:
        """The codex exec command line of one decision."""
        command = [self.codex_path]
        if model:
            command.extend(["--model", model])
        command.extend(CONFIG_FLAGS)
        if servers:
            command.extend(["-c", mcp_servers_off(servers)])
        command.extend(["exec", "--skip-git-repo-check"])
        command.extend(self.sandbox_flags)
        command.extend(
            [
                "--cd",
                tempdir,
                "--ephemeral",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                *(["--image", str(image_path)] if image_bytes is not None else []),
                "-",
            ]
        )
        return command

    def _look_at_features(self, tempdir: str, deadline: float) -> None:
        """Refuses the call where `codex <the call's -c flags> features list` shows on a feature the call turns off
        (LOOKED_AT_FEATURES): REFUSAL_STATUS, with a message that names those features and says what to do, logged
        here; nothing Codex printed goes into either."""
        command = [self.codex_path, *CONFIG_FLAGS, *FEATURES_LIST_COMMAND]
        on = self._listed(command, tempdir, deadline, features_on, "codex features list")
        if on:
            message = features_on_error(on)
            print(f"[codex-bridge] {message}; the Codex call was not made", file=sys.stderr, flush=True)
            raise BridgeError(message, status_code=REFUSAL_STATUS)

    def _mcp_servers_off(self, tempdir: str, deadline: float) -> list[str]:
        """The MCP servers this call turns off: each that `codex <the call's -c flags> mcp list --json` names, from
        every config layer Codex reads.

        Where that list shows one on, the list runs once more with the table that turns them off (mcp_servers_off), and
        a server it still shows on refuses the call: REFUSAL_STATUS, with a message that names none of them; their names
        are logged here.
        """
        listed = self._mcp_list(tempdir, deadline, [])
        names = list(listed)
        if any(listed.values()):
            relisted = self._mcp_list(tempdir, deadline, names)
            if still_on := [name for name, on in relisted.items() if on]:
                print(
                    "[codex-bridge] codex mcp list shows MCP servers on with the call's table that turns them off, so "
                    f"the Codex call was not made: {json.dumps(still_on)}",
                    file=sys.stderr,
                    flush=True,
                )
                raise BridgeError(MCP_SERVERS_ON_ERROR, status_code=REFUSAL_STATUS)
        return names

    def _mcp_list(self, tempdir: str, deadline: float, servers: list[str]) -> dict[str, bool]:
        """What `codex <the call's -c flags> mcp list --json` lists (listed_mcp_servers), with the table that turns off
        ``servers`` where there are any."""
        table = ["-c", mcp_servers_off(servers)] if servers else []
        command = [self.codex_path, *CONFIG_FLAGS, *table, *MCP_LIST_COMMAND]
        return self._listed(command, tempdir, deadline, listed_mcp_servers, "codex mcp list")

    def _listed(
        self, command: list[str], tempdir: str, deadline: float, read: Callable[[bytes], Any], name: str
    ) -> Any:
        """What ``read`` makes of what ``command`` (one of the lists a call runs before its exec, which ``name`` names)
        printed, run in the call's directory with the call's environment.

        One that exits non-zero (Codex's answer to a config.toml it cannot read or parse, on which exec refuses to run
        too) or prints what ``read`` does not take (None) refuses the call: REFUSAL_STATUS, with its exit code, the
        length of its stdout and its stderr logged here. One that exits non-zero while the bridge is stopping
        (kill_in_flight kills it) is answered 503, as a call that reaches codex after that sweep is. One that does not
        answer by the deadline, or has no time left to start, fails the call with the timed-out error itself, as a
        timed-out exec does (504), logged here with ``name``.

        What a list prints on stdout is in no line logged here: `codex mcp list --json` prints each server's env and
        http_headers values. Of it, only the names of the servers a second list still shows on are logged
        (_mcp_servers_off).
        """
        try:
            returncode, stdout, stderr = self._run_codex(command, b"", timeout=deadline - time.monotonic(), cwd=tempdir)
        except _CodexTimedOut as exc:
            print(f"[codex-bridge] {name}: {exc.message}", file=sys.stderr, flush=True)
            raise
        if returncode != 0 and self._stopping:
            raise BridgeError(BRIDGE_STOPPING_ERROR, status_code=503)
        listed = read(stdout) if returncode == 0 else None
        if listed is None:
            outcome = f"exited with {returncode}" if returncode else "exited with 0 and printed no list"
            errors = stderr.decode("utf-8", errors="replace").strip()
            print(
                f"[codex-bridge] {name} {outcome}: {len(stdout)} bytes on stdout, not logged; stderr: "
                f"{errors[:LOGGED_OUTPUT_CHARS] or '(nothing)'}",
                file=sys.stderr,
                flush=True,
            )
            raise BridgeError(f"{name} failed; see the bridge log", status_code=REFUSAL_STATUS)
        return listed

    def _run_codex(self, command: list[str], prompt: bytes, *, timeout: float, cwd: str) -> tuple[int, bytes, bytes]:
        """Run codex with `prompt` on stdin in the folder `cwd`, for `timeout` seconds; on timeout kill its whole
        process group, not just the direct child, and raise the timed-out error, with nothing codex printed in its
        text, its cause or its context. With no time left (`timeout` <= 0) no codex is started, and the run is a
        timed-out one."""
        grouped = hasattr(os, "killpg")  # POSIX; the bridge listens on a Unix socket, so this is its normal case
        with self._in_flight_lock:
            if self._stopping:
                raise BridgeError(BRIDGE_STOPPING_ERROR, status_code=503)
            if timeout <= 0:
                raise self._timed_out()
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=grouped,
                env=codex_environment(self.pass_env),
                cwd=cwd,
            )
            self._in_flight[process] = grouped
        try:
            stdout, stderr = process.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process, grouped)
            _reap_killed(process)
        except BaseException:
            _kill_process_tree(process, grouped)
            _reap_killed(process)
            raise
        else:
            return process.returncode, stdout, stderr
        finally:
            with self._in_flight_lock:
                self._in_flight.pop(process, None)
        # Only a timed-out run gets here. Raised past its except block: on POSIX the TimeoutExpired holds what codex
        # printed on stdout and stderr, so it must be neither the cause nor the context of the error that goes up.
        raise self._timed_out()

    def _timed_out(self) -> _CodexTimedOut:
        return _CodexTimedOut(f"codex timed out after {self.request_timeout_seconds:.0f}s")

    def kill_in_flight(self) -> int:
        """Kill every codex run still going (its whole process group on POSIX), for bridge shutdown; returns how many
        it signalled.

        Codex leads its own session, so it does not get the terminal's SIGINT when the bridge is stopped by hand, and
        the handler threads that wait on it are daemon threads: without this it would run on, orphaned. From here on
        the service starts no codex: a decision that gets that far is refused with 503.

        A run whose returncode is set just before its signal is skipped: only the handler thread's communicate() sets
        it, by reaping codex, and the pid may belong to another process by then. The sweep never polls: a poll would
        reap a leader that exited while its children still hold its pipes, and the group would go unsignalled. An
        exited leader nobody has reaped stays a zombie, so its pid and process group stay reserved and killpg reaches
        the children. The race with communicate() narrows to the moment between that check and the signal; it is not
        closed.
        """
        with self._in_flight_lock:
            self._stopping = True  # no codex starts after this sweep
            in_flight = list(self._in_flight.items())
        signalled = 0
        for process, grouped in in_flight:
            if process.returncode is not None:
                continue  # reaped by the handler's communicate() since the list was taken
            _kill_process_tree(process, grouped)
            signalled += 1
        return signalled


def _kill_process_tree(process: subprocess.Popen, grouped: bool) -> None:
    try:
        if grouped:
            os.killpg(process.pid, signal.SIGKILL)  # the group codex leads (start_new_session): its children too
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass  # already gone


def _reap_killed(process: subprocess.Popen) -> None:
    """Drain a killed run's pipes, then close them and wait for it, each for at most CODEX_DRAIN_TIMEOUT_SECONDS.

    An unbounded communicate() waited for EOF on stdout, which a descendant that moved to its own session (setsid),
    out of the group the kill reached, could hold open for as long as it lived. This never blocks longer than twice
    the bound; a run still not reaped by then is not waited for again: its group was already killed, and a
    descendant that left the group is the OS's (_run_codex drops the run from _in_flight either way, so the shutdown
    sweep never sees it).
    """
    try:
        process.communicate(timeout=CODEX_DRAIN_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
    try:
        process.wait(timeout=CODEX_DRAIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"[codex-bridge] killed codex run {process.pid} did not exit in time", file=sys.stderr, flush=True)


class UnixHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str, service: CodexBridgeService):
        self.service = service
        self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(socket_path, BridgeRequestHandler)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._connection_slots.acquire(blocking=False):
            _answer_busy(request)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()  # no handler thread started, so none will release it
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


def _answer_busy(request: socket.socket) -> None:
    """The 503 for a connection past the cap, sent without waiting: the serving thread must not block on a client
    that does not read. One that cannot take it gets the close alone."""
    try:
        request.setblocking(False)
        request.send(_BUSY_REPLY)
    except OSError:
        pass


class _HeaderDeadline:
    """One request's header deadline: under its lock, the headers coming in and the deadline passing exclude each
    other. A late timer of an earlier request on the same connection sees its own, settled, state."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.headers_in = False
        self.late = False


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "AutoBrowserCodexHostBridge/0.1"
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_READ_TIMEOUT_SECONDS

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found"})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/openai/decide":
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found"})
            return

        try:
            payload = self._read_json_body()
            response = self.server.service.handle_openai_decide(payload)  # type: ignore[attr-defined]
        except BridgeError as exc:
            self._send_json(exc.status_code, {"error": exc.message})
            return
        except json.JSONDecodeError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid json: {exc}"})
            return
        except UnicodeDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request body must be UTF-8 encoded JSON"})
            return
        except TimeoutError:
            self.close_connection = True
            self._send_json(HTTPStatus.REQUEST_TIMEOUT, {"error": "timed out reading the request body"})
            return
        except Exception:
            # The detail goes to the bridge's log, not to the caller.
            traceback.print_exc(file=sys.stderr)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})
            return

        self._send_json(HTTPStatus.OK, response)

    def handle_one_request(self) -> None:
        # The request line and headers get a total deadline, as the body does. Past it the connection is shut down,
        # which ends the blocked read; parse_request stops the timer once the headers are in.
        self._header_deadline = _HeaderDeadline()
        self._header_timer = threading.Timer(
            REQUEST_HEADER_DEADLINE_SECONDS, self._end_late_headers, args=(self._header_deadline,)
        )
        self._header_timer.daemon = True
        self._header_timer.start()
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # The deadline shut the connection down under a request line or headers still coming in, so the stdlib's
            # answer to what had arrived (a 400) had nowhere to go. A broken connection the deadline did not cause is
            # not this handler's to hide.
            with self._header_deadline.lock:
                late = self._header_deadline.late
            if not late:
                raise
            self.close_connection = True
        finally:
            self._header_timer.cancel()

    def parse_request(self) -> bool:
        try:
            parsed = super().parse_request()
        finally:
            self._header_timer.cancel()
            # cancel() cannot stop a callback that has already started: which came first is settled here.
            with self._header_deadline.lock:
                self._header_deadline.headers_in = True
                late = self._header_deadline.late
        # Headers cut short by the deadline are not a request.
        return parsed and not late

    def _end_late_headers(self, deadline: _HeaderDeadline) -> None:
        with deadline.lock:
            if deadline.headers_in:
                return  # the headers made it: the timer fired as parse_request was stopping it
            deadline.late = True
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already closed

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[codex-bridge] {format % args}", file=sys.stderr)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            # rfile.read(-1) would read until the client closes the connection.
            self.close_connection = True
            raise BridgeError("invalid Content-Length", status_code=400)
        if length > MAX_REQUEST_BODY_BYTES:
            # The body stays unread, so the connection cannot be reused for another request.
            self.close_connection = True
            raise BridgeError(
                f"request body of {length} bytes exceeds the {MAX_REQUEST_BODY_BYTES} byte limit",
                status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        body = self._read_exactly(length)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise BridgeError("request body must be a JSON object", status_code=400)
        return payload

    def _read_exactly(self, length: int) -> bytes:
        """`length` bytes of the body, within REQUEST_BODY_DEADLINE_SECONDS in total (TimeoutError past it)."""
        deadline = time.monotonic() + REQUEST_BODY_DEADLINE_SECONDS
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("request body not delivered in time")
                self.connection.settimeout(min(left, self.timeout))
                chunk = self.rfile.read1(min(remaining, 65536))
                if not chunk:
                    self.close_connection = True
                    raise BridgeError("request body shorter than its Content-Length", status_code=400)
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            # The reply and the next keep-alive request get the full per-read timeout, not what the body left.
            self.connection.settimeout(self.timeout)
        return b"".join(chunks)

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Host-side Codex bridge for Auto Browser")
    parser.add_argument(
        "--socket-path",
        default="data/host-bridge/codex.sock",
        help="Unix socket path to listen on",
    )
    parser.add_argument(
        "--codex-path",
        default="codex",
        help="Path to the host codex CLI",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional default model override",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=55.0,
        help="Kill host codex requests that exceed this timeout",
    )
    parser.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Also pass environment variable NAME to Codex (repeatable). Codex otherwise gets only an allowlist: "
            "CODEX_BASE_ENV_NAMES, any LC_* name and CODEX_PROVIDER_ENV_NAMES, each listed in docs/deployment.md "
            "(Codex host bridge). Its value reaches Codex and every command Codex runs."
        ),
    )
    return parser.parse_args()


def main() -> int:
    make_non_dumpable()  # first, before any Codex run
    args = parse_args()
    for name in args.pass_env:
        if not _ENV_NAME_RE.fullmatch(name) or not name.isascii():
            print(
                f"[codex-bridge] --pass-env {name!r} is not an environment variable name: use letters, digits and "
                "underscores, not starting with a digit",
                file=sys.stderr,
            )
            return 2
    try:
        socket_path = prepare_socket_path(args.socket_path)
    except SocketPathError as exc:
        print(f"[codex-bridge] {exc}", file=sys.stderr)
        return 2

    os.umask(0o077)
    service = CodexBridgeService(
        codex_path=args.codex_path,
        default_model=args.model,
        request_timeout_seconds=args.request_timeout_seconds,
        sandbox_mode=os.environ.get("CODEX_SANDBOX_MODE", DEFAULT_SANDBOX_MODE),
        allow_host_exec=os.environ.get("CODEX_BRIDGE_ALLOW_HOST_EXEC", "").strip().lower()
        in {"1", "true", "yes", "on"},
        pass_env=tuple(args.pass_env),
    )
    server = UnixHTTPServer(str(socket_path), service)
    # What this bridge bound: just before its shutdown removes the socket it checks that the path still names it.
    # That keeps out a socket a later bridge bound (each bridge holds its lock until it exits), but not one a
    # process ignoring the lock puts there between the check and the unlink.
    bound = _socket_identity(socket_path)
    # The umask above already makes it 0600; say so explicitly rather than rely on it.
    os.chmod(socket_path, 0o600)

    def shutdown(_: int, __: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"[codex-bridge] listening on {socket_path}", file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        stopped = service.kill_in_flight()
        if stopped:
            print(f"[codex-bridge] stopped {stopped} codex run(s) still in flight", file=sys.stderr)
        server.server_close()
        if bound is not None and _socket_identity(socket_path) == bound:
            socket_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

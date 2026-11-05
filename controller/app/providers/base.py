from __future__ import annotations

import asyncio
import base64
import email.utils
import json
import logging
import mimetypes
import os
import socket
import stat
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from typing import Any, Iterator

import httpx
from pydantic import ValidationError

from ..config import Settings
from ..models import BROWSER_ACTION_SCHEMA, BrowserActionDecision, ProviderName
from ..result_shaping import compact_session

logger = logging.getLogger(__name__)

# Parse strategies fall through on malformed input only. model_validate* raise
# ValidationError; json.loads / raw_decode raise JSONDecodeError (a ValueError),
# or RecursionError on brackets nested too deep to decode.
# Narrowing to these lets a real bug (AttributeError, TypeError, ...) surface
# instead of being silently swallowed as a parse miss.
_PARSE_ERRORS = (ValidationError, ValueError, RecursionError)
# Longest a provider's Retry-After may hold one retry.
MAX_RETRY_AFTER_SECONDS = 30.0

DEFAULT_PROVIDER_AUTH_MODES = {"api", "cli"}

# What a provider CLI child may inherit from the controller: what any program needs to run, and to reach
# the network through a proxy. Everything else (the controller's own tokens, other providers' keys, Stripe
# and so on) stays out; each adapter adds the few variables its own CLI reads, and
# CLI_PROVIDER_ENV_PASSTHROUGH adds more.
CLI_BASE_ENV_NAMES = frozenset(
    {
        "PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT",
        "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG", "LANGUAGE", "TZ",
        "TERM", "NO_COLOR", "CI", "USER", "LOGNAME", "SHELL",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    }
)  # fmt: skip
CLI_BASE_ENV_PREFIXES = ("LC_",)
# Each CLI_HOME that could not be created, warned about once.
_UNUSABLE_CLI_HOMES: set[str] = set()


# The one error a caller gets when no private CLI home can be used. It names no server path (it can reach agent
# step results and MCP answers); the paths, owners and modes refused are in the log.
PRIVATE_CLI_HOME_ERROR = (
    "provider CLIs have no usable private working directory (see the controller log for why); "
    "set CLI_HOME to a writable directory"
)
# Empty files in the private CLI home. The Gemini CLI loads the first .env it finds in its working directory, then
# its parents, then the user's home: these hold nothing and stop that search before a parent's (the user's home
# is now an ancestor).
_ENV_SENTINELS = (".env", ".gemini/.env")


class _Refused(Exception):
    """A private CLI home candidate that cannot be used, and why (with its path, owner and mode, for the log)."""


def _private_cli_home_candidates() -> Iterator[Path]:
    """Where this user's private CLI home may live, best first. Never the shared system temp dir: the CLIs read
    config from the parents of their working directory too, and anyone can create a file in /tmp.

    On Windows a per-user directory under %LOCALAPPDATA%. On POSIX $XDG_RUNTIME_DIR/auto-browser-cli when that is
    an absolute path to a directory this user owns that no one else can write, then ~/.cache/auto-browser/cli-home.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        yield Path(base) / "auto-browser" / "cli-home"
        return
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    if os.path.isabs(runtime):  # a relative value would resolve against the controller's working directory
        try:
            info = os.lstat(runtime)
        except OSError:
            info = None
        if info is not None and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022:
            yield Path(runtime) / "auto-browser-cli"
    yield Path.home() / ".cache" / "auto-browser" / "cli-home"


def _make_private_dir(path: Path) -> None:
    """Make ``path`` and any missing parent 0o700, then check it on POSIX: the directory must be real (not a
    symlink), this user's and 0o700, and no ancestor up to the first root-owned one may be writable by group or
    others without the sticky bit (whoever can write one could put their own directory in this one's place)."""
    if os.name == "nt":
        path.mkdir(parents=True, exist_ok=True)
        return
    for directory in [*reversed(path.parents), path]:
        if not os.path.lexists(directory):
            try:
                os.mkdir(directory, 0o700)
            except FileExistsError:
                pass
            else:
                os.chmod(directory, 0o700)  # the umask may have removed bits from the mkdir mode
    uid = os.getuid()
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        kind = "a symlink" if stat.S_ISLNK(info.st_mode) else "a directory" if stat.S_ISDIR(info.st_mode) else "a file"
        raise _Refused(
            f"it must be a real directory owned by uid {uid} with mode 0700; found {kind}, mode "
            f"{stat.S_IMODE(info.st_mode):04o}, owner {info.st_uid}"
        )
    for ancestor in Path(os.path.realpath(path)).parents:
        info = os.stat(ancestor)
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise _Refused(
                f"its ancestor {ancestor} is writable by group or others without the sticky bit "
                f"(mode {stat.S_IMODE(info.st_mode):04o}, owner {info.st_uid})"
            )
        if info.st_uid == 0:
            break


def _private_cli_home() -> str:
    """The CLI home when CLI_HOME is unset or cannot be created: the first candidate that can be made and passes
    its checks, made and checked on every call (a temp or cache cleaner may remove it, and another user may put
    something in its place), with its empty .env sentinels. When none can be used (no home, a home that cannot
    hold it, every candidate refused), one RuntimeError that says to set CLI_HOME, with the reasons in the log."""
    reasons: list[str] = []
    try:
        for path in _private_cli_home_candidates():
            try:
                _make_private_dir(path)
                for name in _ENV_SENTINELS:
                    sentinel = path / name
                    sentinel.parent.mkdir(mode=0o700, exist_ok=True)
                    sentinel.touch(mode=0o600, exist_ok=True)
            except (_Refused, OSError) as exc:
                why = str(exc) if isinstance(exc, _Refused) else f"{type(exc).__name__}: {exc}"
                logger.warning("the private CLI home %s cannot be used: %s", path, why)
                reasons.append(f"{path}: {why}")
                continue
            return str(path)
    except (OSError, RuntimeError) as exc:  # Path.home() with no HOME and no passwd entry, for one
        reasons.append(f"{type(exc).__name__}: {exc}")
    logger.error("no private CLI home can be used: %s", "; ".join(reasons) or "no candidate")
    raise RuntimeError(PRIVATE_CLI_HOME_ERROR)


@dataclass
class ProviderDecision:
    provider: ProviderName
    model: str
    decision: BrowserActionDecision
    usage: dict[str, Any] | None = None
    raw_text: str | None = None


@dataclass
class CLIResult:
    command: list[str]
    stdout: str
    stderr: str
    returncode: int


@dataclass
class ProviderAPIError(RuntimeError):
    provider: ProviderName
    message: str
    status_code: int | None = None
    retryable: bool = False
    raw_error: dict[str, Any] | None = None

    def __str__(self) -> str:
        if self.status_code is None:
            return f"{self.provider} provider error: {self.message}"
        return f"{self.provider} provider error ({self.status_code}): {self.message}"


class BaseProviderAdapter(ABC):
    provider: ProviderName
    # Variables this provider's own CLI reads (its key, config directory, endpoint), passed to the child.
    cli_env_names: tuple[str, ...] = ()

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    @abstractmethod
    def default_model(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def configured(self) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def missing_detail(self) -> str:
        raise NotImplementedError

    @property
    def readiness_detail(self) -> str:
        return "configured" if self.configured else self.missing_detail

    @property
    def login_command(self) -> str | None:
        return None

    async def decide(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None = None,
        previous_steps: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
    ) -> ProviderDecision:
        if not self.configured:
            raise RuntimeError(self.missing_detail)
        return await self._decide(
            goal=goal,
            observation=observation,
            context_hints=context_hints,
            previous_steps=previous_steps or [],
            model_override=model_override,
        )

    @abstractmethod
    async def _decide(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
    ) -> ProviderDecision:
        raise NotImplementedError

    async def _post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        max_attempts = max(1, self.settings.model_max_retries + 1)
        async with httpx.AsyncClient(timeout=timeout or self.settings.model_request_timeout_seconds) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    response = await client.post(url, headers=headers, json=payload)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt < max_attempts:
                        await asyncio.sleep(self.settings.model_retry_backoff_seconds * (2 ** (attempt - 1)))
                        continue
                    raise ProviderAPIError(
                        provider=self.provider,
                        message="provider network request failed",
                        status_code=None,
                        retryable=True,
                    ) from exc

                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < max_attempts:
                        await asyncio.sleep(self._retry_delay(attempt, response))
                        continue

                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    payload = self._safe_json(response)
                    raise ProviderAPIError(
                        provider=self.provider,
                        message=self._extract_error_message(payload) or response.text[:300],
                        status_code=response.status_code,
                        retryable=response.status_code == 429 or response.status_code >= 500,
                        raw_error=payload if isinstance(payload, dict) else None,
                    ) from exc

                return response.json()

        raise ProviderAPIError(provider=self.provider, message="request failed without a response")

    def _retry_delay(self, attempt: int, response: httpx.Response) -> float:
        """Backoff before retrying a 429/5xx, honouring the provider's Retry-After.

        The fixed 1s/2s backoff retried a rate limit long before the window the
        provider named, so every retry was spent on another 429 and the step
        failed anyway. Capped so one header cannot park a step for minutes.
        """
        delay = self.settings.model_retry_backoff_seconds * (2 ** (attempt - 1))
        retry_after = (response.headers.get("retry-after") or "").strip()
        if retry_after:
            try:
                seconds = float(retry_after)
            except ValueError:
                try:
                    when = email.utils.parsedate_to_datetime(retry_after)
                    seconds = (when - datetime.now(when.tzinfo or timezone.utc)).total_seconds()
                except (TypeError, ValueError):
                    seconds = 0.0
            if seconds == seconds:  # not NaN
                delay = max(delay, min(seconds, MAX_RETRY_AFTER_SECONDS))
        return delay

    @staticmethod
    def encode_image(path: str) -> tuple[str, str]:
        file_path = Path(path)
        mime_type = mimetypes.guess_type(file_path.name)[0] or "image/png"
        data = base64.b64encode(file_path.read_bytes()).decode("ascii")
        return mime_type, data

    @classmethod
    def encode_screenshot(cls, observation: dict[str, Any]) -> tuple[str, str] | None:
        """The observation's screenshot as (mime, base64), or None when it has none.

        observe(preset="text") returns `screenshot_path: None` (and that is the
        default preset), so a missing screenshot is a normal observation, not an
        error: the adapters send a text-only request instead of crashing.
        """
        path = observation.get("screenshot_path")
        if not path:
            return None
        return cls.encode_image(path)

    @staticmethod
    def compact_observation(observation: dict[str, Any]) -> dict[str, Any]:
        interactables = []
        for item in observation.get("interactables", []):
            interactables.append(
                {
                    "element_id": item.get("element_id"),
                    "label": item.get("label"),
                    "role": item.get("role"),
                    "tag": item.get("tag"),
                    "type": item.get("type"),
                    "disabled": item.get("disabled"),
                    "href": item.get("href"),
                    "bbox": item.get("bbox"),
                    "selector_hint": item.get("selector_hint"),
                }
            )
        return {
            "session": compact_session(observation.get("session")),
            "url": observation.get("url"),
            "title": observation.get("title"),
            "active_element": observation.get("active_element"),
            "text_excerpt": observation.get("text_excerpt"),
            "dom_outline": observation.get("dom_outline"),
            "accessibility_outline": observation.get("accessibility_outline"),
            "ocr": observation.get("ocr"),
            "interactables": interactables,
            "console_messages": observation.get("console_messages", []),
            "page_errors": observation.get("page_errors", []),
            "request_failures": observation.get("request_failures", []),
            "tabs": observation.get("tabs", []),
            "recent_downloads": observation.get("recent_downloads", []),
            "takeover_url": observation.get("takeover_url"),
            # On Chrome's error page for a navigation the pre-request gate stopped: what was stopped and why,
            # or the model sees the error page with no word on it. Absent otherwise, as in the observation.
            **(
                {"blocked_navigation": observation["blocked_navigation"]}
                if observation.get("blocked_navigation")
                else {}
            ),
        }

    def build_text_prompt(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
    ) -> str:
        compact_observation = self.compact_observation(observation)
        prior_steps = previous_steps[-6:]
        return (
            "Choose exactly one next browser action.\n"
            "Rules:\n"
            "- Use only the current observation. element_id values are observation-scoped.\n"
            "- Prefer element_id over selector. Use coordinates only for click when no reliable locator exists.\n"
            "- Never invent URLs, elements, or file paths.\n"
            "- Always set risk_category. Use read for navigate/hover/scroll/wait/reload/go_back/go_forward/done, write for normal click/type/press/select_option, upload for file uploads.\n"
            "- If an action would post/send/publish content, set risk_category=post.\n"
            "- If an action would submit a payment/order, set risk_category=payment.\n"
            "- If an action would change profile/settings/security/billing/account state, set risk_category=account_change.\n"
            "- If an action would delete/remove/cancel/close something, set risk_category=destructive.\n"
            "- If the goal is already complete, return action=done.\n"
            "- If the next step involves login, MFA, CAPTCHA, payments, sending/posting, or you are uncertain, return action=request_human_takeover.\n"
            "- For upload, use only an explicitly provided staged file_path.\n"
            "- Use wait when the page is loading or a result is expected to appear shortly without interacting.\n"
            "- Use reload, go_back, or go_forward only when that browser navigation is clearly the best next move.\n"
            f"Goal:\n{goal}\n\n"
            f"Context hints:\n{context_hints or 'None'}\n\n"
            f"Previous steps (most recent last):\n{json.dumps(prior_steps, ensure_ascii=False)}\n\n"
            f"Current observation:\n{json.dumps(compact_observation, ensure_ascii=False)}"
        )

    @property
    def action_schema(self) -> dict[str, Any]:
        return BROWSER_ACTION_SCHEMA

    @property
    def strict_action_schema(self) -> dict[str, Any]:
        return self.make_strict_json_schema(self.action_schema)

    @classmethod
    def make_strict_json_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        return cls._normalize_json_schema_node(schema)

    @classmethod
    def _normalize_json_schema_node(cls, node: Any) -> Any:
        if isinstance(node, list):
            return [cls._normalize_json_schema_node(item) for item in node]
        if not isinstance(node, dict):
            return node

        normalized: dict[str, Any] = {}
        for key, value in node.items():
            if key == "default":
                continue
            if key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
                normalized[key] = {
                    child_key: cls._normalize_json_schema_node(child_value) for child_key, child_value in value.items()
                }
                continue
            if key in {"items", "anyOf", "allOf", "oneOf", "not", "if", "then", "else"}:
                normalized[key] = cls._normalize_json_schema_node(value)
                continue
            normalized[key] = value

        properties = normalized.get("properties")
        if isinstance(properties, dict):
            normalized["required"] = list(properties.keys())
            normalized.setdefault("additionalProperties", False)

        return normalized

    def build_cli_prompt(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        include_schema: bool = True,
    ) -> str:
        schema_text = ""
        if include_schema:
            schema_text = (
                "Return only a single JSON object that matches this JSON Schema. "
                "Do not wrap it in markdown.\n"
                f"{json.dumps(self.action_schema, ensure_ascii=False)}\n\n"
            )
        return schema_text + self.build_text_prompt(
            goal=goal,
            observation=observation,
            context_hints=context_hints,
            previous_steps=previous_steps,
        )

    def cli_environment(self) -> dict[str, str]:
        allowed = CLI_BASE_ENV_NAMES | {name.upper() for name in self.cli_env_names}
        allowed |= {name.upper() for name in self.settings.cli_provider_env_passthrough_names}
        env = {
            name: value
            for name, value in os.environ.items()
            if name.upper() in allowed or name.upper().startswith(CLI_BASE_ENV_PREFIXES)
        }
        if (self.settings.cli_home or "").strip():
            env["HOME"] = self.effective_cli_home()
        env.setdefault("CI", "1")
        env.setdefault("NO_COLOR", "1")
        return env

    def effective_cli_home(self) -> str:
        """The one CLI home a provider CLI child gets: its working directory, and its HOME when CLI_HOME is set.

        Never the controller's checkout: the Gemini CLI loads a .env from its working directory and parents into its
        environment, so a child started there re-reads the secrets `cli_environment` withheld. CLI_HOME when it
        exists or can be created; otherwise (unset, or a host run with no writable /data) this user's private
        directory (_private_cli_home). That one lies under the user's home, so the home is among its parents, but
        empty .env sentinels in it stop the Gemini CLI's search before a parent's .env. When neither can be used, a
        RuntimeError (PRIVATE_CLI_HOME_ERROR) that says to set CLI_HOME.
        """
        cli_home = (self.settings.cli_home or "").strip()
        if not cli_home:
            return _private_cli_home()
        try:
            Path(cli_home).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            first = cli_home not in _UNUSABLE_CLI_HOMES
            _UNUSABLE_CLI_HOMES.add(cli_home)
            try:
                fallback = _private_cli_home()
            except RuntimeError:
                if first:
                    logger.warning("CLI_HOME %s cannot be created (%s), and no private CLI home either", cli_home, exc)
                raise
            if first:
                logger.warning(
                    "CLI_HOME %s cannot be created (%s); provider CLI children start in, and get as HOME, %s, which "
                    "holds no CLI sign-in. CLI mode needs CLI_HOME set to a writable directory on host runs.",
                    cli_home,
                    exc,
                    fallback,
                )
            return fallback
        return cli_home

    @staticmethod
    def cli_binary_exists(path: str | None) -> bool:
        return bool(path and which(path))

    @staticmethod
    def normalize_auth_mode(raw: str | None) -> str:
        return (raw or "").strip().lower()

    @property
    def supported_auth_modes(self) -> tuple[str, ...]:
        return tuple(sorted(DEFAULT_PROVIDER_AUTH_MODES))

    def auth_mode_supported(self, value: str) -> bool:
        return value in self.supported_auth_modes

    def invalid_auth_mode_detail(self, value: str) -> str:
        value_label = value or "<empty>"
        supported = ", ".join(self.supported_auth_modes)
        return f"{self.provider} auth mode '{value_label}' is invalid; expected one of: {supported}"

    @staticmethod
    def describe_api_readiness(*, api_key: str | None, env_var: str) -> tuple[bool, str]:
        if api_key:
            return True, f"ready via {env_var}"
        return False, f"{env_var} is not configured"

    def describe_cli_readiness(
        self,
        *,
        cli_path: str | None,
        cli_label: str,
        auth_markers: tuple[str, ...],
    ) -> tuple[bool, str]:
        resolved_cli = which(cli_path) if cli_path else None
        if not resolved_cli:
            expected_path = cli_path or cli_label
            return False, (
                f"{self.provider} CLI binary was not found: {expected_path}. The controller image includes "
                "the provider CLIs only when built with INSTALL_AGENT_CLIS=true."
            )

        cli_home = (self.settings.cli_home or "").strip()
        if not cli_home:
            return (
                True,
                f"ready via {cli_label} CLI ({resolved_cli}); CLI_HOME is unset so auth state is delegated to the CLI environment",
            )

        home_path = Path(cli_home)
        if not home_path.exists():
            return False, f"CLI_HOME path does not exist: {home_path}"

        if not auth_markers:
            return True, f"ready via {cli_label} CLI ({resolved_cli}) with CLI_HOME={home_path}"

        matches = [str(home_path / marker) for marker in auth_markers if (home_path / marker).exists()]
        if not matches:
            expected = ", ".join(str(home_path / marker) for marker in auth_markers)
            return (
                False,
                f"No {self.provider} CLI auth state found under {home_path}; expected one of: {expected}. "
                "Run the CLI interactively once with HOME set to CLI_HOME, or use scripts/bootstrap_cli_auth.sh.",
            )

        return True, f"ready via {cli_label} CLI ({resolved_cli}); auth state found at {', '.join(matches)}"

    @staticmethod
    def describe_socket_readiness(*, socket_path: str | None, label: str) -> tuple[bool, str]:
        if not socket_path:
            return False, f"{label} socket path is not configured"
        path = Path(socket_path)
        if not path.exists():
            return False, f"{label} socket does not exist: {path}"
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            return False, f"{label} socket could not be inspected: {exc}"
        if not stat.S_ISSOCK(mode):
            return False, f"{label} socket path is not a Unix socket: {path}"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1.0)
                client.connect(str(path))
                client.sendall(b"GET /healthz HTTP/1.1\r\nHost: host-bridge\r\nConnection: close\r\n\r\n")
                response = client.recv(4096)
        except OSError as exc:
            return False, f"{label} socket is not accepting connections: {exc}"
        status_line = response.splitlines()[0] if response else b""
        if b"200" not in status_line:
            return False, f"{label} socket health check failed: {status_line.decode('utf-8', errors='replace')}"
        return True, f"ready via {label} socket ({path})"

    async def run_cli(
        self,
        *,
        command: list[str],
        input_text: str | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> CLIResult:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env or self.cli_environment(),
            cwd=cwd or self.effective_cli_home(),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input_text.encode("utf-8") if input_text is not None else None),
                timeout=self.settings.model_request_timeout_seconds,
            )
        except asyncio.CancelledError:
            # A cancelled agent job (the operator pressed cancel) left the CLI
            # running to completion in the background, still spending tokens.
            if process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
            raise
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise ProviderAPIError(
                provider=self.provider,
                message=f"CLI command timed out after {self.settings.model_request_timeout_seconds:.0f}s",
                retryable=True,
            ) from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"CLI exited with code {process.returncode}"
            raise ProviderAPIError(
                provider=self.provider,
                message=detail[:1200],
                retryable=False,
            )
        return CLIResult(command=command, stdout=stdout, stderr=stderr, returncode=process.returncode)

    def parse_decision_text(self, text: str) -> BrowserActionDecision:
        text = text.strip()
        if not text:
            raise RuntimeError("provider returned an empty response")

        try:
            return BrowserActionDecision.model_validate_json(text)
        except _PARSE_ERRORS:
            pass

        # The prompt asks for exactly one decision. A reply that holds two different ones (fenced, bare, or both in
        # one wrapper) is ambiguous, and choosing between them by position lets JSON quoted from the page choose, so
        # it fails as an unparseable reply does. The same decision repeated (the model said it twice) is one decision.
        decisions: list[BrowserActionDecision] = []
        try:
            decisions += self._decisions_in(json.loads(text))
        except _PARSE_ERRORS:
            pass
        decisions += self._decisions_in_text(text)
        distinct: list[BrowserActionDecision] = []
        for decision in decisions:
            if decision not in distinct:
                distinct.append(decision)
        if len(distinct) == 1:
            return distinct[0]

        ambiguity = f": its reply holds {len(distinct)} different decisions" if distinct else ""
        raise RuntimeError(f"{self.provider} CLI did not return a valid BrowserActionDecision JSON object{ambiguity}")

    @classmethod
    def _decisions_in_text(cls, text: str) -> list[BrowserActionDecision]:
        """Every decision-shaped JSON object in `text`, fenced or not. An object that holds a decision is read whole."""
        decoder = json.JSONDecoder()
        found: list[BrowserActionDecision] = []
        index = text.find("{")
        while index != -1:
            try:
                # From `index` in place, with an absolute end: no copy of the rest of the text at every brace.
                candidate, end = decoder.raw_decode(text, index)
            except _PARSE_ERRORS:
                index = text.find("{", index + 1)
                continue
            decisions = cls._decisions_in(candidate)
            if not decisions:
                index = text.find("{", index + 1)
                continue
            found += decisions
            index = text.find("{", end)
        return found

    def parse_decision_file(self, path: Path) -> BrowserActionDecision:
        return self.parse_decision_text(path.read_text(encoding="utf-8"))

    @staticmethod
    def _decisions_in(payload: Any) -> list[BrowserActionDecision]:
        """Every decision in one JSON value: the value itself, or each one it holds. Walked with a stack rather than
        recursion, so a deeply nested value cannot raise RecursionError."""
        found: list[BrowserActionDecision] = []
        stack = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                try:
                    found.append(BrowserActionDecision.model_validate(value))
                except _PARSE_ERRORS:
                    stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
        return found

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any] | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _extract_error_message(payload: dict[str, Any] | None) -> str | None:
        if not payload:
            return None
        error = payload.get("error")
        if isinstance(error, dict):
            parts = [
                error.get("message"),
                error.get("type"),
                error.get("status"),
                error.get("code"),
            ]
            text = " | ".join(str(part) for part in parts if part)
            if text:
                return text
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message
        return None

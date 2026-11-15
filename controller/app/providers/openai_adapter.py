from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import secrets
import shutil
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from ..codex_sandbox import codex_sandbox_flags
from ..models import BrowserActionDecision, ResumeCheckpoint
from ..utils import error_text_for_log, quoted_for_log
from .base import (
    CLI_STDOUT_LIMIT_BYTES,
    PAGE_DATA_NOTICE,
    PRIVATE_CLI_HOME_ERROR,
    BaseProviderAdapter,
    ProviderAPIError,
    ProviderDecision,
    call_directory_refused,
)

logger = logging.getLogger(__name__)

# A codex config can name a /home/<user>/.codex home from another machine or container; that one place outside the CLI
# home is worth linking to the real one. Anything else is skipped.
_HOME_ROOT_PARTS = 4  # ("/", "home", "<user>", ".codex")
# How the controller opens what a Codex call left at its decision file's name: on POSIX never through a link at that
# name, never waiting for a writer, and as bytes on Windows too. One code path everywhere: a flag the platform lacks
# is 0.
_DECISION_FILE_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
)


def _decision_file_bytes(
    path: Path | str, opener: Callable[[Path | str, int], int] = os.open
) -> tuple[bytes | None, str]:
    """What is at ``path`` (opened by ``opener``) and "", where it is a regular file of at most CLI_STDOUT_LIMIT_BYTES
    that is itself there; (None, "") where nothing is there; otherwise (None, what is there instead), for the log.

    The call's directory and the CLI home are the CLI's to write, and the controller is not under the CLI sandbox: a
    link there could name a file the CLI cannot open, and a FIFO would hold the event loop until something writes to
    it.
    """
    try:
        fd = opener(path, _DECISION_FILE_FLAGS)
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None, "a link"
        return None, "something that cannot be opened (%s)" % errno.errorcode.get(exc.errno or 0, exc.errno)
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISFIFO(mode):
            return None, "a FIFO"
        if stat.S_ISDIR(mode):
            return None, "a directory"
        if not stat.S_ISREG(mode):
            return None, "no regular file"
        chunks: list[bytes] = []
        size = 0
        while size <= CLI_STDOUT_LIMIT_BYTES and (chunk := os.read(fd, CLI_STDOUT_LIMIT_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > CLI_STDOUT_LIMIT_BYTES:
        return None, "larger than %d bytes" % CLI_STDOUT_LIMIT_BYTES
    return b"".join(chunks), ""


def _codex_root_allowed(expected: PurePosixPath, cli_home: PurePosixPath) -> bool:
    if ".." in expected.parts:
        return False
    if cli_home in expected.parents:
        return True
    return len(expected.parts) == _HOME_ROOT_PARTS and expected.parts[:2] == ("/", "home")


# Where the OS opens, makes and links relative to an open directory, and removes a tree that way (not Windows), what
# the controller makes in the CLI home (a Codex call's directory, a root the codex config names) is made from the
# descriptor of the directory it goes in; elsewhere by its path, as before.
_BY_DESCRIPTOR = {os.open, os.mkdir, os.symlink} <= os.supports_dir_fd and shutil.rmtree.avoids_symlink_attacks
# The home is only made in and opened from, which O_PATH (Linux) allows without the right to list it: a home the
# controller may write and enter but not list takes a call's directory, as it did when that was made by path.
_HOME_FLAGS = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_DIRECTORY", 0)
_PART_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _link_in_cli_home(cli_home: str, parts: tuple[str, ...], target: Path) -> str:
    """Make the directories ``parts[:-1]`` in ``cli_home`` and, in the last of them, the link ``parts[-1]`` to
    ``target``; "" when it is made or something is already at its name, else why not (OSError and ValueError raise).

    The CLI writes its home, and the controller is not under the CLI sandbox: a part the CLI made a link, or put back
    as one after the controller made it, would have the controller make directories where the CLI cannot. Each part is
    opened from the one before it and never through a link, and what is made is made in the directory opened."""
    fd = os.open(cli_home, _HOME_FLAGS)
    try:
        for name in parts[:-1]:
            try:
                os.mkdir(name, dir_fd=fd)
            except FileExistsError:
                pass
            try:
                inner = os.open(name, _PART_FLAGS, dir_fd=fd)
            except OSError as exc:
                # Linux answers a link opened O_DIRECTORY | O_NOFOLLOW with ENOTDIR, as it does a file.
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    return f"its part {quoted_for_log(name)} is a link or not a directory"
                raise
            os.close(fd)
            fd = inner
        try:
            os.symlink(target, parts[-1], target_is_directory=True, dir_fd=fd)
        except FileExistsError:
            pass
        return ""
    finally:
        os.close(fd)


class _CallDirectory:
    """A Codex call's working directory in the CLI home, made at once as tempfile.TemporaryDirectory makes one:
    ``name`` is its path, which the CLI is given, and ``with`` gives the name and removes the directory after. What
    the controller makes and reads in it, it opens with ``open``.

    A CLI working in the home meanwhile (another call's) can move this directory aside and leave a link at its name,
    to a place the CLI sandbox keeps it out of, and the controller is not under the sandbox. So on POSIX the directory
    is made from a descriptor of the home, ``open`` opens from a descriptor of the directory made (wherever it has been
    moved to, never through what is at its name), and it is removed without following a link. Windows has no such
    descriptors: there it is made, opened and removed by its path, as before.
    """

    def __init__(self, home: str) -> None:
        if not _BY_DESCRIPTOR:
            self._by_path: tempfile.TemporaryDirectory[str] | None = tempfile.TemporaryDirectory(dir=home)
            self.name = self._by_path.name
            return
        self._by_path = None
        self._home = os.open(home, _HOME_FLAGS)
        self._entry = f"tmp{secrets.token_hex(8)}"
        try:
            os.mkdir(self._entry, 0o700, dir_fd=self._home)
            try:
                self._fd = os.open(self._entry, _PART_FLAGS, dir_fd=self._home)
            except BaseException:
                self._remove(os.rmdir)  # still empty: rmdir refuses a link a CLI left at its name
                raise
        except BaseException:
            os.close(self._home)
            raise
        self.name = os.path.join(home, self._entry)

    def open(self, name: Path | str, flags: int) -> int:
        """os.open of ``name`` in this directory, with the mode open() gives a file it makes: an ``opener`` for it."""
        if self._by_path is not None:
            return os.open(os.path.join(self.name, name), flags, 0o666)
        return os.open(name, flags, 0o666, dir_fd=self._fd)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *_: object) -> None:
        if self._by_path is not None:
            self._by_path.cleanup()
            return
        os.close(self._fd)
        try:
            self._remove(shutil.rmtree)
        finally:
            os.close(self._home)

    def _remove(self, remover: Callable[..., None]) -> None:
        try:
            remover(self._entry, dir_fd=self._home)
        except OSError as exc:
            # A link left at its name, which rmtree and rmdir refuse, or what a CLI left there that cannot be removed:
            # the call's outcome stands.
            logger.warning("a Codex call's directory in the CLI home was not removed: %s", error_text_for_log(exc))


try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib


class OpenAIAdapter(BaseProviderAdapter):
    provider = "openai"
    # CODEX_CA_CERTIFICATE: the PEM bundle Codex reads before SSL_CERT_FILE (a TLS proxy or a private root CA).
    cli_env_names = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME", "CODEX_API_KEY", "CODEX_CA_CERTIFICATE")

    @property
    def supported_auth_modes(self) -> tuple[str, ...]:
        return ("api", "cli", "host_bridge")

    @property
    def default_model(self) -> str:
        if self.auth_mode in {"cli", "host_bridge"}:
            return self.settings.openai_cli_model or "codex-default"
        return self.settings.openai_model

    @property
    def configured(self) -> bool:
        ready, _ = self._readiness()
        return ready

    @property
    def missing_detail(self) -> str:
        return self.readiness_detail

    @property
    def readiness_detail(self) -> str:
        _, detail = self._readiness()
        return detail

    @property
    def auth_mode(self) -> str:
        return self.normalize_auth_mode(self.settings.openai_auth_mode)

    @property
    def login_command(self) -> str | None:
        if self.auth_mode in {"cli", "host_bridge"}:
            return self.settings.openai_cli_path or "codex"
        return None

    def _readiness(self) -> tuple[bool, str]:
        if not self.auth_mode_supported(self.auth_mode):
            return False, self.invalid_auth_mode_detail(self.auth_mode)
        if self.auth_mode == "host_bridge":
            return self.describe_socket_readiness(
                socket_path=self.settings.openai_host_bridge_socket,
                label="OpenAI host bridge",
            )
        if self.auth_mode == "cli":
            ready, detail = self.describe_cli_readiness(
                cli_path=self.settings.openai_cli_path,
                cli_label="codex",
                auth_markers=(".codex",),
            )
            cli_home = (self.settings.cli_home or "").strip()
            if ready and cli_home and (refused := call_directory_refused(cli_home)):
                return False, (
                    f"codex cannot make its call directory under CLI_HOME {cli_home} ({refused}): give the controller "
                    "a CLI_HOME it can write (docker-compose.host-subscriptions.yml mounts a tmpfs there since 1.18.1)"
                )
            return ready, detail
        return self.describe_api_readiness(api_key=self.settings.openai_api_key, env_var="OPENAI_API_KEY")

    async def _decide(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
        resume_checkpoints: list[ResumeCheckpoint] | None = None,
    ) -> ProviderDecision:
        if self.auth_mode == "host_bridge":
            return await self._decide_via_host_bridge(
                goal=goal,
                observation=observation,
                context_hints=context_hints,
                previous_steps=previous_steps,
                resume_checkpoints=resume_checkpoints,
                model_override=model_override,
            )
        if self.auth_mode == "cli":
            return await self._decide_via_cli(
                goal=goal,
                observation=observation,
                context_hints=context_hints,
                previous_steps=previous_steps,
                resume_checkpoints=resume_checkpoints,
                model_override=model_override,
            )

        model = model_override or self.settings.openai_model
        image = self.encode_screenshot(observation)
        # The Responses API, not chat completions: current OpenAI models
        # (gpt-6.1-sol) refuse function tools on chat completions while they
        # reason, and cannot turn reasoning off. The request also used to send
        # the non-strict schema with strict: true and temperature 0, each of
        # which OpenAI rejected with a 400 — every step failed.
        payload = {
            "model": model,
            "instructions": (
                f"You are the Auto Browser planner. Pick exactly one next action. {PAGE_DATA_NOTICE} "
                "Use the provided function tool for your answer."
            ),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": self.build_text_prompt(
                                goal=goal,
                                observation=observation,
                                context_hints=context_hints,
                                previous_steps=previous_steps,
                                resume_checkpoints=resume_checkpoints,
                            ),
                        },
                        *(
                            [{"type": "input_image", "image_url": f"data:{image[0]};base64,{image[1]}"}]
                            if image
                            else []
                        ),
                    ],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "browser_action",
                    "description": "Select the single best next browser action.",
                    "parameters": self.strict_action_schema,
                    "strict": True,
                }
            ],
            "tool_choice": {"type": "function", "name": "browser_action"},
        }
        response = await self._post_json(
            url=f"{self.settings.openai_base_url.rstrip('/')}/responses",
            headers={
                "Authorization": f"Bearer {self.settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )
        call = next(
            (item for item in response.get("output") or [] if item.get("type") == "function_call"),
            None,
        )
        if call is None:
            raise RuntimeError(
                f"OpenAI did not return a browser_action function call (status={response.get('status')})"
            )
        arguments = call.get("arguments") or "{}"
        decision = BrowserActionDecision.model_validate_json(arguments)
        return ProviderDecision(
            provider=self.provider,
            model=response.get("model", model),
            decision=decision,
            usage=response.get("usage"),
            raw_text=arguments,
        )

    async def _decide_via_cli(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
        resume_checkpoints: list[ResumeCheckpoint] | None = None,
    ) -> ProviderDecision:
        model = model_override or self.settings.openai_cli_model
        prompt = self.build_cli_prompt(
            goal=goal,
            observation=observation,
            context_hints=context_hints,
            previous_steps=previous_steps,
            resume_checkpoints=resume_checkpoints,
            include_schema=False,
        )
        directory = self._call_directory()
        with directory as tempdir:
            temp_root = Path(tempdir)
            schema_path = temp_root / "browser_action_schema.json"
            output_path = temp_root / "decision.json"
            try:
                # Made, never written through: the CLI of a call that runs at the same time can write in this
                # directory, and what it put at this name could be a link to a file of its choosing.
                with open(schema_path.name, "x", encoding="utf-8", opener=directory.open) as schema:
                    schema.write(json.dumps(self.strict_action_schema, ensure_ascii=False))
            except FileExistsError:
                logger.warning("something was at the schema file's name in a new Codex call's directory: no call made")
                raise RuntimeError("the Codex call's working directory was changed before the call") from None
            self._ensure_codex_config_path_compatibility()
            config_overrides = [
                "project_doc_fallback_filenames=[]",
                "agents={}",
                "mcp_servers={}",
                "features.multi_agent=false",
                "features.apps=false",
                'web_search="disabled"',
            ]

            command = [self.settings.openai_cli_path]
            if model:
                command.extend(["--model", model])
            for override in config_overrides:
                command.extend(["-c", override])
            command.extend(["exec", "--skip-git-repo-check"])
            command.extend(codex_sandbox_flags(self.settings))
            command.extend(
                [
                    "--cd",
                    tempdir,
                    "--ephemeral",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    *(["--image", observation["screenshot_path"]] if observation.get("screenshot_path") else []),
                    "-",
                ]
            )

            result = await self.run_cli(
                command=command, input_text=prompt, cwd=tempdir, image_path=observation.get("screenshot_path")
            )
            written, instead = _decision_file_bytes(output_path.name, directory.open)
            if instead:
                logger.warning("the Codex call's decision file is %s, and is not read", instead)
                # As a reply that cannot be parsed fails, naming no path and nothing of what is there.
                raise RuntimeError(f"{self.provider} CLI did not return a valid BrowserActionDecision JSON object")
            raw_text = result.stdout if written is None else written.decode("utf-8")
            decision = self.parse_decision_text(raw_text)
            return ProviderDecision(
                provider=self.provider,
                model=model or self.default_model,
                decision=decision,
                usage={"auth_mode": "cli", "transport": "codex-exec"},
                raw_text=raw_text,
            )

    def _call_directory(self) -> _CallDirectory:
        """The directory one Codex call works in, under the CLI home and not the shared system temp dir, whose other
        users could plant config in a parent.

        One error that names no path when it cannot be made, the one the CLI home's own failures raise. A step that
        fails on it says only orchestrator.STEP_FAILED_ERROR: what this changes is the log, where the path and the
        cause get a line of their own.
        """
        home = self.effective_cli_home()
        try:
            return _CallDirectory(home)
        except OSError as exc:
            logger.error(
                "could not make a working directory under the CLI home %s: %s: %s",
                quoted_for_log(home),
                type(exc).__name__,
                error_text_for_log(exc),
            )
            raise RuntimeError(PRIVATE_CLI_HOME_ERROR) from None

    async def _decide_via_host_bridge(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
        resume_checkpoints: list[ResumeCheckpoint] | None = None,
    ) -> ProviderDecision:
        model = model_override or self.settings.openai_cli_model
        prompt = self.build_cli_prompt(
            goal=goal,
            observation=observation,
            context_hints=context_hints,
            previous_steps=previous_steps,
            resume_checkpoints=resume_checkpoints,
            include_schema=False,
        )
        image = self.encode_screenshot(observation)
        bridge_request: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "schema": self.strict_action_schema,
        }
        if image:
            bridge_request["screenshot"] = {"media_type": image[0], "base64": image[1]}
        response = await self._post_host_bridge_request(bridge_request)
        raw_text = str(response.get("raw_text") or "").strip()
        if not raw_text:
            raise RuntimeError("OpenAI host bridge returned an empty response")
        decision = self.parse_decision_text(raw_text)
        return ProviderDecision(
            provider=self.provider,
            model=str(response.get("model") or model or self.default_model),
            decision=decision,
            usage={"auth_mode": "host_bridge", "transport": "codex-host-bridge"},
            raw_text=raw_text,
        )

    async def _post_host_bridge_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        socket_path = self.settings.openai_host_bridge_socket
        max_attempts = max(1, self.settings.model_max_retries + 1)
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://host-bridge",
            timeout=self.settings.model_request_timeout_seconds,
        ) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    response = await client.post("/openai/decide", json=payload)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt < max_attempts:
                        await asyncio.sleep(self.settings.model_retry_backoff_seconds * (2 ** (attempt - 1)))
                        continue
                    raise ProviderAPIError(
                        provider=self.provider,
                        message="host bridge network request failed",
                        status_code=None,
                        retryable=True,
                    ) from exc

                try:
                    body = response.json()
                except Exception:
                    body = None

                retryable_status = response.status_code == 429 or response.status_code >= 500
                if response.status_code >= 400:
                    if retryable_status and attempt < max_attempts:
                        await asyncio.sleep(self.settings.model_retry_backoff_seconds * (2 ** (attempt - 1)))
                        continue
                    detail = None
                    if isinstance(body, dict):
                        error = body.get("error")
                        if isinstance(error, str) and error.strip():
                            detail = error
                        elif isinstance(error, dict):
                            detail = error.get("message") or error.get("detail")
                        if detail is None:
                            detail = body.get("detail")
                    raise ProviderAPIError(
                        provider=self.provider,
                        message=str(detail or response.text[:1200] or "host bridge request failed"),
                        status_code=response.status_code,
                        retryable=retryable_status,
                        raw_error=body if isinstance(body, dict) else None,
                    )

                if not isinstance(body, dict):
                    raise RuntimeError("OpenAI host bridge returned a non-object response")
                return body

        raise ProviderAPIError(
            provider=self.provider,
            message="host bridge request failed without a response",
            retryable=True,
        )

    def _ensure_codex_config_path_compatibility(self) -> None:
        if getattr(self, "_codex_config_checked", False):
            return
        cli_home = (self.settings.cli_home or "").strip()
        if not cli_home:
            return

        actual_codex_home = Path(cli_home) / ".codex"
        # The CLI writes its home: config.toml is read as a call's decision.json is (a FIFO there would hold the event
        # loop this runs on), and what is there instead is said once.
        written, instead = _decision_file_bytes(actual_codex_home / "config.toml")
        if instead:
            self._codex_config_checked = True
            logger.warning("codex's config.toml in the CLI home is %s, and is not read", instead)
            return
        if written is None:
            return

        try:
            config = tomllib.loads(written.decode("utf-8"))
        except Exception:
            return

        # Once per adapter: the config is read and the links made on the first call that finds it.
        self._codex_config_checked = True
        cli_home_posix = PurePosixPath(Path(cli_home).as_posix())
        for expected_codex_home in sorted(self._extract_codex_roots(config)):
            if expected_codex_home == actual_codex_home:
                continue
            expected = PurePosixPath(expected_codex_home.as_posix())
            # The place is checked before anything touches the path: stat() on /root/.codex raises PermissionError
            # for a non-root controller on Python 3.11 and 3.12, and that used to end the loop for every later root.
            if not _codex_root_allowed(expected, cli_home_posix):
                logger.warning(
                    "codex config names %s; not linking it to the CLI home (only the CLI home and /home/<user>/.codex "
                    "are linked)",
                    expected_codex_home.as_posix(),
                )
                continue
            if _BY_DESCRIPTOR and cli_home_posix in expected.parents:
                try:
                    refused = _link_in_cli_home(cli_home, expected.relative_to(cli_home_posix).parts, actual_codex_home)
                except (OSError, ValueError) as exc:
                    refused = f"the link could not be made ({type(exc).__name__})"
                if refused:
                    logger.warning(
                        "codex config names %s; not linking it to the CLI home: %s",
                        quoted_for_log(expected.as_posix()),
                        refused,
                    )
                continue
            try:
                if expected_codex_home.exists():
                    continue
            except (OSError, ValueError) as exc:
                logger.warning(
                    "codex config names %s; not linking it to the CLI home: it cannot be checked (%s)",
                    expected_codex_home.as_posix(),
                    type(exc).__name__,
                )
                continue
            try:
                expected_codex_home.parent.mkdir(parents=True, exist_ok=True)
                expected_codex_home.symlink_to(actual_codex_home, target_is_directory=True)
            except FileExistsError:
                continue
            except (OSError, ValueError) as exc:
                # ValueError: a path the OS cannot take (a NUL byte), which exists() answers False for.
                logger.warning(
                    "codex config names %s; not linking it to the CLI home: the link could not be made (%s)",
                    expected_codex_home.as_posix(),
                    type(exc).__name__,
                )
                continue

    @classmethod
    def _extract_codex_roots(cls, payload: Any) -> set[Path]:
        roots: set[Path] = set()
        if isinstance(payload, dict):
            for value in payload.values():
                roots.update(cls._extract_codex_roots(value))
            return roots
        if isinstance(payload, list):
            for value in payload:
                roots.update(cls._extract_codex_roots(value))
            return roots
        if isinstance(payload, str) and payload.startswith("/") and "/.codex/" in payload:
            prefix, _ = payload.split("/.codex/", 1)
            roots.add(Path(f"{prefix}/.codex"))
        return roots

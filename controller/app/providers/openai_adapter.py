from __future__ import annotations

import asyncio
import json
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import httpx

from ..models import BrowserActionDecision
from .base import BaseProviderAdapter, ProviderAPIError, ProviderDecision


class OpenAIAdapter(BaseProviderAdapter):
    provider = "openai"

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
            return self.describe_cli_readiness(
                cli_path=self.settings.openai_cli_path,
                cli_label="codex",
                auth_markers=(".codex",),
            )
        return self.describe_api_readiness(api_key=self.settings.openai_api_key, env_var="OPENAI_API_KEY")

    async def _decide(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
    ) -> ProviderDecision:
        if self.auth_mode == "host_bridge":
            return await self._decide_via_host_bridge(
                goal=goal,
                observation=observation,
                context_hints=context_hints,
                previous_steps=previous_steps,
                model_override=model_override,
            )
        if self.auth_mode == "cli":
            return await self._decide_via_cli(
                goal=goal,
                observation=observation,
                context_hints=context_hints,
                previous_steps=previous_steps,
                model_override=model_override,
            )

        model = model_override or self.settings.openai_model
        mime_type, image_b64 = self.encode_image(observation["screenshot_path"])
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the Auto Browser planner. Pick exactly one next action. "
                        "Use the provided function tool for your answer."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": self.build_text_prompt(
                                goal=goal,
                                observation=observation,
                                context_hints=context_hints,
                                previous_steps=previous_steps,
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_b64}",
                            },
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "browser_action",
                        "description": "Select the single best next browser action.",
                        "parameters": self.action_schema,
                        "strict": True,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "browser_action"}},
        }
        response = await self._post_json(
            url=f"{self.settings.openai_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )
        choice = response["choices"][0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            raise RuntimeError("OpenAI did not return a tool call for browser_action")
        arguments = tool_calls[0]["function"]["arguments"]
        decision = BrowserActionDecision.model_validate_json(arguments)
        usage = response.get("usage")
        return ProviderDecision(
            provider=self.provider,
            model=response.get("model", model),
            decision=decision,
            usage=usage,
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
    ) -> ProviderDecision:
        model = model_override or self.settings.openai_cli_model
        prompt = self.build_cli_prompt(
            goal=goal,
            observation=observation,
            context_hints=context_hints,
            previous_steps=previous_steps,
            include_schema=False,
        )
        with tempfile.TemporaryDirectory() as tempdir:
            temp_root = Path(tempdir)
            schema_path = temp_root / "browser_action_schema.json"
            output_path = temp_root / "decision.json"
            schema_path.write_text(json.dumps(self.strict_action_schema, ensure_ascii=False), encoding="utf-8")
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
            command.extend(
                [
                    "exec",
                    "--skip-git-repo-check",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--cd",
                    tempdir,
                    "--ephemeral",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "--image",
                    observation["screenshot_path"],
                    "-",
                ]
            )

            result = await self.run_cli(command=command, input_text=prompt, cwd=tempdir)
            raw_text = output_path.read_text(encoding="utf-8") if output_path.exists() else result.stdout
            decision = self.parse_decision_text(raw_text)
            return ProviderDecision(
                provider=self.provider,
                model=model or self.default_model,
                decision=decision,
                usage={"auth_mode": "cli", "transport": "codex-exec"},
                raw_text=raw_text,
            )

    async def _decide_via_host_bridge(
        self,
        *,
        goal: str,
        observation: dict[str, Any],
        context_hints: str | None,
        previous_steps: list[dict[str, Any]],
        model_override: str | None,
    ) -> ProviderDecision:
        model = model_override or self.settings.openai_cli_model
        prompt = self.build_cli_prompt(
            goal=goal,
            observation=observation,
            context_hints=context_hints,
            previous_steps=previous_steps,
            include_schema=False,
        )
        mime_type, image_b64 = self.encode_image(observation["screenshot_path"])
        response = await self._post_host_bridge_request(
            {
                "model": model,
                "prompt": prompt,
                "schema": self.strict_action_schema,
                "screenshot": {
                    "media_type": mime_type,
                    "base64": image_b64,
                },
            }
        )
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
                        await asyncio.sleep(
                            self.settings.model_retry_backoff_seconds * (2 ** (attempt - 1))
                        )
                        continue
                    raise ProviderAPIError(
                        provider=self.provider,
                        message=str(exc),
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
                        await asyncio.sleep(
                            self.settings.model_retry_backoff_seconds * (2 ** (attempt - 1))
                        )
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
        cli_home = (self.settings.cli_home or "").strip()
        if not cli_home:
            return

        actual_codex_home = Path(cli_home) / ".codex"
        config_path = actual_codex_home / "config.toml"
        if not config_path.exists():
            return

        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return

        for expected_codex_home in self._extract_codex_roots(config):
            if expected_codex_home == actual_codex_home or expected_codex_home.exists():
                continue
            try:
                expected_codex_home.parent.mkdir(parents=True, exist_ok=True)
                expected_codex_home.symlink_to(actual_codex_home, target_is_directory=True)
            except FileExistsError:
                continue
            except OSError:
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

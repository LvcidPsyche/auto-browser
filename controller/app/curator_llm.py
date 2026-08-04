"""
curator_llm.py — Minimal text-completion adapter for the Skills Curator sub-agent.

Self-contained; supports Claude, OpenAI, and Gemini API endpoints via `httpx`.
Returns text strings (not BrowserActionDecision) so the main orchestrator
decision pipeline is untouched.

Ships with graceful degradation:
  - API key present → full synthesis mode
  - CLI-only auth (no API key) → `None` adapter, curator falls back to
    raw-skill passthrough. Functional, just no pre-synthesis.

Carried into the current release line from an earlier staging branch.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


_CLAUDE_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_CLAUDE_DEFAULT_MODEL = "claude-opus-4-7"
_OPENAI_DEFAULT_MODEL = "gpt-4o"
_GEMINI_DEFAULT_MODEL = "gemini-1.5-pro"

_DEFAULT_MAX_TOKENS = 1024
_DEFAULT_TIMEOUT = 30.0


class CuratorLLMAdapter:
    """
    Provider-agnostic text completion adapter for the Skills Curator.

    Usage:
        adapter = CuratorLLMAdapter.from_env()  # auto-picks provider
        if adapter.ready:
            text = await adapter.complete("Summarize: ...")
        else:
            # CLI-only install — fall back to raw skill passthrough
            ...
    """

    def __init__(
        self,
        provider: str,
        *,
        model: Optional[str] = None,
        api_key_env: Optional[str] = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        provider_norm = provider.lower().strip()
        if provider_norm not in {"claude", "openai", "gemini"}:
            raise ValueError(f"Unsupported curator provider: {provider!r}")
        self.provider = provider_norm
        self.model = model or self._default_model()
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._api_key_env_override = api_key_env

    # ── Construction helpers ──────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> Optional["CuratorLLMAdapter"]:
        """
        Build an adapter from env config. Returns None when no API key is
        available for the requested provider (graceful degraded mode).
        """
        provider = os.environ.get("CURATOR_PROVIDER", "claude").lower().strip()
        model = os.environ.get("CURATOR_MODEL") or None
        try:
            adapter = cls(provider, model=model)
        except ValueError as exc:
            logger.warning("curator: invalid provider %r — %s", provider, exc)
            return None
        if not adapter.ready:
            logger.info(
                "curator: no API key configured for provider=%s — degraded mode",
                adapter.provider,
            )
            return None
        return adapter

    # ── Introspection ─────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """True when an API key is present for the configured provider."""
        return bool(os.environ.get(self._api_key_env()))

    def _default_model(self) -> str:
        return {
            "claude": _CLAUDE_DEFAULT_MODEL,
            "openai": _OPENAI_DEFAULT_MODEL,
            "gemini": _GEMINI_DEFAULT_MODEL,
        }[self.provider]

    def _api_key_env(self) -> str:
        if self._api_key_env_override:
            return self._api_key_env_override
        return {
            "claude": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY",
        }[self.provider]

    # ── Completion ────────────────────────────────────────────────────────

    async def complete(self, prompt: str, *, system: Optional[str] = None) -> str:
        """
        Synchronous-feel wrapper around provider-specific chat completion.
        Raises RuntimeError if no API key is available; callers should guard
        with `if adapter.ready:`.
        """
        if not self.ready:
            raise RuntimeError(f"{self.provider} curator adapter has no API key set")
        build_fn = {
            "claude": self._build_claude,
            "openai": self._build_openai,
            "gemini": self._build_gemini,
        }[self.provider]
        url, headers, body = build_fn(prompt, system)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            return self._extract_text(resp.json())

    # ── Provider-specific request builders ────────────────────────────────

    def _build_claude(self, prompt: str, system: Optional[str]) -> tuple[str, dict, dict]:
        headers = {
            "x-api-key": os.environ[self._api_key_env()],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        return _CLAUDE_URL, headers, body

    def _build_openai(self, prompt: str, system: Optional[str]) -> tuple[str, dict, dict]:
        headers = {
            "Authorization": f"Bearer {os.environ[self._api_key_env()]}",
            "Content-Type": "application/json",
        }
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        return _OPENAI_URL, headers, body

    def _build_gemini(self, prompt: str, system: Optional[str]) -> tuple[str, dict, dict]:
        api_key = os.environ[self._api_key_env()]
        url = _GEMINI_URL_TMPL.format(model=self.model) + f"?key={api_key}"
        headers = {"Content-Type": "application/json"}
        parts: list[dict[str, str]] = []
        if system:
            parts.append({"text": f"System: {system}\n\n"})
        parts.append({"text": prompt})
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        return url, headers, body

    # ── Response extraction ───────────────────────────────────────────────

    def _extract_text(self, data: dict[str, Any]) -> str:
        try:
            if self.provider == "claude":
                # { "content": [{"type":"text","text":"..."}], ... }
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block.get("text", "")
                return ""
            if self.provider == "openai":
                # { "choices": [{"message":{"content":"..."}}] }
                return data["choices"][0]["message"]["content"]
            if self.provider == "gemini":
                # { "candidates":[{"content":{"parts":[{"text":"..."}]}}] }
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("curator: malformed %s response: %s", self.provider, exc)
        return ""


def build_curator_adapter() -> Optional[CuratorLLMAdapter]:
    """Factory that is safe to call from startup. Returns None on any failure."""
    try:
        return CuratorLLMAdapter.from_env()
    except Exception as exc:  # never let curator wiring break boot
        logger.warning("curator: adapter build failed — %s", exc)
        return None

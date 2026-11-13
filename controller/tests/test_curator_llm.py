from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings
from app.curator_llm import CuratorLLMAdapter, build_curator_adapter


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict, dict]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def post(self, url: str, *, headers: dict, json: dict) -> _FakeResponse:
        self.calls.append((url, headers, json))
        return self.response


class CuratorDefaultModelTests(unittest.IsolatedAsyncioTestCase):
    """STORE-31: the curator's own defaults (claude-opus-4-7, gpt-4o, gemini-1.5-pro) had gone stale beside the
    controller's model settings, so a curator with no CURATOR_MODEL called a model the agent providers do not."""

    def _settings(self, **extra: str) -> Settings:
        return Settings(
            _env_file=None, CLAUDE_MODEL="claude-set", OPENAI_MODEL="openai-set", GEMINI_MODEL="gemini-set", **extra
        )

    async def test_the_default_model_is_the_providers_model_setting(self) -> None:
        settings = self._settings()

        for provider, expected in (("claude", "claude-set"), ("openai", "openai-set"), ("gemini", "gemini-set")):
            with self.subTest(provider=provider):
                adapter = CuratorLLMAdapter(provider, settings=settings)
                self.assertEqual(adapter._default_model(), expected)
                self.assertEqual(adapter.model, expected)

    async def test_the_request_names_that_model(self) -> None:
        fake_client = _FakeAsyncClient(_FakeResponse({"content": []}))

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True),
            patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
        ):
            await CuratorLLMAdapter("claude", settings=self._settings()).complete("hi")

        self.assertEqual(fake_client.calls[0][2]["model"], "claude-set")

    async def test_curator_model_still_overrides_the_default(self) -> None:
        settings = self._settings(CURATOR_PROVIDER="openai", CURATOR_MODEL="curators-pick")

        with patch.dict(os.environ, {"OPENAI_API_KEY": "key"}, clear=True):
            adapter = CuratorLLMAdapter.from_env(settings)

        self.assertEqual(adapter.model, "curators-pick")
        self.assertEqual(CuratorLLMAdapter("openai", model="explicit", settings=settings).model, "explicit")

    async def test_from_env_with_no_curator_model_takes_the_setting_of_the_curators_provider(self) -> None:
        settings = self._settings(CURATOR_PROVIDER="gemini")

        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}, clear=True):
            adapter = CuratorLLMAdapter.from_env(settings)

        self.assertEqual(adapter.model, "gemini-set")


class CuratorLLMAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_from_env_returns_none_for_invalid_provider_or_missing_key(self) -> None:
        with patch.dict(os.environ, {"CURATOR_PROVIDER": "bogus"}, clear=False):
            self.assertIsNone(CuratorLLMAdapter.from_env())

        with patch.dict(os.environ, {"CURATOR_PROVIDER": "openai"}, clear=True):
            self.assertIsNone(CuratorLLMAdapter.from_env())

    async def test_from_env_missing_key_does_not_log_env_var_name(self) -> None:
        with (
            patch.dict(os.environ, {"CURATOR_PROVIDER": "openai"}, clear=True),
            self.assertLogs("app.curator_llm", level="INFO") as captured,
        ):
            self.assertIsNone(CuratorLLMAdapter.from_env())

        joined = "\n".join(captured.output)
        self.assertIn("provider=openai", joined)
        self.assertNotIn("OPENAI_API_KEY", joined)

    async def test_complete_builds_claude_request_and_extracts_text(self) -> None:
        fake_client = _FakeAsyncClient(_FakeResponse({"content": [{"type": "text", "text": "claude-output"}]}))
        adapter = CuratorLLMAdapter("claude", model="claude-test")

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "anthropic-key"}, clear=True),
            patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
        ):
            result = await adapter.complete("summarize this", system="be concise")

        self.assertEqual(result, "claude-output")
        url, headers, body = fake_client.calls[0]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "anthropic-key")
        self.assertEqual(body["system"], "be concise")
        self.assertEqual(body["messages"][0]["content"], "summarize this")

    async def test_claude_requests_follow_anthropic_base_url(self) -> None:
        # A gateway or proxy for Anthropic traffic (logging, data governance) must see the curator's calls too.
        fake_client = _FakeAsyncClient(_FakeResponse({"content": [{"type": "text", "text": "ok"}]}))
        adapter = CuratorLLMAdapter("claude", model="claude-test")

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "anthropic-key"}, clear=True),
            patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
            patch(
                "app.curator_llm.get_settings",
                return_value=SimpleNamespace(
                    anthropic_base_url="https://proxy.example/v1/", anthropic_version="2023-06-01"
                ),
            ),
        ):
            await adapter.complete("hello")

        self.assertEqual(fake_client.calls[0][0], "https://proxy.example/v1/messages")

    async def test_complete_builds_openai_request_and_extracts_text(self) -> None:
        fake_client = _FakeAsyncClient(_FakeResponse({"choices": [{"message": {"content": "openai-output"}}]}))
        adapter = CuratorLLMAdapter("openai", model="gpt-test")

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "openai-key"}, clear=True),
            patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
        ):
            result = await adapter.complete("hello world", system="system note")

        self.assertEqual(result, "openai-output")
        url, headers, body = fake_client.calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer openai-key")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "system note"})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "hello world"})

    async def test_complete_builds_gemini_request_and_extracts_text(self) -> None:
        fake_client = _FakeAsyncClient(
            _FakeResponse({"candidates": [{"content": {"parts": [{"text": "gemini-output"}]}}]})
        )
        adapter = CuratorLLMAdapter("gemini", model="gemini-test")

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True),
            patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
        ):
            result = await adapter.complete("idea list", system="use bullets")

        self.assertEqual(result, "gemini-output")
        url, headers, body = fake_client.calls[0]
        # In a header, not the URL: httpx puts the URL in its error messages,
        # and the curator logs those.
        self.assertTrue(url.endswith("gemini-test:generateContent"), url)
        self.assertNotIn("gemini-key", url)
        self.assertEqual(headers["x-goog-api-key"], "gemini-key")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "System: use bullets\n\n")
        self.assertEqual(body["contents"][0]["parts"][1]["text"], "idea list")

    async def test_the_gemini_model_is_quoted_into_the_url_path(self) -> None:
        # STORE-29: CURATOR_MODEL went into the path as written, so ?, # or a space ended or split it; quoted as
        # the Gemini provider adapter quotes provider_model ('/' and ':' kept for ids such as tunedModels/x).
        settings = SimpleNamespace(gemini_base_url="https://gemini.example/v1beta")
        fake_client = _FakeAsyncClient(_FakeResponse({"candidates": []}))

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "key"}, clear=True),
            patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
        ):
            await CuratorLLMAdapter("gemini", model="m 1?alt=sse#x", settings=settings).complete("hi")

        url = fake_client.calls[0][0]
        self.assertEqual(url, "https://gemini.example/v1beta/models/m%201%3Falt%3Dsse%23x:generateContent")

    async def test_openai_and_gemini_requests_follow_their_base_url_settings(self) -> None:
        # Same flaw as the Claude branch had: a gateway or proxy the operator routes provider traffic through.
        settings = SimpleNamespace(
            openai_base_url="https://openai-proxy.example/v1/",
            gemini_base_url="https://gemini-proxy.example/v1beta/",
            anthropic_base_url="https://unused.example/v1",
            anthropic_version="2024-01-01",
        )
        for provider, env_var, payload, expected in (
            ("openai", "OPENAI_API_KEY", {"choices": []}, "https://openai-proxy.example/v1/chat/completions"),
            (
                "gemini",
                "GEMINI_API_KEY",
                {"candidates": []},
                "https://gemini-proxy.example/v1beta/models/m-test:generateContent",
            ),
        ):
            with self.subTest(provider=provider):
                fake_client = _FakeAsyncClient(_FakeResponse(payload))
                with (
                    patch.dict(os.environ, {env_var: "key"}, clear=True),
                    patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
                    patch("app.curator_llm.get_settings", side_effect=AssertionError("cached settings were read")),
                ):
                    await CuratorLLMAdapter(provider, model="m-test", settings=settings).complete("hi")
                self.assertEqual(fake_client.calls[0][0], expected)

    async def test_the_claude_request_uses_the_settings_it_was_built_with(self) -> None:
        settings = SimpleNamespace(
            anthropic_base_url="https://injected.example/v1", anthropic_version="2024-01-01", claude_model="claude-x"
        )
        fake_client = _FakeAsyncClient(_FakeResponse({"content": []}))

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True),
            patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
            patch("app.curator_llm.get_settings", side_effect=AssertionError("cached settings were read")),
        ):
            await CuratorLLMAdapter("claude", settings=settings).complete("hi")

        url, headers, _ = fake_client.calls[0]
        self.assertEqual(url, "https://injected.example/v1/messages")
        self.assertEqual(headers["anthropic-version"], "2024-01-01")

    async def test_the_default_anthropic_version_is_unchanged(self) -> None:
        fake_client = _FakeAsyncClient(_FakeResponse({"content": []}))

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True),
            patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
        ):
            await CuratorLLMAdapter("claude").complete("hi")

        self.assertEqual(fake_client.calls[0][1]["anthropic-version"], "2023-06-01")

    async def test_complete_requires_api_key(self) -> None:
        adapter = CuratorLLMAdapter("openai")

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "no API key"):
                await adapter.complete("prompt")

    async def test_whitespace_around_the_key_is_stripped_in_every_header(self) -> None:
        # The curator reads its key from os.environ, so Settings' stripping (#248) never reached it.
        cases = (
            ("claude", "ANTHROPIC_API_KEY", "x-api-key", "anthropic-key", {"content": []}),
            ("openai", "OPENAI_API_KEY", "Authorization", "Bearer openai-key", {"choices": []}),
            ("gemini", "GEMINI_API_KEY", "x-goog-api-key", "gemini-key", {"candidates": []}),
        )
        for provider, env_var, header, expected, payload in cases:
            key = expected.removeprefix("Bearer ")
            for raw in (f" {key}", f"{key}\n", f"\t{key} "):
                with self.subTest(provider=provider, raw=raw):
                    fake_client = _FakeAsyncClient(_FakeResponse(payload))
                    with (
                        patch.dict(os.environ, {env_var: raw}, clear=True),
                        patch("app.curator_llm.httpx.AsyncClient", return_value=fake_client),
                    ):
                        await CuratorLLMAdapter(provider).complete("prompt")
                    self.assertEqual(fake_client.calls[0][1][header], expected)

    async def test_a_whitespace_only_key_counts_as_unset(self) -> None:
        with patch.dict(os.environ, {"CURATOR_PROVIDER": "openai", "OPENAI_API_KEY": "  \n"}, clear=True):
            self.assertFalse(CuratorLLMAdapter("openai").ready)
            self.assertIsNone(CuratorLLMAdapter.from_env())

    async def test_extract_text_returns_empty_for_malformed_payload(self) -> None:
        adapter = CuratorLLMAdapter("openai")

        self.assertEqual(adapter._extract_text({}), "")

    async def test_build_curator_adapter_swallows_factory_failures(self) -> None:
        with patch("app.curator_llm.CuratorLLMAdapter.from_env", side_effect=RuntimeError("boom")):
            self.assertIsNone(build_curator_adapter())


class CuratorOptInTests(unittest.TestCase):
    """A provider key alone no longer turns the curator on.

    ANTHROPIC_API_KEY is set by anyone running the Claude agent provider, and
    the curator then called a paid model on every session close.
    """

    def _init(self, settings: Settings):
        from types import SimpleNamespace

        from app.startup.extensions import _init_curator

        app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
            _init_curator(app)
        return app.state.curator_adapter

    def test_off_by_default_even_with_a_key(self) -> None:
        self.assertIsNone(self._init(Settings(_env_file=None)))

    def test_on_when_asked_for(self) -> None:
        self.assertIsNotNone(self._init(Settings(_env_file=None, CURATOR_ENABLED=True)))

    def test_the_adapter_is_built_with_the_controllers_settings_not_the_cached_ones(self) -> None:
        from types import SimpleNamespace

        from app.startup.extensions import _init_curator

        settings = Settings(
            _env_file=None,
            CURATOR_ENABLED=True,
            CURATOR_PROVIDER="openai",
            OPENAI_BASE_URL="https://proxy.example/v1",
        )
        app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        env = {"OPENAI_API_KEY": "key"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("app.curator_llm.get_settings", side_effect=AssertionError("cached settings were read")),
        ):
            _init_curator(app)
            url = app.state.curator_adapter._build_openai("prompt", None)[0]

        self.assertEqual(url, "https://proxy.example/v1/chat/completions")

    def test_enabling_says_the_review_is_not_wired(self) -> None:
        with self.assertLogs("app.startup.extensions", level="WARNING") as captured:
            self._init(Settings(_env_file=None, CURATOR_ENABLED=True))
        self.assertTrue(any("no model call" in line for line in captured.output), captured.output)

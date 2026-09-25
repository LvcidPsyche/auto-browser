from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.config import Settings
from app.providers.base import BaseProviderAdapter, ProviderAPIError


class FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, headers=None, json=None):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class DummyAdapter(BaseProviderAdapter):
    provider = "openai"

    @property
    def default_model(self) -> str:
        return "dummy"

    @property
    def configured(self) -> bool:
        return True

    @property
    def missing_detail(self) -> str:
        return ""

    async def _decide(self, **kwargs):  # pragma: no cover - unused here
        raise NotImplementedError


class ProviderResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        settings = Settings(_env_file=None)
        settings.artifact_root = str(root / "artifacts")
        settings.upload_root = str(root / "uploads")
        settings.auth_root = str(root / "auth")
        settings.approval_root = str(root / "approvals")
        settings.session_store_root = str(root / "sessions")
        settings.model_max_retries = 1
        settings.model_retry_backoff_seconds = 0
        self.adapter = DummyAdapter(settings)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_post_json_retries_retryable_status_codes(self) -> None:
        request = httpx.Request("POST", "https://example.com")
        responses = [
            httpx.Response(429, request=request, json={"error": {"message": "rate limited"}}),
            httpx.Response(200, request=request, json={"ok": True}),
        ]

        with patch("app.providers.base.httpx.AsyncClient", return_value=FakeAsyncClient(responses)):
            payload = await self.adapter._post_json(
                url="https://example.com",
                headers={},
                payload={"demo": True},
            )

        self.assertEqual(payload, {"ok": True})

    async def test_post_json_normalizes_final_provider_error(self) -> None:
        request = httpx.Request("POST", "https://example.com")
        responses = [
            httpx.Response(500, request=request, json={"error": {"message": "upstream exploded"}}),
            httpx.Response(500, request=request, json={"error": {"message": "still broken"}}),
        ]

        with patch("app.providers.base.httpx.AsyncClient", return_value=FakeAsyncClient(responses)):
            with self.assertRaises(ProviderAPIError) as ctx:
                await self.adapter._post_json(
                    url="https://example.com",
                    headers={},
                    payload={"demo": True},
                )

        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("still broken", str(ctx.exception))

    async def test_rate_limit_retry_waits_for_retry_after(self) -> None:
        request = httpx.Request("POST", "https://example.com")
        responses = [
            httpx.Response(429, request=request, headers={"retry-after": "7"}, json={}),
            httpx.Response(200, request=request, json={"ok": True}),
        ]
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        with (
            patch("app.providers.base.httpx.AsyncClient", return_value=FakeAsyncClient(responses)),
            patch("app.providers.base.asyncio.sleep", new=fake_sleep),
        ):
            await self.adapter._post_json(url="https://example.com", headers={}, payload={})

        self.assertEqual(sleeps, [7.0])
        huge = httpx.Response(429, request=request, headers={"retry-after": "86400"})
        self.assertEqual(self.adapter._retry_delay(1, huge), 30.0)
        date = httpx.Response(429, request=request, headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"})
        self.assertEqual(self.adapter._retry_delay(1, date), 0)

    async def test_cancelled_cli_call_kills_the_process(self) -> None:
        import asyncio
        import sys

        created: list[asyncio.subprocess.Process] = []
        real_exec = asyncio.create_subprocess_exec

        async def recording_exec(*args, **kwargs):
            process = await real_exec(*args, **kwargs)
            created.append(process)
            return process

        with patch("app.providers.base.asyncio.create_subprocess_exec", recording_exec):
            task = asyncio.create_task(
                self.adapter.run_cli(command=[sys.executable, "-c", "import time; time.sleep(30)"], env={"PATH": ""})
            )
            for _ in range(100):
                if created:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(len(created), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        process = created[0]
        try:
            # Killed by the cancel, not left running for the full sleep.
            self.assertIsNotNone(process.returncode)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
